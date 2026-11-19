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
utils/ai_tools.py – Werkzeuge (Tool Use) für den KI-Chat: echte Daten statt Raten.

Gleiche Datenbasis wie /art, /shop, /codes und die Board-Seiten (utils/directory).
Jedes Werkzeug liefert kompaktes JSON (gekürzt), damit die Antwort günstig bleibt.
Warnhinweise der Community gibt es hier bewusst NICHT (nur /shop auf dem AAM-Server
bzw. Board nach Login).
"""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote

from config import BOARD_PUBLIC_URL
from utils import directory as D
from utils.currency import ensure_rates

logger = logging.getLogger(__name__)

_MAX_JSON = 3500

TOOLS = [
    {
        "name": "art_suchen",
        "description": "Findet Ameisenarten im aktuellen Shop-Angebot nach (Teil-)Namen und löst "
                       "Synonyme auf den gültigen Namen (AntCat) auf. Zuerst nutzen, wenn der Name "
                       "unsicher ist.",
        "input_schema": {"type": "object", "properties": {
            "suchbegriff": {"type": "string", "description": "z. B. 'Lasius niger' oder 'campo nico'"}},
            "required": ["suchbegriff"]},
    },
    {
        "name": "angebote",
        "description": "Aktuelle Angebote einer Art bei allen erfassten Shops mit Preis (in EUR, "
                       "Fremdwährung zusätzlich), Bestand und Link, günstigste zuerst.",
        "input_schema": {"type": "object", "properties": {
            "art": {"type": "string", "description": "Wissenschaftlicher Artname"},
            "nur_lagernd": {"type": "boolean", "description": "Nur lagernde Angebote (Standard: ja)"}},
            "required": ["art"]},
    },
    {
        "name": "preisverlauf",
        "description": "Günstigster Preis einer Art heute, vor 3 und vor 12 Monaten (EUR).",
        "input_schema": {"type": "object", "properties": {"art": {"type": "string"}},
                         "required": ["art"]},
    },
    {
        "name": "shop_info",
        "description": "Infos zu einem Shop: Community-Bewertung (Schnitt, Anzahl, letzte), Lieferzeit, "
                       "Sortiment, lagernde Angebote, Arten nur dort, Preisniveau, Partner-Status.",
        "input_schema": {"type": "object", "properties": {
            "shop": {"type": "string", "description": "Shopname oder Domain, z. B. 'Antstore' oder 'antstore.net'"}},
            "required": ["shop"]},
    },
    {
        "name": "rabattcodes",
        "description": "Aktuell gültige Rabattcodes eines Shops (aus dem Rabattcode-Kanal der Community).",
        "input_schema": {"type": "object", "properties": {"shop": {"type": "string"}},
                         "required": ["shop"]},
    },
]

SYSTEM_HINT = (
    "Du hast Werkzeuge mit den aktuellen Daten der Community (Angebote, Preise, Preisverlauf, "
    "Shop-Bewertungen, Lieferzeiten, Rabattcodes). Nutze sie bei allen Fragen zu Arten, Preisen, "
    "Verfügbarkeit, Shops und Codes, statt zu raten. Nenne nur Zahlen, die ein Werkzeug geliefert "
    "hat, und verlinke die mitgelieferten Board-Seiten. Findet ein Werkzeug nichts, sag das offen."
)


class _LoopShim:
    """execute_db erwartet ein Objekt mit .loop (sonst der Bot)."""
    def __init__(self):
        self.loop = asyncio.get_running_loop()


def _board(path: str) -> str:
    return f"{BOARD_PUBLIC_URL}{path}" if BOARD_PUBLIC_URL else ""


def _dump(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= _MAX_JSON else s[:_MAX_JSON] + " …(gekürzt)"


async def _art(name: str):
    sp = await asyncio.to_thread(D.find_species, name)
    if sp:
        return sp
    hits = await asyncio.to_thread(D.search_species, name, 1)
    return await asyncio.to_thread(D.find_species, hits[0]) if hits else None


async def run_tool(name: str, args: dict) -> str:
    await ensure_rates()
    try:
        if name == "art_suchen":
            q = str(args.get("suchbegriff", ""))[:80]
            canon = await asyncio.to_thread(D.canonical_name, q)
            hits = await asyncio.to_thread(D.search_species, q, 10)
            return _dump({"gueltiger_name": canon, "im_angebot": hits})

        if name in ("angebote", "preisverlauf"):
            sp = await _art(str(args.get("art", ""))[:80])
            if not sp:
                canon = await asyncio.to_thread(D.canonical_name, str(args.get("art", "")))
                return _dump({"fehler": "Art wird derzeit in keinem erfassten Shop angeboten",
                              "gueltiger_name": canon})
            link = _board(f"/art/{quote(D.slug(sp['name']))}")
            if name == "preisverlauf":
                t = await asyncio.to_thread(D.price_trend, sp, 12)
                return _dump({"art": sp["name"], "guenstigster_heute_eur": t["now"],
                              "vor_3_monaten_eur": t["m3"], "vor_12_monaten_eur": t["m12"],
                              "board": link})
            only = args.get("nur_lagernd", True) is not False
            offers = sorted(sp["offers"], key=lambda o: (not o["in_stock"], o["price"] is None, o["price"] or 0))
            if only:
                offers = [o for o in offers if o["in_stock"]]
            return _dump({"art": sp["name"], "shops_gesamt": len(sp["shops"]), "board": link,
                          "angebote": [{"shop": o["shop"], "preis": D.fmt_price(o),
                                        "lagernd": o["in_stock"], "link": o["url"]} for o in offers[:12]]})

        if name in ("shop_info", "rabattcodes"):
            reviews = await asyncio.to_thread(D.loaded_reviews)
            sh = await asyncio.to_thread(D.find_shop, str(args.get("shop", ""))[:80], reviews)
            if not sh:
                hits = await asyncio.to_thread(D.search_shops, str(args.get("shop", "")), reviews, 5)
                return _dump({"fehler": "Shop nicht gefunden", "aehnlich": [n for n, _ in hits]})
            dom = sh["domain"]
            bot = _LoopShim()
            if name == "rabattcodes":
                codes = await D.active_codes(bot, dom)
                return _dump({"shop": sh["name"], "codes": [
                    {"code": c["code"], "rabatt": c["discount"] or None, "ab": c["min_order"],
                     "gueltig": "dauerhaft" if c["permanent"] else (c["until"] or "ohne Enddatum")}
                    for c in codes]})
            rv = reviews.get(dom) or {}
            dl = await D.delivery_of(bot, dom)
            return _dump({
                "shop": sh["name"], "domain": dom, "land": sh.get("country"),
                "bewertung_schnitt_von_10": rv.get("avg"), "bewertungen": rv.get("count", 0),
                "letzte_bewertung": rv.get("last"),
                "lieferzeit_tage_schnitt": dl["avg"] if dl else None,
                "arten": sh["species_count"], "angebote": sh["offers_count"],
                "lagernd": sh["instock_count"], "arten_nur_hier": sh["exclusive"][:10],
                "preisniveau_prozent_vs_schnitt": sh["price_level"],
                "partner": bool(D.partner_of(dom)), "board": _board(f"/shop/{quote(dom)}"),
            })
    except Exception as e:
        logger.warning("[AI-Tools] %s fehlgeschlagen: %s", name, e)
        return _dump({"fehler": "Daten gerade nicht verfügbar"})
    return _dump({"fehler": f"unbekanntes Werkzeug {name}"})
