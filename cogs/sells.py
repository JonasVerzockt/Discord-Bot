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

Vorbild: der !sells-Befehl des Antony-Bots. Datenquelle ist – wie beim
restlichen Preis-Tracking – shops_data.json (vom Grabber aus antcheck.info).
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
from utils.availability import load_shop_data, normalize_species_name
from utils.currency import ensure_rates, to_eur
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
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                return dt.strftime("%d.%m.%Y %H:%M UTC")
            except ValueError:
                return str(ts)
    except Exception:
        pass
    return None


def _fnum(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


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
                    "title":       (p.get("title") or sp).strip(),
                    "description": (p.get("description") or "").strip(),
                    "min":         p.get("min_price"),
                    "max":         p.get("max_price"),
                    "cur":         p.get("currency_iso") or "EUR",
                })

        if not found_species:
            await ctx.followup.send(l10n.get("sells_none", lang, query=query))
            return
        if not offers:
            await ctx.followup.send(l10n.get("sells_no_stock", lang, query=query))
            return

        parts: list[str] = []
        offered_species = sorted(offers.keys())
        if len(found_species) > len(offered_species):
            parts.append(l10n.get(
                "sells_multi_hint", lang,
                query=query, found=len(found_species),
                offered=", ".join(offered_species),
            ))
            parts.append("")

        for sp in offered_species:
            parts.append(f"***{sp}***")
            parts.append(l10n.get("sells_source", lang))
            parts.append(l10n.get("sells_disclaimer", lang))
            shops_sorted = sorted(
                offers[sp],
                key=lambda o: (o["rating"] is None, -(o["rating"] or 0), o["shop_name"].lower()),
            )
            for o in shops_sorted:
                parts.append("")
                parts.append(f"{flag_emoji(o['country'])} **{o['shop_name']}**")
                if o["title"]:
                    parts.append(o["title"])
                price = _price_md(o["min"], o["max"], o["cur"])
                if o["description"] and o["description"].lower() != o["title"].lower():
                    parts.append(f"{o['description']}: {price}")
                else:
                    parts.append(price)
            parts.append("")

        parts.append(l10n.get("sells_footer", lang, ts=_read_fetched_at() or "?"))

        for chunk in _chunks("\n".join(parts)):
            await ctx.followup.send(chunk)


def setup(bot: discord.Bot):
    bot.add_cog(SellsCog(bot))
