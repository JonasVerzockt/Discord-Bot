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
utils/ai_parser.py – KI-gestützte Extraktion von Review-Daten.

Nutzt claude-haiku für strukturierte JSON-Extraktion aus Discord-Bewertungen.

Verwendung:
    from utils.ai_parser import parse_with_ai, looks_like_review, build_row
"""
import os
import re
import json

import anthropic
from dotenv import load_dotenv

from config import REVIEW_PARSER_MODEL

load_dotenv()

_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_PROMPT = """\
Extrahiere aus dieser Ameisen-Shop-Bewertung die Felder als JSON. Nur JSON, kein Text.
Shop: {shop_name} | Datum: {date}
{message}
Felder:
datum→"{date}" | benutzername→"anonym" | shop_name→bereinigt
shop_typ→ameisenshop|terraristikshop|pflanzenshop|futtershop|aquaristikshop
produkte→semikolon-sep.
geld_ausgegeben→Produktpreis ohne €/Versand als Zahl, null wenn unklar
bewertung→0-10 (X/10=X, X/5=X×2, cap 10), null wenn unklar
positiv→positive Aspekte semikolon-sep., ""=keine
negativ→Kritikpunkte semikolon-sep., ""=keine"""

# JSON-Schema für Structured Outputs: garantiert valides, vollständiges JSON
# (alle Felder present, korrekte Typen) -> keine Parse-Fehler/Retries mehr.
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "datum":           {"type": "string"},
        "benutzername":    {"type": "string"},
        "shop_name":       {"type": "string"},
        "shop_typ":        {"type": "string"},
        "produkte":        {"type": "string"},
        "geld_ausgegeben": {"type": ["number", "null"]},
        "bewertung":       {"type": ["number", "null"]},
        "positiv":         {"type": "string"},
        "negativ":         {"type": "string"},
    },
    "required": ["datum", "benutzername", "shop_name", "shop_typ", "produkte",
                 "geld_ausgegeben", "bewertung", "positiv", "negativ"],
    "additionalProperties": False,
}


def looks_like_review(content: str) -> bool:
    """Schnell-Check: Sieht diese Nachricht wie eine Bewertung aus?"""
    return "🛒" in content or (
        "Shop:" in content and ("Fazit" in content or "/10" in content or "/5" in content)
    )


def parse_with_ai(content: str, shop: str, date: str) -> dict:
    """
    Sendet Nachrichtentext an Claude Haiku und gibt das extrahierte JSON zurück.
    Wirft json.JSONDecodeError wenn die KI kein valides JSON liefert.
    """
    resp = _ai.messages.create(
        model=REVIEW_PARSER_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": _PROMPT.format(
            shop_name=shop, date=date, message=content
        )}],
        output_config={"format": {"type": "json_schema", "schema": _REVIEW_SCHEMA}},
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def build_row(parsed: dict) -> list:
    """Wandelt das geparste Dict in eine Sheet-Zeile (Spalten A–I) um."""
    return [
        parsed.get("datum", ""),
        parsed.get("benutzername", "anonym"),
        parsed.get("shop_name", ""),
        parsed.get("shop_typ", ""),
        parsed.get("produkte", ""),
        parsed.get("geld_ausgegeben") or "",
        parsed.get("bewertung") if parsed.get("bewertung") is not None else "",
        parsed.get("positiv", ""),
        parsed.get("negativ", ""),
    ]
