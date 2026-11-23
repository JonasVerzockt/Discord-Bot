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
cogs/directory.py – /art (Artensteckbrief) und /shop (Händlerseite) in Discord.

Daten aus utils/directory (gemeinsam mit den Board-Seiten /art/<Name>, /shop/<Domain>).
Warnhinweise der Community erscheinen in Discord nur auf dem AAM-Server (MAP_GUILD_ID),
auf dem Board nur nach "Login mit Discord" als Server-Mitglied.
"""
import asyncio
import logging
from urllib.parse import quote

import discord
from discord.ext import commands

from config import BOARD_PUBLIC_URL, MAP_GUILD_ID
from utils import directory as D
from utils.currency import ensure_rates
from utils.localization import l10n, get_user_lang
from utils.sheet import warn_emoji
from cogs.server_settings import allowed_channel

logger = logging.getLogger(__name__)

_MAX_OFFERS = 10
_COLOR = 0x2EA043


def _eur(v) -> str:
    return f"{v:.2f} €".replace(".", ",") if v is not None else "–"


def _board(path: str) -> str:
    return f"{BOARD_PUBLIC_URL}{path}" if BOARD_PUBLIC_URL else ""


async def _ac_species(ctx: discord.AutocompleteContext):
    await ensure_rates()
    return await asyncio.to_thread(D.search_species, ctx.value or "", 25)


async def _ac_shop(ctx: discord.AutocompleteContext):
    await ensure_rates()
    reviews = await asyncio.to_thread(D.loaded_reviews)
    items = await asyncio.to_thread(D.search_shops, ctx.value or "", reviews, 25)
    return [discord.OptionChoice(name=f"{n} ({d})"[:100] if n != d else d, value=d) for n, d in items]


class _CardView(discord.ui.View):
    """Button unter /art: postet die teilbare Artenkarte als Bild (einmal pro Antwort)."""

    def __init__(self, bot, species: str, lang: str):
        super().__init__(timeout=600)
        self.bot, self.species, self.lang = bot, species, lang
        self.children[0].label = l10n.get("dir_card_btn", lang)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="📤")
    async def post_card(self, button: discord.ui.Button, interaction: discord.Interaction):
        import io
        from utils import species_card, season as SEA
        await interaction.response.defer()
        sp = await asyncio.to_thread(D.find_species, self.species)
        if not sp:
            return
        trend = await asyncio.to_thread(D.price_trend, sp, 12)
        prof = await SEA.profile(self.bot, sp["key"])
        url = (BOARD_PUBLIC_URL.split("://", 1)[-1] + f"/art/{D.slug(sp['name'])}") if BOARD_PUBLIC_URL else ""
        png = await asyncio.to_thread(species_card.render, sp, trend, self.lang, prof,
                                      SEA.MONTHS_SHORT.get(self.lang, SEA.MONTHS_SHORT["de"]), url)
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            l10n.get("dir_card_posted", self.lang, user=interaction.user.mention),
            file=discord.File(io.BytesIO(png), filename=f"{D.slug(sp['name'])}.png"),
            allowed_mentions=discord.AllowedMentions.none())


class DirectoryCog(commands.Cog, name="Directory"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /art ──────────────────────────────────────────────────────────────────
    @discord.slash_command(
        name="art",
        description="Species profile: offers, prices, price trend, rarity",
        description_localizations={"de": "Artensteckbrief: Angebote, Preise, Preisverlauf, Seltenheit"},
    )
    @allowed_channel()
    async def art(
        self, ctx: discord.ApplicationContext,
        name: discord.Option(str, "Species, e.g. Camponotus nicobarensis",
                             description_localizations={"de": "Art, z. B. Camponotus nicobarensis"},
                             autocomplete=_ac_species, required=True),
    ):
        await ctx.defer()
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ensure_rates()
        sp = await asyncio.to_thread(D.find_species, name)
        if not sp:
            canon = await asyncio.to_thread(D.canonical_name, name)
            key = "dir_art_not_offered" if canon else "dir_art_unknown"
            await ctx.followup.send(l10n.get(key, lang, name=canon or name))
            return
        trend = await asyncio.to_thread(D.price_trend, sp, 12)
        from utils import price_signal as PS, season as SEA
        sig = await asyncio.to_thread(PS.signals, [o["pid"] for o in sp["offers"] if o["in_stock"]])
        seas = SEA.describe(await SEA.profile(self.bot, sp["key"]), lang)

        offers = sorted(sp["offers"], key=lambda o: (not o["in_stock"], o["price"] is None, o["price"] or 0))
        instock = [o for o in offers if o["in_stock"]]
        n_shops = len(sp["shops"])
        e = discord.Embed(title=f"🐜 {sp['name']}", color=_COLOR,
                          url=_board(f"/art/{quote(D.slug(sp['name']))}") or None)
        rarity = (l10n.get("dir_art_rare", lang) if n_shops == 1
                  else l10n.get("dir_art_shops", lang, n=n_shops))
        e.description = l10n.get("dir_art_summary", lang, shops=n_shops, instock=len(instock),
                                 offers=len(offers)) + f"\n{rarity}"

        lines = []
        for o in instock[:_MAX_OFFERS]:
            mark = PS.EMOJI.get(sig.get(o["pid"]), "")
            lines.append(f"{mark + ' ' if mark else ''}[{o['shop']}](<{o['url']}>) · {D.fmt_price(o)}")
        if len(instock) > _MAX_OFFERS:
            lines.append(l10n.get("dir_more", lang, n=len(instock) - _MAX_OFFERS))
        if any(sig.get(o["pid"]) for o in instock[:_MAX_OFFERS]):
            lines.append(f"-# {l10n.get('price_sig_legend', lang)}")
        e.add_field(name=l10n.get("dir_art_instock_h", lang),
                    value="\n".join(lines) or l10n.get("dir_art_none_instock", lang), inline=False)

        e.add_field(name=l10n.get("dir_art_trend_h", lang), inline=False, value=l10n.get(
            "dir_art_trend", lang, now=_eur(trend["now"]), m3=_eur(trend["m3"]), m12=_eur(trend["m12"])))
        if seas:
            e.add_field(name=l10n.get("season_h", lang), value=seas, inline=False)
        e.set_footer(text=l10n.get("dir_art_footer", lang, name=sp["name"]))
        await ctx.followup.send(embed=e, view=_CardView(self.bot, sp["name"], lang))

    # ── /shop ─────────────────────────────────────────────────────────────────
    @discord.slash_command(
        name="shop",
        description="Shop profile: rating, assortment, price level, discount codes",
        description_localizations={"de": "Händlerseite: Bewertung, Sortiment, Preisniveau, Rabattcodes"},
    )
    @allowed_channel()
    async def shop(
        self, ctx: discord.ApplicationContext,
        name: discord.Option(str, "Shop name or domain",
                             description_localizations={"de": "Shopname oder Domain"},
                             autocomplete=_ac_shop, required=True),
    ):
        await ctx.defer()
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ensure_rates()
        reviews = await asyncio.to_thread(D.loaded_reviews)
        sh = await asyncio.to_thread(D.find_shop, name, reviews)
        if not sh:
            await ctx.followup.send(l10n.get("dir_shop_unknown", lang, name=name))
            return
        dom = sh["domain"]
        rv = reviews.get(dom)
        codes = await D.active_codes(self.bot, dom)
        partner = D.partner_of(dom)

        e = discord.Embed(title=f"🏪 {sh['name']}" + (" 🤝" if partner else ""), color=_COLOR,
                          url=_board(f"/shop/{quote(dom)}") or None)
        e.description = f"<{sh['url']}>"
        delivery = await D.delivery_of(self.bot, dom)
        if rv and rv["avg"] is not None:
            val = l10n.get("dir_shop_rating", lang, avg=f"{rv['avg']:.2f}".replace(".", ","),
                           n=rv["count"], last=rv["last"])
            if delivery:
                val += "\n" + l10n.get("dir_shop_delivery", lang,
                                        d=f"{delivery['avg']:.1f}".replace(".", ","), n=delivery["n"])
            e.add_field(name=l10n.get("dir_shop_rating_h", lang), inline=False, value=val)
        else:
            e.add_field(name=l10n.get("dir_shop_rating_h", lang),
                        value=l10n.get("dir_shop_no_reviews", lang), inline=False)
        if sh["offers_count"]:
            val = l10n.get("dir_shop_assort", lang, species=sh["species_count"],
                           offers=sh["offers_count"], instock=sh["instock_count"],
                           excl=len(sh["exclusive"]))
            if sh["price_level"] is not None:
                pl = sh["price_level"]
                key = "dir_shop_level_avg" if abs(pl) <= 2 else (
                    "dir_shop_level_below" if pl < 0 else "dir_shop_level_above")
                val += "\n" + l10n.get(key, lang, p=abs(pl), n=sh["price_level_n"])
            e.add_field(name=l10n.get("dir_shop_assort_h", lang), value=val, inline=False)
        if codes:
            cl = []
            for c in codes[:8]:
                extra = f" · {c['discount']}" if c["discount"] else ""
                if c["min_order"]:
                    extra += " · " + l10n.get("discount_min_order", lang, min_order=c["min_order"]).strip(" ·")
                cl.append(f"`{c['code']}`{extra}")
            e.add_field(name=l10n.get("dir_shop_codes_h", lang), value="\n".join(cl), inline=False)
        # Warnhinweise nur auf dem AAM-Server
        if MAP_GUILD_ID and ctx.guild_id == MAP_GUILD_ID:
            warns = D.warnings_of(dom)
            if warns:
                wl = [f"{warn_emoji(w.get('level'))} {w.get('text','')}"
                      + (f" ({w['date']})" if w.get("date") else "") for w in warns[:5]]
                e.add_field(name=l10n.get("dir_shop_warn_h", lang),
                            value="\n".join(wl)[:1000], inline=False)
        if partner:
            e.set_footer(text=l10n.get("dir_shop_partner", lang))
        await ctx.followup.send(embed=e)


def setup(bot: discord.Bot):
    bot.add_cog(DirectoryCog(bot))
