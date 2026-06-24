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

Alle Tabs des konfigurierten Sheets werden eingelesen und als kompakter
Plaintext-Block aufbereitet, der in den System-Prompt injiziert wird.

Zugriff: Google Service Account (JSON-Key-Datei, Pfad in .env)
"""

import logging
from typing import Optional

import config as cfg

logger = logging.getLogger(__name__)

# Nur diese Tabs werden eingelesen
_ALLOWED_TABS = {"Übersicht", "Händler A-Z"}

# Gecachter Shop-Daten-Block (wird vom Cog aktualisiert)
_cached_block: Optional[str] = None


def load_shop_data() -> Optional[str]:
    """
    Laedt die konfigurierten Tabs aus dem Google Sheet und gibt einen
    formatierten Textblock zurueck, der in den System-Prompt eingebettet wird.

    Nutzt denselben Service Account (_gc) und dieselbe Spreadsheet-ID
    (SPREADSHEET_ID) wie der Review-Bot – keine doppelte Konfiguration noetig.

    Returns:
        Formatierter String oder None bei Fehler / fehlender Konfiguration.
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
            logger.debug(f"[ShopData] Tab '{ws.title}' uebersprungen (nicht in _ALLOWED_TABS)")
            continue
        try:
            rows = ws.get_all_values()
        except Exception as e:
            logger.warning(f"[ShopData] Tab '{ws.title}' konnte nicht gelesen werden: {e}")
            continue

        if not rows or len(rows) < 2:
            continue  # Leerer Tab oder nur Kopfzeile

        headers = [h.strip() for h in rows[0]]
        lines: list[str] = []

        for row in rows[1:]:
            # Leere Zeilen ueberspringen
            if not any(cell.strip() for cell in row):
                continue
            # Nur Zellen mit Inhalt ausgeben: "Spalte: Wert"
            parts = [
                f"{h}: {v.strip()}"
                for h, v in zip(headers, row)
                if h and v.strip()
            ]
            if parts:
                lines.append(" | ".join(parts))

        if lines:
            sections.append(f"[Tab: {ws.title}]\n" + "\n".join(lines))

    if not sections:
        logger.info("[ShopData] Sheet geladen, aber keine Daten gefunden")
        return None

    block = (
        "### Shop-Bewertungsdaten (aus AAM Google Sheets, automatisch geladen)\n"
        + "\n\n".join(sections)
    )
    logger.info(
        f"[ShopData] {len(sh.worksheets())} Tab(s) geladen, "
        f"{sum(s.count(chr(10)) for s in sections)} Zeilen, "
        f"{len(block)} Zeichen"
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
    # Fehler: alten Cache behalten
    return False
