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
utils/map_tags.py – Fester Tag-Katalog für die Halter-Karte.

Bewusst eine FESTE, vordefinierte Liste (kein Freitext): Datenschutz + Moderation.
Jeder Tag: eindeutiger Code + Label de/en/eo. Gruppiert für die Anzeige.
Verwendet von cogs/map.py (Auswahl) und cogs/board.py (Anzeige/Filter).
"""
from __future__ import annotations

# Gruppen → Tags. Reihenfolge = Anzeigereihenfolge.
TAG_GROUPS: list[dict] = [
    {"code": "exp", "label": {"de": "Erfahrung", "en": "Experience", "eo": "Sperto"},
     "tags": [
        ("beginner",     {"de": "Anfänger",            "en": "Beginner",         "eo": "Komencanto"}),
        ("advanced",     {"de": "Fortgeschritten",     "en": "Advanced",         "eo": "Progresinta"}),
        ("expert",       {"de": "Langjährig/Experte",  "en": "Long-time/Expert", "eo": "Spertulo"}),
     ]},
#    {"code": "trade", "label": {"de": "Angebot & Suche", "en": "Offer & Search", "eo": "Oferto & Serĉo"},
#     "tags": [
#        ("offer_colony", {"de": "Biete Ableger/Kolonien", "en": "Offering colonies", "eo": "Ofertas koloniojn"}),
#        ("seek_colony",  {"de": "Suche Ableger/Kolonien", "en": "Looking for colonies", "eo": "Serĉas koloniojn"}),
#        ("swap",         {"de": "Tausch",                 "en": "Swap",              "eo": "Interŝanĝo"}),
#        ("giveaway",     {"de": "Abzugeben (Auflösung)",  "en": "Giving away",       "eo": "Fordonota"}),
#     ]},
    {"code": "comm", "label": {"de": "Bereitschaft / Community", "en": "Availability / Community", "eo": "Preteco / Komunumo"},
     "tags": [
        ("meetups",      {"de": "Offen für Treffen",   "en": "Open to meetups",    "eo": "Malferma al renkontiĝoj"}),
        ("visits",       {"de": "Besichtigung möglich","en": "Visits possible",    "eo": "Vizitoj eblaj"}),
        ("mentor",       {"de": "Biete Hilfe/Mentoring","en": "Offering mentoring", "eo": "Ofertas mentoradon"}),
        ("seek_mentor",  {"de": "Suche Mentor",        "en": "Looking for a mentor","eo": "Serĉas mentoron"}),
        ("carpool",      {"de": "Fahrgemeinschaft zu Treffen", "en": "Carpool to meetups", "eo": "Kunveturado"}),
     ]},
 #   {"code": "log", "label": {"de": "Logistik", "en": "Logistics", "eo": "Loĝistiko"},
 #    "tags": [
 #       ("ship",         {"de": "Versand möglich",     "en": "Shipping possible",  "eo": "Sendado ebla"}),
 #       ("local",        {"de": "Nur lokal/Abholung",  "en": "Local/pickup only",  "eo": "Nur loke/preni"}),
 #    ]},
    {"code": "focus", "label": {"de": "Schwerpunkt", "en": "Focus", "eo": "Fokuso"},
     "tags": [
        ("native",       {"de": "Heimische Arten",     "en": "Native species",     "eo": "Hejmaj specioj"}),
        ("exotic",       {"de": "Exotische Arten",     "en": "Exotic species",     "eo": "Ekzotaj specioj"}),
        ("leafcutter",   {"de": "Blattschneider",      "en": "Leafcutters",        "eo": "Foliotranĉantoj"}),
        ("honeypot",     {"de": "Honigtopf-Ameisen",   "en": "Honeypot ants",      "eo": "Mielpot-formikoj"}),
        ("breeding",     {"de": "Nachzucht/Zucht",     "en": "Breeding",           "eo": "Bredado"}),
     ]},
    {"code": "diy", "label": {"de": "Technik/DIY", "en": "Tech/DIY", "eo": "Tekniko/DIY"},
     "tags": [
        ("formicaria",   {"de": "Baut Formicarien",    "en": "Builds formicaria",  "eo": "Konstruas formikejojn"}),
        ("print3d",      {"de": "3D-Druck",            "en": "3D printing",        "eo": "3D-presado"}),
        ("ytong",        {"de": "Ytong/DIY",           "en": "Ytong/DIY",          "eo": "Ytong/DIY"}),
        ("macro",        {"de": "Makro-Fotografie",    "en": "Macro photography",  "eo": "Makrofotado"}),
     ]},
]

# Flache Nachschlage-Struktur: code -> {de,en,eo}
_TAG_LABELS: dict[str, dict] = {}
for _g in TAG_GROUPS:
    for _code, _lbl in _g["tags"]:
        _TAG_LABELS[_code] = _lbl


def all_tag_codes() -> list[str]:
    """Alle gültigen Tag-Codes (Reihenfolge wie im Katalog)."""
    return list(_TAG_LABELS.keys())


def is_valid(code: str) -> bool:
    return code in _TAG_LABELS


def label(code: str, lang: str = "de") -> str:
    """Lokalisiertes Label für einen Tag-Code (Fallback de, dann Code)."""
    lbl = _TAG_LABELS.get(code)
    if not lbl:
        return code
    return lbl.get(lang) or lbl.get("de") or code


def labels(codes, lang: str = "de") -> list[str]:
    return [label(c, lang) for c in codes if is_valid(c)]
