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
utils/sheets_shop_data.py – Laedt Shop-Bewertungsdaten aus Google Sheets
fuer den AI-Chat-Bot.

Zwei Tabs werden eingelesen und tab-spezifisch geparst:
  - "Haendler A-Z": Kompakte Shopliste mit Bewertung + Anzahl
  - "Uebersicht":   Warnhinweise, neue Shops, nicht bewertet seit >1 Jahr

Zugriff: Nutzt denselben _gc (Service Account) und SPREADSHEET_ID wie
der Review-Bot – keine extra Konfiguration noetig.
"""

import logging
from typing import Optional

import config as cfg

logger = logging.getLogger(__name__)

# Nur diese Tabs werden eingelesen
_ALLOWED_TABS = {"Übersicht", "Händler A-Z"}

# Gecachter Shop-Daten-Block (wird vom Cog aktualisiert)
_cached_block: Optional[str] = None


# ── Tab-spezifische Parser ────────────────────────────────────────────────────

def _parse_haendler_az(rows: list[list[str]]) -> str:
    """
    Parst den Tab 'Haendler A-Z'.
    Erwartet: Kopfzeile + Datenzeilen (Shop, Anzahl, Durchschnitt, ...).
    Leere Zeilen (kein Shop-Name in Spalte A) werden uebersprungen.
    Interne Hilfsspalten (z.B. 'Hilfsspalte') werden ausgeblendet.
    """
    _SKIP_COLS = {"hilfsspalte", "helper", "intern"}
    headers = [h.strip() for h in rows[0]]
    lines: list[str] = []

    for row in rows[1:]:
        if not row[0].strip():
            continue  # Leere Zeile (z.B. nur Hilfsspalte mit FALSE)
        parts = [
            f"{h}: {v.strip()}"
            for h, v in zip(headers, row)
            if h and v.strip() and h.strip().lower() not in _SKIP_COLS
        ]
        if parts:
            lines.append(" | ".join(parts))

    return "[Haendler A-Z – alle Shops mit Community-Bewertung]\n" + "\n".join(lines)


def _parse_uebersicht(rows: list[list[str]]) -> str:
    """
    Parst den Tab 'Uebersicht' mit seinem komplexen Multi-Tabellen-Layout.
    Extrahiert gezielt:
      1. Warnhinweise der Community (Level 1-3)
      2. Neu bewertete Shops
      3. Shops die laenger nicht bewertet wurden (>1 Jahr)
    """
    WARNING_LEVELS = {
        "1": "⚠️ Erhoehte Wachsamkeit",
        "2": "🚨 Akute Warnung / Hohes Risiko",
        "3": "🔴 Bestaedigter Scam / Akute Gefahr",
    }

    warnings:     list[str] = []
    new_shops:    list[str] = []
    old_shops:    list[str] = []

    for row in rows:
        # Zeile hat mindestens 12 Spalten (Uebersicht-Layout)
        padded = row + [""] * 12

        col_a  = padded[0].strip()
        col_b  = padded[1].strip()
        col_c  = padded[2].strip()
        col_d  = padded[3].strip()
        col_g  = padded[6].strip()   # Neue Shops: Datum
        col_h  = padded[7].strip()   # Neue Shops: Shop
        col_j  = padded[9].strip()   # Laenger nicht bewertet: Datum
        col_k  = padded[10].strip()  # Laenger nicht bewertet: Shop

        # Warnhinweise: Spalte A = Level (1/2/3), B = Beschreibung, C = Shop, D = Datum
        if col_a in WARNING_LEVELS and col_c:
            entry = f"{WARNING_LEVELS[col_a]}: {col_c}"
            if col_b:
                entry += f" – {col_b}"
            if col_d:
                entry += f" (seit {col_d})"
            warnings.append(entry)

        # Neue Shops (Spalte G = Datum, H = Shop)
        if col_g and col_h and col_h.lower() not in ("shop", ""):
            new_shops.append(f"{col_h} (seit {col_g})")

        # Laenger nicht bewertet (Spalte J = Datum, K = Shop)
        if col_j and col_k and col_k.lower() not in ("shop", ""):
            old_shops.append(f"{col_k} (letzte Bewertung: {col_j})")

    parts: list[str] = ["[Uebersicht]"]

    if warnings:
        parts.append("COMMUNITY-WARNHINWEISE (unbedingt beachten!):\n" +
                     "\n".join(f"  {w}" for w in warnings))

    if new_shops:
        parts.append("Neu bewertete Shops:\n" +
                     "\n".join(f"  {s}" for s in new_shops))

    if old_shops:
        parts.append("Laenger nicht bewertet (>1 Jahr):\n" +
                     "\n".join(f"  {s}" for s in old_shops))

    return "\n\n".join(parts)


# ── Haupt-Ladefunktion ────────────────────────────────────────────────────────

def load_shop_data() -> Optional[str]:
    """
    Laedt die konfigurierten Tabs aus dem Google Sheet und gibt einen
    formatierten Textblock zurueck, der in den System-Prompt eingebettet wird.
    """
    if not cfg.SPREADSHEET_ID:
        logger.debug("[ShopData] GOOGLE_SPREADSHEET_ID nicht gesetzt – uebersprungen")
        return None

    try:
        from utils.sheet import _gc
        sh = _gc.open_by_key(cfg.SPREADSHEET_ID)
    except Exception as e:
        logger.error(f"[ShopData] Google Sheets Verbindungsfehler: {e}")
        return None

    sections: list[str] = []

    for ws in sh.worksheets():
        if ws.title not in _ALLOWED_TABS:
            logger.debug(f"[ShopData] Tab '{ws.title}' uebersprungen")
            continue
        try:
            rows = ws.get_all_values()
        except Exception as e:
            logger.warning(f"[ShopData] Tab '{ws.title}' Lesefehler: {e}")
            continue

        if not rows or len(rows) < 2:
            continue

        try:
            if ws.title == "Händler A-Z":
                section = _parse_haendler_az(rows)
            elif ws.title == "Übersicht":
                section = _parse_uebersicht(rows)
            else:
                continue

            if section:
                sections.append(section)
                logger.debug(f"[ShopData] Tab '{ws.title}' geparst: {len(section)} Zeichen")

        except Exception as e:
            logger.warning(f"[ShopData] Tab '{ws.title}' Parse-Fehler: {e}")

    if not sections:
        logger.info("[ShopData] Sheet geladen, aber keine Daten gefunden")
        return None

    block = (
        "### Shop-Bewertungsdaten (AAM Community, automatisch geladen)\n\n"
        + "\n\n".join(sections)
    )
    logger.info(
        f"[ShopData] Geladen: {len(sections)} Tab(s), {len(block)} Zeichen "
        f"(~{len(block)//3.5:.0f} Tokens)"
    )
    return block


def get_cached_block() -> Optional[str]:
    """Gibt den zuletzt geladenen Shop-Daten-Block zurueck."""
    return _cached_block


def refresh() -> bool:
    """
    Laedt die Shop-Daten neu und aktualisiert den Cache.
    Returns True bei Erfolg, False bei Fehler.
    """
    global _cached_block
    data = load_shop_data()
    if data is not None:
        _cached_block = data
        return True
    return False
