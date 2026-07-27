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
import re
from collections import Counter
from datetime import datetime

import discord
from discord.ext import commands

from config import SHOPS_DATA_FILE
from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data, normalize_species_name, strip_html, format_rating, is_live_ant_species, matches_species_query
from utils.currency import ensure_rates, to_eur
from utils.timez import berlin_from_iso
from utils.text_chunks import chunk_paragraphs
from utils.embeds import EMBED_COLOR
from utils.sheet import get_shop_warnings, warn_emoji
from utils.countries import flag_emoji, country_name, country_label
from utils import species_catalog
from cogs.server_settings import allowed_channel

logger = logging.getLogger(__name__)


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


def _canon_species(sp: str) -> str:
    """Gruppierungs-Schlüssel: Whitespace normalisiert + case-insensitiv, damit
    „Lasius niger", „lasius niger" und „Lasius Niger" zu EINER Gruppe werden."""
    return re.sub(r"\s+", " ", (sp or "").strip()).casefold()


def _binomial_display(normalized: str) -> str:
    """Sauberer Binomial-Anzeigename aus dem normalisierten Suchbegriff:
    Gattung groß, Epitheton(e) klein – z.B. „camponotus nicobarensis" → „Camponotus
    nicobarensis". Wird als Sammel-Überschrift genutzt, wenn die Suche ein Binomen
    ist und alle Treffer unter dieser einen Art gebündelt werden."""
    parts = normalized.split()
    if not parts:
        return normalized
    return " ".join([parts[0].capitalize()] + [p.lower() for p in parts[1:]])


def _pick_display(names: Counter) -> str:
    """Wählt aus mehreren Schreibweisen desselben Artnamens den Anzeigenamen:
    bevorzugt eine saubere Binomial-Schreibweise (Erstes Wort groß, Rest klein),
    danach die häufigste Schreibweise."""
    def score(name: str):
        proper = bool(name) and name[0].isupper() and name[1:] == name[1:].lower()
        return (proper, names[name])
    return max(names, key=score)


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
        force: discord.Option(  # type: ignore[valid-type]
            bool,
            "Skip name validation (allow unknown / variant spellings)",
            description_localizations={"de": "Namensprüfung überspringen (unbekannte/abweichende Schreibweisen zulassen)"},
            required=False,
            default=False,
        ),
    ):
        await ctx.defer()
        lang   = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        query  = species.strip()
        search = normalize_species_name(query)
        cc     = (country or "").strip().lower() or None

        # Namensprüfung gegen die Artenliste (außer bei force): bei Synonym/
        # Tippfehler/unbekannt Hinweis + korrekte Schreibweise, dann abbrechen.
        if not force:
            _msg = species_catalog.validation_message(species_catalog.check(query), lang)
            if _msg:
                await ctx.followup.send(_msg, ephemeral=True)
                return

        await ensure_rates()
        shop_data = await load_shop_data(self.bot)

        def _collect(match_fn, collapse_key: str | None = None, collapse_disp: str | None = None):
            """Sammelt Arten + Angebote aus allen (Länder-gefilterten) Shops für
            Produkte, deren species-Feld match_fn erfüllt.

            Gruppiert case-insensitiv nach kanonischem Artnamen (siehe
            _canon_species), damit reine Schreibweise-Varianten keine getrennten
            Gruppen erzeugen. Ist collapse_key gesetzt (Binomen-Suche), werden ALLE
            Treffer unter genau diesem Schlüssel/Anzeigenamen zusammengefasst –
            sicher, weil match_fn den Suchbegriff bereits als Anker bestätigt hat.
            Rückgabe: (keys, offers_by_key, display_by_key).
            """
            fs: set[str] = set()
            off: dict[str, list] = {}
            names: dict[str, Counter] = {}
            for shop in shop_data.values():
                scountry = (shop.get("country") or "").strip().lower()
                if cc and scountry != cc:
                    continue
                for p in shop.get("products", []):
                    sp = (p.get("species") or "").strip()
                    if not sp:
                        continue
                    # Der GBIF-kanonische Artname (vom Grabber gesetzt) wird sowohl
                    # beim Matching ALS AUCH beim Gruppieren berücksichtigt: so findet
                    # eine Suche nach dem akzeptierten Namen auch synonym benannte
                    # Angebote, und diese landen shopübergreifend in EINER Gruppe.
                    canon = (p.get("canonical_species") or "").strip()
                    if not (match_fn(sp) or (canon and match_fn(canon))):
                        continue
                    if collapse_key is not None:
                        # Binomen-Suche: alle Treffer unter EINER Art bündeln.
                        key, disp = collapse_key, (collapse_disp or collapse_key)
                    elif canon:
                        disp = canon
                        key = _canon_species(canon)
                    else:
                        # Fallback: dekodiertes Rohfeld (Entities raus, z.B. „&#8211;" -> „–").
                        disp = strip_html(sp)
                        key = _canon_species(disp)
                    fs.add(key)
                    names.setdefault(key, Counter())[disp] += 1
                    if not (p.get("in_stock") and p.get("is_active")):
                        continue
                    off.setdefault(key, []).append({
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
            display = {k: _pick_display(c) for k, c in names.items()}
            return fs, off, display

        # 1) Primär: exakter/Gattungs-Anker-Match wie bei den Notifications –
        #    schließt Merch/Präparate zuverlässig aus (kein Keyword-Blacklisting).
        #    Ist die Suche ein Binomen (Gattung+Art), werden alle Treffer unter
        #    dieser einen Art gebündelt (Varianten/Bundles als Unterpunkte) –
        #    sonst wie bisher pro (dekodiertem) Artnamen gruppiert.
        if " " in search:
            found_species, offers, display = _collect(
                lambda sp: matches_species_query(sp, query),
                collapse_key=search, collapse_disp=_binomial_display(search),
            )
        else:
            found_species, offers, display = _collect(lambda sp: matches_species_query(sp, query))
        # 2) Fallback nur, wenn exakt nichts gefunden wurde: Teilsuche (z.B. reines
        #    Epitheton „aethiops"), strukturell auf saubere Binomen begrenzt, damit
        #    weiterhin kein Merch durchrutscht.
        if not found_species:
            found_species, offers, display = _collect(
                lambda sp: is_live_ant_species(sp) and search in normalize_species_name(sp)
            )

        if not found_species:
            await ctx.followup.send(l10n.get("sells_none", lang, query=query))
            return
        if not offers:
            await ctx.followup.send(l10n.get("sells_no_stock", lang, query=query))
            return

        # Baut die Zeilen EINES Angebots (Shop-Header + Warnungen + Titel + Link +
        # Preiszeilen). Leere Liste = kein echter Preis (Angebot überspringen).
        def _offer_block(o) -> list[str]:
            vs = [
                v for v in o["variants"]
                if v.get("in_stock") and v.get("is_active") and _has_price(v.get("price"))
            ]
            price_lines: list[str] = []
            if vs:
                for i, v in enumerate(vs, 1):
                    label  = strip_html(v.get("title") or v.get("description") or f"Variante {i}")
                    vprice = _price_md(v.get("price"), v.get("price"), v.get("currency_iso") or o["cur"])
                    price_lines.append(f"{label}: {vprice}")
            elif _has_price(o["min"]) or _has_price(o["max"]):
                price = _price_md(o["min"], o["max"], o["cur"])
                if o["description"] and len(o["description"]) <= 60 and o["description"].lower() != o["title"].lower():
                    price_lines.append(f"{o['description']}: {price}")
                else:
                    price_lines.append(price)
            if not price_lines:
                return []
            lines = ["", f"{flag_emoji(o['country'])} **{o['shop_name']}**"
                     + (f" · {format_rating(o['rating'])}" if o["rating"] is not None else "")]
            for w in get_shop_warnings(o.get("shop_web", ""), o["shop_name"]):
                lines.append(l10n.get(
                    "warn_shop_line", lang,
                    emoji=warn_emoji(w["level"]), level=w["level"], text=w["text"],
                ))
            if o["title"]:
                lines.append(o["title"])
            if o.get("url"):
                lines.append(l10n.get("sells_product_link", lang, url=o["url"]))
            lines.extend(price_lines)
            return lines

        _rating_key = lambda o: (o["rating"] is None, -(o["rating"] or 0), o["shop_name"].lower())

        # Angebote je Art aufbauen. Ohne Länderfilter werden die Angebote je Art
        # zusätzlich nach Shop-Region (Land) gruppiert – mit Regions-Unterüberschrift.
        species_blocks: dict[str, list] = {}
        for sp in sorted(offers.keys()):
            if cc is None:
                by_country: dict[str, list] = {}
                for o in offers[sp]:
                    by_country.setdefault(o["country"], []).append(o)
                # Länder nach lokalisiertem Namen sortieren, Unbekannt (leer) ans Ende.
                groups = [
                    (c, by_country[c])
                    for c in sorted(by_country, key=lambda c: (c == "", country_name(c, lang).lower()))
                ]
            else:
                groups = [(None, offers[sp])]

            sp_parts: list[str] = []
            for country_code, group_offers in groups:
                block: list[str] = []
                for o in sorted(group_offers, key=_rating_key):
                    block.extend(_offer_block(o))
                if not block:
                    continue
                if country_code is not None:
                    # Regions-Unterüberschrift (unterstrichen, klar von Shop-Namen abgesetzt).
                    sp_parts.append("")
                    sp_parts.append(f"__{country_label(country_code, lang)}__")
                sp_parts.extend(block)
            if sp_parts:
                species_blocks[sp] = sp_parts

        if not species_blocks:
            await ctx.followup.send(l10n.get("sells_no_stock", lang, query=query))
            return

        shown_species = sorted(species_blocks, key=lambda k: display.get(k, k).lower())
        parts: list[str] = []
        if len(found_species) > len(shown_species):
            parts.append(l10n.get(
                "sells_multi_hint", lang,
                query=query, found=len(found_species),
                offered=", ".join(display.get(k, k) for k in shown_species),
            ))
            parts.append("")
        for sp in shown_species:
            parts.append(f"***{display.get(sp, sp)}***")
            parts.append(l10n.get("sells_source", lang))
            parts.append(l10n.get("sells_disclaimer", lang))
            parts.extend(species_blocks[sp])
            parts.append("")

        parts.append(l10n.get("sells_footer", lang, ts=_read_fetched_at() or "?"))

        for chunk in chunk_paragraphs("\n".join(parts), 4000):
            await ctx.followup.send(
                embed=discord.Embed(description=chunk, color=EMBED_COLOR)
            )


def setup(bot: discord.Bot):
    bot.add_cog(SellsCog(bot))
