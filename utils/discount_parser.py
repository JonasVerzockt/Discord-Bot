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
utils/discount_parser.py – Haiku-gestützte Extraktion von Rabattcodes aus
Discord-Nachrichten.

Kein Keyword-Vorfilter: Jede (nicht-leere) Nachricht wird an Haiku geschickt,
das im Zweifel selbst entscheidet, ob ein echter Rabattcode enthalten ist
(kein Code => leeres Array).

Nach der KI läuft eine feste Prüfung (_validate): Werte, die nicht im Text
stehen, werden verworfen (erfundene Prozente, Codes, "dauerhaft"), Bestellfristen
sind kein Enddatum, und "bis Ende November" / "vom 7.10. bis 25.10." werden
deterministisch erkannt.

Verwendung:
    from utils.discount_parser import parse_codes
"""
import os
import re
import json
import logging
import calendar
from datetime import date

import anthropic
from dotenv import load_dotenv

from config import DISCOUNT_PARSER_MODEL
from utils.ai_compat import thinking_off, max_tokens_for, text_of

load_dotenv()

logger = logging.getLogger(__name__)

_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_PROMPT = """\
Du extrahierst Rabattcodes aus einer Discord-Nachricht einer Ameisen-Community.
Die Nachricht kann Text UND/ODER Bilder (Screenshots, Flyer, Shop-Werbung) enthalten.
Erkenne Rabattcodes auch dann, wenn sie NUR im Bild stehen (z.B. auf einem Werbebanner).
Nachrichtendatum (Referenz für relative Datumsangaben): {date}
Autor der Nachricht (Discord-Name, oft der Shop selbst): {author}

Gib ein JSON-Objekt {{"codes": [ ... ]}} zurück. Jedes Array-Element beschreibt
GENAU EINEN Rabattcode:
{{"shop": "Shopname", "shop_url": "URL oder null", "code": "DERCODE",
  "discount": "z.B. 20% oder 10€", "valid_from": "YYYY-MM-DD oder null",
  "valid_until": "YYYY-MM-DD oder null", "permanent": true/false,
  "min_order": "z.B. 25€ oder null"}}

Regeln:
- Nimm NUR Einträge mit einem echten Gutschein-/Rabattcode auf. Rabatte ohne Code ignorieren.
- Mehrere Codes in einer Nachricht => mehrere Array-Elemente.
- Datumsangaben relativ zum Nachrichtendatum aufloesen:
  "nur heute" => valid_until = Nachrichtendatum;
  "bis morgen" => Nachrichtendatum + 1 Tag;
  "nächste Tage" / "diese Woche" / "solange der Vorrat reicht" / "kurzzeitig"
    => valid_until = Nachrichtendatum + 7 Tage;
  Teildatum ohne Jahr (z.B. "bis 14.06.") => Jahr aus dem Nachrichtendatum, sodass das Datum in der Zukunft liegt;
  Zeitraum "vom X bis Y" => valid_from = X, valid_until = Y.
- Saison-/Aktionsrabatte OHNE explizites Enddatum (Black Friday, Cyber Monday,
  Ostern, Weihnachten, Sommer-Sale, Jubiläum o.ä.) => setze ein ungefähres
  Enddatum relativ zum Nachrichtendatum (z.B. Black Friday / Cyber Monday =>
  30.11. bzw. 02.12. des Jahres; Oster-/Weihnachts-/Sommeraktion => Ende des
  jeweiligen Zeitraums). Solche Aktionen sind NIE permanent.
- "dauerhaft", "immer", "Dauerrabattcode" => permanent = true und valid_until = null.
- Enddatum (valid_until) NUR, wenn das Datum ausdrücklich die Gültigkeit des
  Codes/Rabatts beschreibt ("gültig bis", "gilt bis", "läuft bis", "endet",
  "einlösbar bis", "bis einschließlich"). Bestellfristen ("bestellt am besten bis
  15.12.", "damit es vor Weihnachten ankommt"), Versand-, Liefer-, Messe- oder
  Urlaubstermine sind KEIN Enddatum => null.
- "bis Ende <Monat>" => letzter Tag dieses Monats.
- "bis nächsten <Wochentag>" / "bis Sonntag" => das nächste solche Datum nach dem Nachrichtendatum.
- shop: Name des Shops. Steht er nicht im Text, darf er aus dem Autorennamen
  abgeleitet werden (z.B. "krafty_myants.de" => MyAnts). Sonst leerer String.
  NIE Platzhalter wie "?", "Unbekannt", "Shopname".
- shop_url aus im Text genannten Links/Domains übernehmen, falls vorhanden.
- discount NUR, wenn Prozent/Betrag ausdrücklich genannt ist; sonst null. Nichts schätzen.
- permanent NUR bei ausdrücklichen Worten wie "dauerhaft", "immer", "Dauerrabattcode".
- KEINE Rabattcodes sind: Wörter, die man in eine Kundenanmerkung/Bestellnotiz
  schreiben soll, Emojis/Flaggen, Aktionsnamen ohne einzugebenden Code,
  automatische Rabatte ohne Code.
- Unbekanntes Datum => null.
- Code in Originalschreibweise uebernehmen.
- Kein echter Rabattcode in der Nachricht => {{"codes": []}}.

Nachricht:
{message}"""

# JSON-Schema für Structured Outputs: garantiert ein Objekt {"codes":[...]} mit
# vollständigen, typkorrekten Einträgen -> keine Parse-Fehler mehr.
_CODE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "shop":        {"type": "string"},
        "shop_url":    {"type": ["string", "null"]},
        "code":        {"type": "string"},
        "discount":    {"type": ["string", "null"]},
        "valid_from":  {"type": ["string", "null"]},
        "valid_until": {"type": ["string", "null"]},
        "permanent":   {"type": "boolean"},
        "min_order":   {"type": ["string", "null"]},
    },
    "required": ["shop", "shop_url", "code", "discount",
                 "valid_from", "valid_until", "permanent", "min_order"],
    "additionalProperties": False,
}
_DISCOUNT_SCHEMA = {
    "type": "object",
    "properties": {"codes": {"type": "array", "items": _CODE_ITEM_SCHEMA}},
    "required": ["codes"],
    "additionalProperties": False,
}


_MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2,
    "märz": 3, "maerz": 3, "march": 3, "april": 4, "apr": 4, "mai": 5, "may": 5,
    "juni": 6, "june": 6, "juli": 7, "july": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "oktober": 10, "okt": 10, "october": 10,
    "november": 11, "nov": 11, "dezember": 12, "dez": 12, "december": 12,
}
_PERM_RE  = re.compile(
    r"dauerhaft|dauer-?(?:rabatt|code)|\bimmer\b|permanent|unbegrenzt|"
    r"ohne\s+(?:enddatum|ablauf)|always|forever|no\s+expir", re.I)
_ORDER_RE = re.compile(r"bestell|versand|versend|liefer|ankomm|weihnachtsbaum|messe|urlaub|abhol", re.I)
_VALID_RE = re.compile(r"gültig|gueltig|gilt|läuft|laeuft|endet|einlösbar|einloesbar|"
                       r"rabatt|code|aktion|sale|valid|until|ends?\b", re.I)
_RANGE_RE = re.compile(
    r"(?:vom|von|ab)\s*(\d{1,2})\.(\d{1,2})\.?\s*(\d{2,4})?\s*(?:bis|–|-)\s*"
    r"(?:zum\s+|einschließlich\s+)?(\d{1,2})\.(\d{1,2})\.?\s*(\d{2,4}\b)?", re.I)
_END_OF_RE = re.compile(r"(?:bis\s+)?\bende\s+(?:des\s+monats\s+)?([a-zäöü]+)", re.I)
_REGIONAL  = re.compile("[\U0001F1E6-\U0001F1FF]")
_NOTE_RE   = re.compile(r"kundenanmerkung|bestellnotiz|anmerkung|order\s+note|kommentarfeld", re.I)


def _year(y: str | None, ref: date) -> int:
    if not y:
        return ref.year
    y = int(y)
    return y + 2000 if y < 100 else y


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _rule_dates(text: str, ref: date) -> tuple[str | None, str | None]:
    """Feste Regeln für Zeiträume, die die KI oft verpasst.
    Gibt (valid_from, valid_until) als ISO zurück oder (None, None)."""
    m = _RANGE_RE.search(text or "")
    if m:
        d1, m1, y1, d2, m2, y2 = m.groups()
        start = _safe(_year(y1 or y2, ref), int(m1), int(d1))
        end   = _safe(_year(y2 or y1, ref), int(m2), int(d2))
        if start and end:
            if end < start:
                end = _safe(end.year + 1, end.month, end.day)
            return start.isoformat(), end.isoformat() if end else None
    for m in _END_OF_RE.finditer(text or ""):
        mon = _MONTHS.get(m.group(1).lower().rstrip("."))
        if mon:
            y = ref.year + (1 if mon < ref.month else 0)
            return None, date(y, mon, calendar.monthrange(y, mon)[1]).isoformat()
    return None, None


def _only_order_deadline(text: str, iso: str) -> bool:
    """True, wenn das Datum im Text NUR als Bestell-/Versandfrist vorkommt."""
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return False
    pat = re.compile(rf"(?<!\d)0?{d.day}\.\s*0?{d.month}\.?(?!\d)")
    hits = list(pat.finditer(text or ""))
    if not hits:
        return False
    for h in hits:
        before = text[max(0, h.start() - 45):h.start()]
        if not _ORDER_RE.search(before) or _VALID_RE.search(before):
            return False
    return True


def _numbers(s: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", s or "")


def _validate(d: dict, text: str, has_images: bool, ref: date) -> dict | None:
    """Verwirft erfundene Werte. Bei Bildern sind Text-Prüfungen abgeschaltet
    (der Code kann nur im Bild stehen)."""
    low = (text or "").casefold()
    code = d["code"]
    if _REGIONAL.search(code) or not re.search(r"[A-Za-z0-9]", code):
        logger.info("🏷️ Code verworfen (Emoji/kein Text): %r", code)
        return None
    if not has_images:
        pos = low.find(code.casefold())
        if pos < 0:
            logger.info("🏷️ Code verworfen (steht nicht im Text): %r", code)
            return None
        if _NOTE_RE.search(low[max(0, pos - 60):pos]):
            logger.info("🏷️ Code verworfen (nur Kundenanmerkung): %r", code)
            return None
        nums = [n.replace(",", ".") for n in _numbers(d["discount"])]
        flat = (text or "").replace(",", ".")
        if nums and not all(re.search(rf"(?<![\d.]){re.escape(n)}(?!\d)", flat) for n in nums):
            logger.info("🏷️ Rabatt verworfen (nicht im Text): %r bei %r", d["discount"], code)
            d["discount"] = ""
        if d["permanent"] and not _PERM_RE.search(text or ""):
            d["permanent"] = False
    if d["permanent"]:
        d["valid_until"] = None
    vf, vu = _rule_dates(text, ref)
    if vu and not d["permanent"]:
        d["valid_until"] = vu
        d["valid_from"] = vf or d["valid_from"]
    elif d["valid_until"] and _only_order_deadline(text, d["valid_until"]):
        logger.info("🏷️ Enddatum verworfen (nur Bestellfrist): %s bei %r", d["valid_until"], code)
        d["valid_until"] = None
    shop = (d["shop"] or "").strip()
    if shop.casefold() in {"?", "unbekannt", "unknown", "shopname", "shop", "n/a", "-", "none"}:
        d["shop"] = ""
    return d


def _norm_date(v) -> str | None:
    """Akzeptiert nur ISO-Datum YYYY-MM-DD, sonst None."""
    if not v:
        return None
    v = str(v).strip()
    return v if re.match(r"^\d{4}-\d{2}-\d{2}$", v) else None


def parse_codes(
    content: str,
    message_date: str,
    images: list[tuple[bytes, str]] | None = None,
    author: str = "",
) -> list[dict]:
    """
    Schickt Nachrichtentext (und optional Bilder) an Claude Haiku und gibt eine
    Liste von Code-Dicts zurück. Wirft bei ungültigem JSON json.JSONDecodeError.

    images: Liste von (rohe Bytes, media_type) – z.B. (b"...", "image/png").
            Wird per Vision mitgeschickt; erkennt Codes auch nur im Bild.
    """
    prompt_text = _PROMPT.format(date=message_date, author=author or "-", message=content or "")

    if images:
        import base64 as _b64
        user_content: list = []
        for img_bytes, media_type in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": _b64.b64encode(img_bytes).decode(),
                },
            })
        user_content.append({"type": "text", "text": prompt_text})
        api_messages = [{"role": "user", "content": user_content}]
    else:
        api_messages = [{"role": "user", "content": prompt_text}]

    resp = _ai.messages.create(
        model=DISCOUNT_PARSER_MODEL,
        max_tokens=max_tokens_for(DISCOUNT_PARSER_MODEL, 700),
        **thinking_off(DISCOUNT_PARSER_MODEL),   # neuere Modelle denken sonst standardmäßig mit
        messages=api_messages,
        output_config={"format": {"type": "json_schema", "schema": _DISCOUNT_SCHEMA}},
    )
    text = text_of(resp).strip()               # ThinkingBlock (ohne .text) überspringen
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    # Structured Outputs liefert {"codes":[...]}; alte Antworten evtl. noch als Array.
    data = parsed.get("codes", []) if isinstance(parsed, dict) else parsed
    if not isinstance(data, list):
        return []

    try:
        ref = date.fromisoformat(message_date[:10])
    except ValueError:
        ref = date.today()
    out: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        code = (d.get("code") or "").strip()
        if not code:
            continue
        item = {
            "shop":        (d.get("shop") or "").strip(),
            "shop_url":    (d.get("shop_url") or "").strip(),
            "code":        code,
            "discount":    (d.get("discount") or "").strip(),
            "valid_from":  _norm_date(d.get("valid_from")),
            "valid_until": _norm_date(d.get("valid_until")),
            "permanent":   bool(d.get("permanent")),
            "min_order":   (d.get("min_order") or "").strip() or None,
        }
        item = _validate(item, content or "", bool(images), ref)
        if item:
            out.append(item)
    return out
