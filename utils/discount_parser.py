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

Verwendung:
    from utils.discount_parser import parse_codes
"""
import os
import re
import json
import logging

import anthropic
from dotenv import load_dotenv

from config import DISCOUNT_PARSER_MODEL

load_dotenv()

logger = logging.getLogger(__name__)

_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_PROMPT = """\
Du extrahierst Rabattcodes aus einer Discord-Nachricht einer Ameisen-Community.
Nachrichtendatum (Referenz für relative Datumsangaben): {date}

Gib AUSSCHLIESSLICH ein JSON-Array zurück (kein weiterer Text, kein Markdown).
Jedes Element beschreibt GENAU EINEN Rabattcode:
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
- shop_url aus im Text genannten Links/Domains übernehmen, falls vorhanden.
- discount möglichst angeben (z.B. "20%", "10€"); nur null, wenn wirklich nicht erkennbar.
- Unbekanntes Datum => null.
- Code in Originalschreibweise uebernehmen.
- Kein echter Rabattcode in der Nachricht => leeres Array [].

Nachricht:
{message}"""


def _norm_date(v) -> str | None:
    """Akzeptiert nur ISO-Datum YYYY-MM-DD, sonst None."""
    if not v:
        return None
    v = str(v).strip()
    return v if re.match(r"^\d{4}-\d{2}-\d{2}$", v) else None


def parse_codes(content: str, message_date: str) -> list[dict]:
    """
    Schickt den Nachrichtentext an Claude Haiku und gibt eine Liste von
    Code-Dicts zurück. Wirft bei ungültigem JSON json.JSONDecodeError.
    """
    resp = _ai.messages.create(
        model=DISCOUNT_PARSER_MODEL,
        max_tokens=700,
        messages=[{"role": "user", "content": _PROMPT.format(
            date=message_date, message=content,
        )}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, list):
        return []

    out: list[dict] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        code = (d.get("code") or "").strip()
        if not code:
            continue
        out.append({
            "shop":        (d.get("shop") or "").strip(),
            "shop_url":    (d.get("shop_url") or "").strip(),
            "code":        code,
            "discount":    (d.get("discount") or "").strip(),
            "valid_from":  _norm_date(d.get("valid_from")),
            "valid_until": _norm_date(d.get("valid_until")),
            "permanent":   bool(d.get("permanent")),
            "min_order":   (d.get("min_order") or "").strip() or None,
        })
    return out
