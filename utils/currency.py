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
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_RATES: dict[str, float] = {}   # {ISO-Code → Faktor X→EUR, z.B. "CAD": 0.681}
_LAST_FETCH: datetime | None = None
_CACHE_TTL       = timedelta(hours=6)
# Verhindert doppelte/parallele Abrufe beim Start (mehrere Cogs rufen ensure_rates
# quasi gleichzeitig auf, bevor _LAST_FETCH gesetzt ist -> sonst mehrfaches Laden).
_fetch_lock = asyncio.Lock()
# Primärquelle: EZB-Referenzkurse (Frankfurter), ~31 große Währungen.
_FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=EUR"
# Breite, komplett offene & key-lose Fallback-Quelle (fawazahmed0), deckt 150+
# Währungen inkl. TWD ab, die die EZB nicht führt. Zweite URL = Spiegel.
_FAWAZ_URLS = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/eur.json",
    "https://latest.currency-api.pages.dev/v1/currencies/eur.json",
)


def _needs_refresh() -> bool:
    return _LAST_FETCH is None or datetime.now(timezone.utc) - _LAST_FETCH > _CACHE_TTL


async def ensure_rates() -> None:
    """Lädt Kurse falls Cache abgelaufen oder noch leer ist.

    Mit Lock + Double-Check: rufen mehrere Cogs beim Start gleichzeitig auf, laedt
    nur der erste; die uebrigen warten und nutzen dann den frischen Cache."""
    if not _needs_refresh():
        return
    async with _fetch_lock:
        # Nach dem Warten erneut pruefen – evtl. hat ein anderer Aufruf schon geladen.
        if _needs_refresh():
            await asyncio.to_thread(_fetch_rates_sync)


def _invert_eur_to_x(eur_to_x: dict) -> dict[str, float]:
    """EUR→X-Kurse in X→EUR-Faktoren umrechnen (Codes in Großbuchstaben)."""
    out: dict[str, float] = {}
    for cur, rate in (eur_to_x or {}).items():
        try:
            if rate:
                out[str(cur).upper()] = 1.0 / float(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return out


def _fetch_frankfurter() -> dict[str, float]:
    resp = requests.get(_FRANKFURTER_URL, timeout=10)
    resp.raise_for_status()
    return _invert_eur_to_x(resp.json().get("rates", {}))


def _fetch_fawaz() -> dict[str, float]:
    last_err: Exception | None = None
    for url in _FAWAZ_URLS:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return _invert_eur_to_x(resp.json().get("eur", {}))
        except Exception as e:  # noqa: BLE001 – zur nächsten URL/Quelle weitergehen
            last_err = e
    raise last_err or RuntimeError("fawazahmed0: keine URL erreichbar")


def _fetch_rates_sync() -> None:
    """Synchroner Abruf der Wechselkurse (läuft im ThreadPool).

    Strategie: die breite Quelle (fawazahmed0) als Basis laden, danach die
    EZB-Referenzkurse (Frankfurter) darüberlegen – die haben für ihre Währungen
    Vorrang. Fällt eine Quelle aus, wird die andere genutzt; fallen beide aus,
    bleiben die zuletzt geladenen Kurse unverändert aktiv.
    """
    global _RATES, _LAST_FETCH
    merged: dict[str, float] = {}

    try:
        merged.update(_fetch_fawaz())
    except Exception as e:
        logger.warning("⚠️ fawazahmed0-Kursquelle nicht erreichbar: %s", e)

    try:
        merged.update(_fetch_frankfurter())   # EZB-Kurse haben Vorrang
    except Exception as e:
        logger.warning("⚠️ Frankfurter API nicht erreichbar: %s", e)

    if merged:
        merged["EUR"] = 1.0
        _RATES = merged
        _LAST_FETCH = datetime.now(timezone.utc)
        logger.info("💶 Währungskurse geladen: %d Währungen (EZB + Fallback)", len(_RATES))
    else:
        logger.warning("⚠️ Keine Kursquelle erreichbar – bestehende Kurse bleiben aktiv.")


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
        mn = "?" if min_price is None else min_price
        mx = "?" if max_price is None else max_price
        return f"{mn}-{mx}{currency or 'EUR'}"

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
