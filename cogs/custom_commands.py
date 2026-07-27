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
cogs/custom_commands.py – Benutzerdefinierte Text-Befehle ("Info-Einträge").

Admins legen per /info_add einen benannten Text an (Markdown 1:1, Eingabe über ein
mehrzeiliges Modal). Abruf über /info name:<Autocomplete>.

Verhalten beim Abruf:
  • Admin-only-Eintrag  → Antwort NUR für den Aufrufer sichtbar (ephemer).
  • Öffentlicher Eintrag → für alle im Kanal sichtbar.
  • Inhalt ab _EMBED_THRESHOLD Zeichen → automatisch als Embed (Limit ~4000),
    darunter als reiner Text.

Bewusst KEINE zur Laufzeit registrierten Slash-Commands (Discord-100er-Limit):
ein einziger /info-Command mit Autocomplete bedient beliebig viele Einträge aus
der DB (Tabelle custom_commands). Ausgaben mit allowed_mentions=none. Admin-only-
Einträge sind für normale User unsichtbar (Autocomplete/Liste/Direktaufruf).
"""
import re
import logging

import discord
from discord.ext import commands

from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.embeds import send_embeds, EMBED_COLOR
from cogs.server_settings import allowed_channel, admin_or_manage_messages

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_MAX_LEN = 4000            # Modal-Eingabelimit (Embed-Beschreibung fasst bis ~4096)
_EMBED_THRESHOLD = 1000    # ab so vielen Zeichen automatisch als Embed posten
_NO_PINGS = discord.AllowedMentions.none()


def _norm_name(name: str) -> str:
    return (name or "").strip().lower()


async def _fetch(bot, name: str):
    rows = await execute_db(
        bot,
        "SELECT name, content, admin_only FROM custom_commands WHERE name=?",
        (_norm_name(name),), fetch=True,
    )
    return rows[0] if rows else None


async def _is_admin(interaction_or_ctx) -> bool:
    author = getattr(interaction_or_ctx, "author", None) or getattr(interaction_or_ctx, "user", None)
    perms = getattr(author, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_messages))


# ── Autocomplete ──────────────────────────────────────────────────────────────

async def _info_autocomplete(ctx: discord.AutocompleteContext):
    """Schlägt Namen vor. Für Nicht-Admins nur öffentliche Einträge."""
    try:
        is_admin = bool(
            ctx.interaction.user and ctx.interaction.user.guild_permissions
            and (ctx.interaction.user.guild_permissions.administrator
                 or ctx.interaction.user.guild_permissions.manage_messages)
        )
    except Exception:
        is_admin = False
    q = (ctx.value or "").lower()
    sql = "SELECT name FROM custom_commands"
    if not is_admin:
        sql += " WHERE admin_only=0"
    rows = await execute_db(ctx.bot, sql + " ORDER BY name", fetch=True) or []
    return [r["name"] for r in rows if q in r["name"]][:25]


async def _all_info_autocomplete(ctx: discord.AutocompleteContext):
    """Alle Einträge (für Admin-Verwaltung)."""
    q = (ctx.value or "").lower()
    rows = await execute_db(ctx.bot, "SELECT name FROM custom_commands ORDER BY name", fetch=True) or []
    return [r["name"] for r in rows if q in r["name"]][:25]


# ── Modal für Inhalt (mehrzeilig, Markdown) ───────────────────────────────────

class _InfoModal(discord.ui.Modal):
    def __init__(self, cog, lang, name, admin_only, existing="", is_edit=False):
        super().__init__(title=l10n.get(
            "info_modal_title_edit" if is_edit else "info_modal_title_add", lang, name=name)[:45])
        self.cog = cog
        self.lang = lang
        self.name = name
        self.admin_only = admin_only
        self.is_edit = is_edit
        self.add_item(discord.ui.InputText(
            label=l10n.get("info_modal_label", lang)[:45],
            style=discord.InputTextStyle.long,
            max_length=_MAX_LEN,
            required=True,
            value=existing or None,
        ))

    async def callback(self, interaction: discord.Interaction):
        content = self.children[0].value or ""
        uid = str(interaction.user.id)
        # as_embed rein informativ mitschreiben (Ausgabe entscheidet ohnehin per Länge).
        as_embed = 1 if len(content) >= _EMBED_THRESHOLD else 0
        if self.is_edit:
            await execute_db(
                self.cog.bot,
                "UPDATE custom_commands SET content=?, admin_only=?, as_embed=?, updated_at=datetime('now') WHERE name=?",
                (content, self.admin_only, as_embed, self.name), commit=True,
            )
            msg = l10n.get("info_updated", self.lang, name=self.name)
        else:
            await execute_db(
                self.cog.bot,
                "INSERT INTO custom_commands (name, content, admin_only, as_embed, created_by) VALUES (?, ?, ?, ?, ?)",
                (self.name, content, self.admin_only, as_embed, uid), commit=True,
            )
            msg = l10n.get("info_added", self.lang, name=self.name)
        logger.info("ℹ️ Info-Eintrag %s: '%s' (admin_only=%s) von %s",
                    "aktualisiert" if self.is_edit else "angelegt", self.name, self.admin_only, uid)
        await interaction.response.send_message(msg, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class CustomCommandsCog(commands.Cog, name="CustomCommands"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /info – Eintrag abrufen ────────────────────────────────────────────────
    @discord.slash_command(
        name="info",
        description="Show a custom info entry",
        description_localizations={"de": "Einen benutzerdefinierten Info-Eintrag anzeigen"},
    )
    @allowed_channel()
    async def info(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(  # type: ignore[valid-type]
            str, "Entry name",
            description_localizations={"de": "Name des Eintrags", "en-US": "Entry name"},
            autocomplete=_info_autocomplete, required=True,
        ),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        row = await _fetch(self.bot, name)
        # Admin-only Einträge sind für normale User unsichtbar: gleiche Antwort wie
        # bei unbekannten Einträgen, damit ihre Existenz nicht verraten wird.
        if not row or (row["admin_only"] and not await _is_admin(ctx)):
            await ctx.respond(l10n.get("info_not_found", lang, name=_norm_name(name)), ephemeral=True)
            return
        await execute_db(
            self.bot, "UPDATE custom_commands SET uses = uses + 1 WHERE name=?",
            (row["name"],), commit=True,
        )
        content = row["content"]
        # Admin-only → nur für den Aufrufer (ephemer); öffentlich → für alle sichtbar.
        ephem = bool(row["admin_only"])
        # Ab _EMBED_THRESHOLD Zeichen als Embed (fasst mehr), sonst reiner Text.
        if len(content) >= _EMBED_THRESHOLD:
            await ctx.respond(
                embed=discord.Embed(description=content, color=EMBED_COLOR),
                allowed_mentions=_NO_PINGS, ephemeral=ephem,
            )
        else:
            await ctx.respond(content, allowed_mentions=_NO_PINGS, ephemeral=ephem)

    # ── /info_list – Übersicht ─────────────────────────────────────────────────
    @discord.slash_command(
        name="info_list",
        description="List available info entries",
        description_localizations={"de": "Verfügbare Info-Einträge auflisten"},
    )
    @allowed_channel()
    async def info_list(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        is_admin = await _is_admin(ctx)
        rows = await execute_db(
            self.bot, "SELECT name, admin_only FROM custom_commands ORDER BY name", fetch=True) or []
        if not is_admin:
            rows = [r for r in rows if not r["admin_only"]]
        if not rows:
            await ctx.respond(l10n.get("info_list_empty", lang), ephemeral=True)
            return
        names = ", ".join(f"`{r['name']}`" + (" 🔒" if r["admin_only"] else "") for r in rows)
        await send_embeds(ctx, l10n.get("info_list_header", lang, count=len(rows)) + "\n" + names)

    # ── /info_add (Admin) ──────────────────────────────────────────────────────
    @discord.slash_command(
        name="info_add",
        description="🔒 [Admin] Create a custom info entry",
        description_localizations={"de": "🔒 [Admin] Benutzerdefinierten Info-Eintrag anlegen"},
    )
    @admin_or_manage_messages()
    async def info_add(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, "Name (a-z, 0-9, _ , -)", description_localizations={"de": "Name (a-z, 0-9, _ , -)", "en-US": "Name (a-z, 0-9, _ , -)"}, required=True),  # type: ignore[valid-type]
        admin_only: discord.Option(bool, "Only admins may use this entry (reply shown only to them)", name_localizations={"de": "nur_admin"}, description_localizations={"de": "Nur Admins dürfen diesen Eintrag nutzen (Antwort nur für sie sichtbar)"}, required=False, default=False),  # type: ignore[valid-type]
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        nm = _norm_name(name)
        if not _NAME_RE.match(nm):
            await ctx.respond(l10n.get("info_invalid_name", lang), ephemeral=True)
            return
        if await _fetch(self.bot, nm):
            await ctx.respond(l10n.get("info_exists", lang, name=nm), ephemeral=True)
            return
        await ctx.send_modal(_InfoModal(self, lang, nm, 1 if admin_only else 0, is_edit=False))

    # ── /info_edit (Admin) ─────────────────────────────────────────────────────
    @discord.slash_command(
        name="info_edit",
        description="🔒 [Admin] Edit an info entry (text and/or admin-only flag)",
        description_localizations={"de": "🔒 [Admin] Info-Eintrag bearbeiten (Text und/oder Nur-Admin-Flag)"},
    )
    @admin_or_manage_messages()
    async def info_edit(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, "Entry name", description_localizations={"de": "Name des Eintrags", "en-US": "Entry name"}, autocomplete=_all_info_autocomplete, required=True),  # type: ignore[valid-type]
        admin_only: discord.Option(bool, "Only admins may use this entry", name_localizations={"de": "nur_admin"}, description_localizations={"de": "Nur Admins dürfen diesen Eintrag nutzen"}, required=False, default=None),  # type: ignore[valid-type]
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        row = await _fetch(self.bot, name)
        if not row:
            await ctx.respond(l10n.get("info_not_found", lang, name=_norm_name(name)), ephemeral=True)
            return
        ao = row["admin_only"] if admin_only is None else (1 if admin_only else 0)
        await ctx.send_modal(_InfoModal(
            self, lang, row["name"], ao, existing=row["content"], is_edit=True))

    # ── /info_remove (Admin) ───────────────────────────────────────────────────
    @discord.slash_command(
        name="info_remove",
        description="🔒 [Admin] Delete an info entry",
        description_localizations={"de": "🔒 [Admin] Info-Eintrag löschen"},
    )
    @admin_or_manage_messages()
    @allowed_channel()
    async def info_remove(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, "Entry name", description_localizations={"de": "Name des Eintrags", "en-US": "Entry name"}, autocomplete=_all_info_autocomplete, required=True),  # type: ignore[valid-type]
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        nm = _norm_name(name)
        rc = await execute_db(
            self.bot, "DELETE FROM custom_commands WHERE name=?", (nm,), commit=True)
        if not rc:
            await ctx.followup.send(l10n.get("info_not_found", lang, name=nm), ephemeral=True)
            return
        logger.info("ℹ️ Info-Eintrag gelöscht: '%s' von %s", nm, ctx.author.id)
        await ctx.followup.send(l10n.get("info_removed", lang, name=nm), ephemeral=True)

    # ── /info_raw (Admin) – Quelltext anzeigen ─────────────────────────────────
    @discord.slash_command(
        name="info_raw",
        description="🔒 [Admin] Show the raw source of an info entry",
        description_localizations={"de": "🔒 [Admin] Quelltext eines Info-Eintrags anzeigen"},
    )
    @admin_or_manage_messages()
    async def info_raw(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(str, "Entry name", description_localizations={"de": "Name des Eintrags", "en-US": "Entry name"}, autocomplete=_all_info_autocomplete, required=True),  # type: ignore[valid-type]
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        row = await _fetch(self.bot, name)
        if not row:
            await ctx.followup.send(l10n.get("info_not_found", lang, name=_norm_name(name)), ephemeral=True)
            return
        flags = []
        if row["admin_only"]:
            flags.append("🔒 admin_only (ephemer)")
        flags.append("Embed" if len(row["content"]) >= _EMBED_THRESHOLD else "Text")
        header = l10n.get("info_raw_header", lang, name=row["name"]) + " (" + ", ".join(flags) + ")"
        body = row["content"].replace("`", "ˋ")  # Backticks entschärfen für den Codeblock
        await ctx.followup.send(f"{header}\n```\n{body[:1900]}\n```", ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(CustomCommandsCog(bot))
