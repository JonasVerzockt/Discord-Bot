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
cogs/sells.py – /sells: Preisvergleich einer Art/Gattung über alle Shops.

Datenquelle ist – wie beim restlichen Preis-Tracking – shops_data.json
(vom Grabber aus antcheck.info).
Zeigt nur lagernde (in_stock + is_active) Angebote, öffentlich im Kanal,
gruppiert nach Art → Shop. Optionaler Länderfilter.
"""
import asyncio
import json
import logging
from datetime import datetime

import discord
from discord.ext import commands

from config import SHOPS_DATA_FILE
from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data, normalize_species_name, strip_html
from utils.currency import ensure_rates, to_eur
from utils.timez import berlin_from_iso
from utils.text_chunks import chunk_lines
from utils.embeds import EMBED_COLOR
from utils.sheet import get_shop_warnings, warn_emoji
from utils.countries import flag_emoji
from cogs.server_settings import allowed_channel

logger = logging.getLogger(__name__)

_MAX_LEN = 2000


def _read_fetched_at() -> str | None:
    """_meta.fetched_at aus shops_data.json → 'DD.MM.YYYY HH:MM UTC'."""
    try:
        with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        ts = raw.get("_meta", {}).get("fetched_at") if isinstance(raw, dict) else None
        if ts:
            return berlin_from_iso(ts) or str(ts)
    except Exception:
        pass
    return None


def _fnum(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _has_price(v) -> bool:
    """True, wenn ein echter positiver Preis vorliegt (nicht 0/leer/unbekannt)."""
    f = _fnum(v)
    return f is not None and f > 0


def _price_md(min_v, max_v, cur: str) -> str:
    """Fett: Originalpreis(-spanne); bei Nicht-EUR zusätzlich kursiv die EUR-Umrechnung."""
    cur = (cur or "EUR").upper()
    lo, hi = _fnum(min_v), _fnum(max_v)
    if lo is None:
        return "?"
    if hi is None or abs(hi - lo) < 0.005:
        orig = f"{lo:.2f} {cur}"
    else:
        orig = f"{lo:.2f}–{hi:.2f} {cur}"
    out = f"**{orig}**"
    if cur != "EUR":
        lo_eur = to_eur(lo, cur)
        hi_eur = to_eur(hi, cur) if hi is not None else None
        if lo_eur is not None:
            if hi_eur is None or abs(hi_eur - lo_eur) < 0.005:
                out += f" (*ca. {lo_eur:.2f} EUR*)"
            else:
                out += f" (*ca. {lo_eur:.2f}–{hi_eur:.2f} EUR*)"
    return out


def _chunks(text: str, max_len: int = _MAX_LEN) -> list[str]:
    """Teilt Text an Zeilenumbrüchen in Discord-taugliche Stücke (<= max_len)."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_len and cur:
            out.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out or [text]


class SellsCog(commands.Cog, name="Sells"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="sells",
        description="Compare offers for an ant species/genus across all shops",
        description_localizations={"de": "Angebote für eine Ameisenart/Gattung über alle Shops vergleichen"},
    )
    @allowed_channel()
    async def sells(
        self,
        ctx: discord.ApplicationContext,
        species: discord.Option(  # type: ignore[valid-type]
            str,
            "Ant species or genus (also partial), e.g. aethiops or Lasius flavus",
            description_localizations={"de": "Ameisenart oder Gattung (auch teilweise), z.B. aethiops oder Lasius flavus", "en-US": "Ant species or genus (also partial), e.g. aethiops or Lasius flavus"},
            required=True,
        ),
        country: discord.Option(  # type: ignore[valid-type]
            str,
            "Optional: filter by country code (de, at, pl, ...)",
            description_localizations={"de": "Optional: nach Ländercode filtern (de, at, pl, ...)", "en-US": "Optional: filter by country code (de, at, pl, ...)"},
            required=False,
            default=None,
        ),
    ):
        await ctx.defer()
        lang   = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        query  = species.strip()
        search = normalize_species_name(query)
        cc     = (country or "").strip().lower() or None

        await ensure_rates()
        shop_data = await load_shop_data(self.bot)

        found_species: set[str] = set()
        offers: dict[str, list] = {}
        for shop in shop_data.values():
            scountry = (shop.get("country") or "").strip().lower()
            if cc and scountry != cc:
                continue
            for p in shop.get("products", []):
                sp = (p.get("species") or "").strip()
                if not sp or search not in normalize_species_name(sp):
                    continue
                found_species.add(sp)
                if not (p.get("in_stock") and p.get("is_active")):
                    continue
                offers.setdefault(sp, []).append({
                    "shop_name":   shop.get("name", "?"),
                    "country":     scountry,
                    "rating":      shop.get("average_rating"),
                    "title":       strip_html(p.get("title") or sp),
                    "description": strip_html(p.get("description") or ""),
                    "min":         p.get("min_price"),
                    "max":         p.get("max_price"),
                    "cur":         p.get("currency_iso") or "EUR",
                    "variants":    p.get("variants") or [],
                    "url":         (p.get("antcheck_url") or p.get("shop_url") or "").strip(),
                    "shop_web":    (shop.get("url") or "").strip(),
                })

        if not found_species:
            await ctx.followup.send(l10n.get("sells_none", lang, query=query))
            return
        if not offers:
            await ctx.followup.send(l10n.get("sells_no_stock", lang, query=query))
            return

        # Angebote je Art aufbauen. Angebote ohne echten Preis (0 €/unbekannt)
        # werden übersprungen; Arten ohne gültiges Angebot ganz weggelassen.
        species_blocks: dict[str, list] = {}
        for sp in sorted(offers.keys()):
            shops_sorted = sorted(
                offers[sp],
                key=lambda o: (o["rating"] is None, -(o["rating"] or 0), o["shop_name"].lower()),
            )
            sp_parts: list[str] = []
            for o in shops_sorted:
                vs = [
                    v for v in o["variants"]
                    if v.get("in_stock") and v.get("is_active") and _has_price(v.get("price"))
                ]
                price_lines: list[str] = []
                if vs:
                    # Varianten-Ebene: pro Variante Einzelpreis
                    for i, v in enumerate(vs, 1):
                        label  = strip_html(v.get("title") or v.get("description") or f"Variante {i}")
                        vprice = _price_md(v.get("price"), v.get("price"), v.get("currency_iso") or o["cur"])
                        price_lines.append(f"{label}: {vprice}")
                elif _has_price(o["min"]) or _has_price(o["max"]):
                    # Fallback: Produkt-Ebene (min/max), falls (noch) keine Varianten
                    price = _price_md(o["min"], o["max"], o["cur"])
                    if o["description"] and len(o["description"]) <= 60 and o["description"].lower() != o["title"].lower():
                        price_lines.append(f"{o['description']}: {price}")
                    else:
                        price_lines.append(price)
                if not price_lines:
                    continue  # 0-€/Preis-unbekannt → Angebot überspringen
                sp_parts.append("")
                sp_parts.append(f"{flag_emoji(o['country'])} **{o['shop_name']}**")
                for w in get_shop_warnings(o.get("shop_web", ""), o["shop_name"]):
                    sp_parts.append(l10n.get(
                        "warn_shop_line", lang,
                        emoji=warn_emoji(w["level"]), level=w["level"], text=w["text"],
                    ))
                if o["title"]:
                    sp_parts.append(o["title"])
                if o.get("url"):
                    sp_parts.append(l10n.get("sells_product_link", lang, url=o["url"]))
                sp_parts.extend(price_lines)
            if sp_parts:
                species_blocks[sp] = sp_parts

        if not species_blocks:
            await ctx.followup.send(l10n.get("sells_no_stock", lang, query=query))
            return

        shown_species = sorted(species_blocks)
        parts: list[str] = []
        if len(found_species) > len(shown_species):
            parts.append(l10n.get(
                "sells_multi_hint", lang,
                query=query, found=len(found_species),
                offered=", ".join(shown_species),
            ))
            parts.append("")
        for sp in shown_species:
            parts.append(f"***{sp}***")
            parts.append(l10n.get("sells_source", lang))
            parts.append(l10n.get("sells_disclaimer", lang))
            parts.extend(species_blocks[sp])
            parts.append("")

        parts.append(l10n.get("sells_footer", lang, ts=_read_fetched_at() or "?"))

        for chunk in chunk_lines("\n".join(parts), 4000):
            await ctx.followup.send(
                embed=discord.Embed(description=chunk, color=EMBED_COLOR)
            )


def setup(bot: discord.Bot):
    bot.add_cog(SellsCog(bot))
