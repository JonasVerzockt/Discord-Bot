# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jonas Beier
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
cogs/bewerten.py – /bewerten: Shop-Bewertung per Formular ausfüllen, fertigen Text kopieren.

Der Bot postet NICHTS in den Bewertungskanal. Er baut aus zwei Formularen (Discord
erlaubt höchstens 5 Textfelder je Formular) den Text exakt nach der Community-Vorlage;
leere Felder bleiben mit Überschrift stehen, damit jede Bewertung gleich aussieht.
Die Person kopiert den Text und postet ihn selbst – der Bewertungs-Bot verarbeitet ihn
dann wie jede andere Bewertung.

Ablauf: /bewerten shop:<Shop> → Formular 1 → Vorschau mit „Teil 2“ → Formular 2 (inkl.
Note, Freitext 0–10, z. B. 7,5) → Vorschau → „Fertig“ → Text im Kopierblock.
"""
import re

import discord
from discord.ext import commands

from config import REVIEW_CHANNEL_ID
from utils.localization import l10n, get_user_lang
from cogs.server_settings import allowed_channel

# Vorlage (Reihenfolge, Leerzeilen und Überschriften exakt wie im Bewertungskanal).
# (Feld, Shortcode, Überschrift, Leerzeile davor)
TEMPLATE = [
    ("shop",      ":shopping_cart:", "Shop:", False),
    ("ordered",   ":pencil:",        "Bestellt:", True),
    ("price",     ":dollar:",        "Preis(e):", False),
    ("shipping",  ":truck:",         "Versand:", True),
    ("packaging", ":package:",       "Verpackung und Sicherung:", False),
    ("quality",   ":medal:",         "Verarbeitung/Qualität:", False),
    ("health",    ":green_heart:",   "Gesundheit d. Tiere (falls vorhanden):", False),
    ("contact",   ":envelope:",      "Kundenkontakt (bei Sonderwünschen oder Mängeln):", True),
    ("verdict",   ":scroll:",        "Fazit mit Skala 0-10:", True),
]
# Für die Vorschau (Bot-Nachrichten wandeln Shortcodes nicht um).
EMOJI = {":shopping_cart:": "🛒", ":pencil:": "📝", ":dollar:": "💵", ":truck:": "🚚",
         ":package:": "📦", ":medal:": "🏅", ":green_heart:": "💚", ":envelope:": "✉️",
         ":scroll:": "📜"}
PART1 = ("ordered", "price", "shipping", "packaging", "quality")
PART2 = ("health", "contact", "verdict")
MAX_LEN = 1900                      # Discord-Limit 2000, Luft für den Kopierblock
_NOTE_RE = re.compile(r"^\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:/\s*10)?\s*$")


def parse_note(raw: str) -> str | None:
    """'7' / '7,5' / '1.4' / '8/10' → '7' / '7,5' / '1,4' / '8' (0 bis 10), sonst None."""
    m = _NOTE_RE.match(raw or "")
    if not m:
        return None
    txt = m.group(1).replace(".", ",")
    try:
        val = float(txt.replace(",", "."))
    except ValueError:
        return None
    if not 0 <= val <= 10:
        return None
    if "," in txt:
        txt = txt.rstrip("0").rstrip(",")
    return txt


def build(data: dict, shortcodes: bool = True) -> str:
    """Bewertungstext nach Vorlage. Leere Felder bleiben mit Überschrift stehen."""
    lines = []
    for key, code, label, gap in TEMPLATE:
        if gap:
            lines.append("")
        if key == "verdict":
            note = data.get("note") or ""
            text = (data.get("verdict") or "").strip()
            val = f"{note}/10" if note else ""
            if text:
                val = f"{val}\n{text}" if val else text
        else:
            val = (data.get(key) or "").strip()
        icon = code if shortcodes else EMOJI[code]
        lines.append(f"{icon} {label} {val}".rstrip() if val else f"{icon} {label}")
    return "\n".join(lines)


def _clean(v: str) -> str:
    return (v or "").replace("```", "'''").strip()


class _Part1(discord.ui.Modal):
    def __init__(self, view: "ReviewWizard"):
        L = lambda k: l10n.get(k, view.lang)[:45]
        super().__init__(title=l10n.get("rv_modal1_title", view.lang)[:45])
        self.v = view
        d = view.data
        for key, lab, style, mx in (("ordered", "rv_f_ordered", discord.InputTextStyle.long, 400),
                                    ("price", "rv_f_price", discord.InputTextStyle.short, 200),
                                    ("shipping", "rv_f_shipping", discord.InputTextStyle.long, 400),
                                    ("packaging", "rv_f_packaging", discord.InputTextStyle.long, 400),
                                    ("quality", "rv_f_quality", discord.InputTextStyle.long, 400)):
            self.add_item(discord.ui.InputText(label=L(lab), style=style, required=False,
                                               max_length=mx, value=d.get(key) or None))

    async def callback(self, interaction: discord.Interaction):
        for key, item in zip(PART1, self.children):
            self.v.data[key] = _clean(item.value)
        await self.v.refresh(interaction)


class _Part2(discord.ui.Modal):
    def __init__(self, view: "ReviewWizard"):
        L = lambda k: l10n.get(k, view.lang)[:45]
        super().__init__(title=l10n.get("rv_modal2_title", view.lang)[:45])
        self.v = view
        d = view.data
        self.add_item(discord.ui.InputText(label=L("rv_f_health"), style=discord.InputTextStyle.long,
                                           required=False, max_length=300, value=d.get("health") or None))
        self.add_item(discord.ui.InputText(label=L("rv_f_contact"), style=discord.InputTextStyle.long,
                                           required=False, max_length=400, value=d.get("contact") or None))
        self.add_item(discord.ui.InputText(label=L("rv_f_verdict"), style=discord.InputTextStyle.long,
                                           required=False, max_length=500, value=d.get("verdict") or None))
        self.add_item(discord.ui.InputText(label=L("rv_f_note"), style=discord.InputTextStyle.short,
                                           required=True, max_length=6, placeholder="7,5",
                                           value=d.get("note_raw") or None))

    async def callback(self, interaction: discord.Interaction):
        h, c, v, n = (item.value for item in self.children)
        self.v.data.update(health=_clean(h), contact=_clean(c), verdict=_clean(v), note_raw=(n or "").strip())
        self.v.data["note"] = parse_note(n)
        self.v.part2_done = True
        await self.v.refresh(interaction)


class ReviewWizard(discord.ui.View):
    def __init__(self, owner_id: int, lang: str, shop: str):
        super().__init__(timeout=1800)
        self.owner_id, self.lang = owner_id, lang
        self.data = {"shop": shop}
        self.part2_done = False
        self._buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    def _buttons(self):
        self.clear_items()
        L = lambda k: l10n.get(k, self.lang)
        b1 = discord.ui.Button(label=L("rv_btn_part1"), style=discord.ButtonStyle.secondary, emoji="✏️")
        b2 = discord.ui.Button(label=L("rv_btn_part2_edit" if self.part2_done else "rv_btn_part2"),
                               style=discord.ButtonStyle.secondary if self.part2_done else discord.ButtonStyle.primary,
                               emoji="✏️" if self.part2_done else "➡️")
        done = discord.ui.Button(label=L("rv_btn_done"), style=discord.ButtonStyle.success, emoji="✅",
                                 disabled=not (self.part2_done and self.data.get("note")))
        stop = discord.ui.Button(label=L("rv_btn_cancel"), style=discord.ButtonStyle.secondary)

        async def on1(i): await i.response.send_modal(_Part1(self))
        async def on2(i): await i.response.send_modal(_Part2(self))

        async def on_done(i):
            text = build(self.data, shortcodes=True)
            if len(text) > MAX_LEN:
                await i.response.send_message(l10n.get("rv_too_long", self.lang, n=len(text), max=MAX_LEN),
                                              ephemeral=True)
                return
            self.stop()
            where = f"<#{REVIEW_CHANNEL_ID}>" if REVIEW_CHANNEL_ID else l10n.get("rv_channel_fallback", self.lang)
            # Hinweis und reine Bewertung getrennt: Am Handy kopiert „Text kopieren“ immer die ganze
            # Nachricht. Die Bewertung steht deshalb allein, ohne Codeblock und mit echten Emojis.
            await i.response.edit_message(content=l10n.get("rv_copy", self.lang, channel=where), view=None)
            await i.followup.send(build(self.data, shortcodes=False)[:2000], ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())

        async def on_stop(i):
            self.stop()
            await i.response.edit_message(content=l10n.get("rv_cancelled", self.lang), view=None)

        b1.callback, b2.callback, done.callback, stop.callback = on1, on2, on_done, on_stop
        for b in (b2, b1, done, stop):
            self.add_item(b)

    def preview(self) -> str:
        head = l10n.get("rv_preview_h", self.lang)
        if not self.part2_done:
            hint = l10n.get("rv_hint_part2", self.lang)
        elif not self.data.get("note"):
            hint = l10n.get("rv_bad_note", self.lang, note=self.data.get("note_raw") or "")
        else:
            hint = l10n.get("rv_hint_done", self.lang)
        body = build(self.data, shortcodes=False)
        return f"{head}\n\n{body}\n\n{hint}"[:2000]

    async def refresh(self, interaction: discord.Interaction):
        self._buttons()
        if interaction.message is not None:
            await interaction.response.edit_message(content=self.preview(), view=self)
        else:
            await interaction.response.send_message(self.preview(), view=self, ephemeral=True)


async def _ac_shop(ctx: discord.AutocompleteContext):
    try:
        from cogs.directory import _ac_shop as ac
        return await ac(ctx)
    except Exception:
        return []


class BewertenCog(commands.Cog, name="Bewerten"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="bewerten",
        description="Fill in a shop review using the template and copy the finished text",
        description_localizations={"de": "Shop-Bewertung nach Vorlage ausfüllen und fertigen Text kopieren"},
    )
    @allowed_channel()
    async def bewerten(
        self, ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop (suggestions as you type, free text allowed)",
                             description_localizations={"de": "Shop (Vorschläge beim Tippen, Freitext erlaubt)"},
                             autocomplete=_ac_shop, required=True, max_length=100),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        view = ReviewWizard(ctx.author.id, lang, _clean(shop))
        await ctx.send_modal(_Part1(view))


def setup(bot: discord.Bot):
    bot.add_cog(BewertenCog(bot))
