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
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": _PROMPT.format(
            shop_name=shop, date=date, message=content
        )}],
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
