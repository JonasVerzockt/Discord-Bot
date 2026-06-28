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
utils/currency.py – Währungsumrechnung via Frankfurter API.

Kostenlos, kein API-Key nötig (https://www.frankfurter.app).
Wechselkurse werden alle 6 Stunden im Speicher gecacht.

Nutzung:
    from utils.currency import ensure_rates, format_price

    await ensure_rates()  # Cache auffrischen falls nötig
    price_str = format_price("34.49", "34.49", "CAD")
    # → "34.49CAD (ca. 23.50€)"
"""
import asyncio
import logging
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

_RATES: dict[str, float] = {}   # {ISO-Code → Faktor X→EUR, z.B. "CAD": 0.681}
_LAST_FETCH: datetime | None = None
_CACHE_TTL  = timedelta(hours=6)
_API_URL    = "https://api.frankfurter.app/latest?from=EUR"


async def ensure_rates() -> None:
    """Lädt Kurse falls Cache abgelaufen oder noch leer ist."""
    global _LAST_FETCH
    if _LAST_FETCH is None or datetime.utcnow() - _LAST_FETCH > _CACHE_TTL:
        await asyncio.to_thread(_fetch_rates_sync)


def _fetch_rates_sync() -> None:
    """Synchroner Abruf der Wechselkurse (läuft in ThreadPool)."""
    global _RATES, _LAST_FETCH
    try:
        resp = requests.get(_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Frankfurter liefert EUR→X, wir brauchen X→EUR
        rates_eur_to_x: dict[str, float] = data.get("rates", {})
        _RATES = {
            cur: 1.0 / rate
            for cur, rate in rates_eur_to_x.items()
            if rate
        }
        _RATES["EUR"] = 1.0
        _LAST_FETCH = datetime.utcnow()
        logger.info("💶 Währungskurse geladen: %d Währungen", len(_RATES))
    except Exception as e:
        logger.warning("⚠️ Frankfurter API nicht erreichbar: %s", e)


def to_eur(amount: float, currency: str) -> float | None:
    """
    Rechnet *amount* in *currency* nach EUR um.

    Returns:
        float  – gerundeter EUR-Betrag
        None   – Kurs unbekannt (Cache leer oder Währung nicht gefunden)
    """
    cur = currency.upper()
    if cur == "EUR":
        return round(amount, 2)
    rate = _RATES.get(cur)
    if rate is None:
        return None
    return round(amount * rate, 2)


def format_price(
    min_price: str | float,
    max_price: str | float,
    currency: str,
) -> str:
    """
    Formatiert eine Preisspanne (min/max) mit optionalem EUR-Hinweis.

    Beispiele:
      EUR (gleich):    "59.99EUR"
      EUR (Spanne):    "10.00-20.00EUR"
      Fremdwährung:    "34.49CAD (ca. 23.50€)"
      Fremdw.-Spanne:  "10.00-20.00CAD (ca. 6.80-13.60€)"
      Kurs unbekannt:  "34.49CAD"
    """
    try:
        min_p = float(min_price or 0)
        max_p = float(max_price or 0)
    except (ValueError, TypeError):
        return f"{min_price}-{max_price}{currency}"

    cur = (currency or "EUR").upper()

    base = f"{min_p:.2f}{cur}" if min_p == max_p else f"{min_p:.2f}-{max_p:.2f}{cur}"

    if cur == "EUR":
        return base

    min_eur = to_eur(min_p, cur)
    if min_eur is None:
        return base   # Kurs nicht verfügbar → Originalwährung ohne Hinweis

    if min_p == max_p:
        eur_hint = f"ca. {min_eur:.2f}€"
    else:
        max_eur = to_eur(max_p, cur)
        if max_eur is None:
            eur_hint = f"ca. {min_eur:.2f}€"
        else:
            eur_hint = f"ca. {min_eur:.2f}-{max_eur:.2f}€"

    return f"{base} ({eur_hint})"
