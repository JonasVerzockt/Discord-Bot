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
cogs/offers.py – /offers: listet alle lagernden Angebote eines Shops.

Gegenstück zu /sells (das pro Art sucht). Datenquelle wie das restliche
Preis-Tracking: shops_data.json (Grabber, antcheck.info) inkl. Varianten.
Öffentliche Ausgabe, nur lagernde (in_stock+is_active) Produkte, gruppiert
nach Produkt → Varianten-Einzelpreise (Fallback: Produkt-Preisspanne).
"""
import discord
from discord.ext import commands

from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data, available_variants
from utils.currency import ensure_rates
from utils.countries import flag_emoji
from cogs.server_settings import allowed_channel
from cogs.sells import _price_md, _chunks, _read_fetched_at
from utils.text_chunks import chunk_lines
from utils.embeds import EMBED_COLOR


class OffersCog(commands.Cog, name="Offers"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="offers",
        description="List all in-stock ant offers of a shop",
        description_localizations={"de": "Alle lagernden Ameisen-Angebote eines Shops auflisten"},
    )
    @allowed_channel()
    async def offers(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(  # type: ignore[valid-type]
            str,
            "Shop name (also partial), e.g. Antstore",
            description_localizations={"de": "Shopname (auch teilweise), z.B. Antstore", "en-US": "Shop name (also partial), e.g. Antstore"},
            required=True,
        ),
    ):
        await ctx.defer()
        lang  = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        query = shop.strip()
        ql    = query.lower()

        await ensure_rates()
        shop_data = await load_shop_data(self.bot)
        shops = list(shop_data.values())

        exact   = [s for s in shops if (s.get("name") or "").strip().lower() == ql]
        matches = exact if exact else [s for s in shops if ql in (s.get("name") or "").lower()]

        if not matches:
            await ctx.followup.send(l10n.get("offers_no_shop", lang, query=query))
            return
        if len(matches) > 1:
            names = "\n".join(
                f"• {s.get('name', '?')}"
                for s in sorted(matches, key=lambda s: (s.get("name") or "").lower())
            )
            await ctx.followup.send(l10n.get("offers_multi", lang, shops=names))
            return

        shop_info = matches[0]
        shop_name = shop_info.get("name", "?")
        country   = (shop_info.get("country") or "").lower()
        prods = [
            p for p in shop_info.get("products", [])
            if p.get("in_stock") and p.get("is_active")
        ]
        if not prods:
            await ctx.followup.send(l10n.get("offers_none", lang, shop=shop_name))
            return

        prods.sort(key=lambda p: ((p.get("species") or "").lower(), (p.get("title") or "").lower()))

        parts = [f"{flag_emoji(country)} **{shop_name}**"]
        if shop_info.get("url"):
            parts.append(f"<{shop_info['url']}>")
        parts.append(l10n.get("sells_source", lang))
        parts.append(l10n.get("sells_disclaimer", lang))

        for p in prods:
            parts.append("")
            title = (p.get("title") or p.get("species") or "?").strip()
            parts.append(f"**{title}**")
            purl = (p.get("antcheck_url") or p.get("shop_url") or "").strip()
            if purl:
                parts.append(l10n.get("sells_product_link", lang, url=purl))
            cur = p.get("currency_iso") or "EUR"
            vs  = available_variants(p)
            if vs:
                for i, v in enumerate(vs, 1):
                    label  = v.get("title") or v.get("description") or f"Variante {i}"
                    vprice = _price_md(v.get("price"), v.get("price"), v.get("currency_iso") or cur)
                    parts.append(f"{label}: {vprice}")
            else:
                parts.append(_price_md(p.get("min_price"), p.get("max_price"), cur))

        parts.append("")
        parts.append(l10n.get("sells_footer", lang, ts=_read_fetched_at() or "?"))

        for chunk in chunk_lines("\n".join(parts), 4000):
            await ctx.followup.send(
                embed=discord.Embed(description=chunk, color=EMBED_COLOR)
            )


def setup(bot: discord.Bot):
    bot.add_cog(OffersCog(bot))
