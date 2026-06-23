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
utils/sheet.py – Google-Sheets-Cache für den AAM Review Bot.

SheetCache lädt das Sheet einmal beim Start und hält alle Daten im Speicher.
Schreiboperationen (append / update) aktualisieren den Cache direkt –
kein erneuter API-Call nötig.

Verwendung:
    from utils.sheet import sheet   # Singleton
    sheet.load()
    row_num = sheet.append([...])
    sheet.update(row_num, [...])
"""
import os
import gspread
from config import SPREADSHEET_ID, SHEET_NAME

# Google-Service-Account einmalig initialisieren
_gc = gspread.service_account(filename="service_account.json")


class SheetCache:
    """
    Einmaliger get_all_values()-Aufruf pro Bot-Session.
    Danach nur noch Schreiben – kein Re-Read.
    """

    def __init__(self):
        self._ws: gspread.Worksheet | None = None
        self._rows: list[list] | None = None

    # ── Verbindung ─────────────────────────────────────────────────────────────
    @property
    def ws(self) -> gspread.Worksheet:
        if self._ws is None:
            self._ws = _gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        return self._ws

    def load(self) -> None:
        """Einmaliger Read beim Start. Leere Trailing-Zeilen werden abgeschnitten."""
        all_rows = self.ws.get_all_values()
        last = 0
        for i, row in enumerate(all_rows):
            if row and row[0].strip():   # nur Spalte A (Datum) zählt
                last = i + 1
        self._rows = all_rows[:last]
        print(f"📥 Sheet geladen: {len(self._rows) - 1} Einträge (von {len(all_rows)} Zeilen)")

    # ── Lese-Helfer ────────────────────────────────────────────────────────────
    @property
    def rows(self) -> list[list]:
        if self._rows is None:
            self.load()
        return self._rows

    @property
    def known_shops(self) -> list[str]:
        return list({r[2].strip() for r in self.rows[1:] if len(r) > 2 and r[2].strip()})

    @property
    def row_count(self) -> int:
        return len(self.rows)

    # ── Schreib-Operationen ────────────────────────────────────────────────────
    def append(self, row: list) -> int:
        """Hängt Zeile an, gibt Zeilennummer zurück, aktualisiert Cache."""
        self.ws.append_row(row, value_input_option="USER_ENTERED")
        padded = [str(v) if v is not None else "" for v in row]
        padded += [""] * max(0, 26 - len(padded))
        self._rows.append(padded)
        return self.row_count

    def update(self, row_num: int, row: list) -> None:
        """Aktualisiert Spalten A–I und hält den Cache aktuell."""
        self.ws.update(
            range_name=f"A{row_num}:I{row_num}",
            values=[row],
            value_input_option="USER_ENTERED",
        )
        if row_num <= len(self._rows):
            for i, v in enumerate(row):
                self._rows[row_num - 1][i] = str(v) if v is not None else ""


# Singleton – wird von allen Cogs und Utils geteilt
sheet = SheetCache()


async def sync_ratings_from_sheet(bot) -> int:
    """
    Liest Durchschnittsbewertungen aus dem 'Haendler A-Z' Sheet (Spalte A = Name,
    Spalte C = Durchschnitt) und schreibt sie via Fuzzy-Match (>=80%) in
    shops.average_rating. Shops ohne passenden Match bleiben unveraendert (kein Rating).
    """
    import logging
    from rapidfuzz import process, fuzz
    from utils.db import execute_db

    logger = logging.getLogger(__name__)

    def _read():
        ws = _gc.open_by_key(SPREADSHEET_ID).worksheet("Händler A-Z")
        rows = ws.get_all_values()
        logger.debug(f"sync_ratings: {len(rows)} Zeilen gelesen, erste 3: {rows[:3]}")
        result = {}
        for row in rows[1:]:  # Headerzeile ueberspringen
            if len(row) >= 3 and row[0].strip() and row[2].strip():
                try:
                    # Deutsches Zahlenformat (Komma) absichern
                    rating_str = row[2].replace(",", ".").strip()
                    result[row[0].strip().lower()] = float(rating_str)
                except ValueError:
                    logger.debug(f"sync_ratings: Konnte Rating nicht parsen: '{row[2]}' fuer '{row[0]}'")
        return result

    try:
        sheet_ratings = await bot.loop.run_in_executor(None, _read)
    except Exception as e:
        logger.error(f"sync_ratings: Sheet-Lesefehler: {e}")
        return 0

    if not sheet_ratings:
        logger.warning("sync_ratings: Keine Ratings im Sheet gefunden")
        return 0

    shop_rows = await execute_db(
        bot, "SELECT id, name FROM shops WHERE name IS NOT NULL", fetch=True
    )
    sheet_names = list(sheet_ratings.keys())

    updated = 0
    for row in shop_rows:
        shop_name = (row["name"] or "").lower()
        if not shop_name:
            continue
        match = process.extractOne(
            shop_name, sheet_names, scorer=fuzz.token_sort_ratio, score_cutoff=80
        )
        if match:
            matched_key, score, _ = match
            rating = sheet_ratings[matched_key]
            await execute_db(
                bot,
                "UPDATE shops SET average_rating=? WHERE id=?",
                (rating, row["id"]),
                commit=True,
            )
            updated += 1
            logger.debug(f"  Rating: '{row['name']}' → '{matched_key}' ({score:.0f}%) = {rating:.2f}")

    logger.info(f"sync_ratings: {updated}/{len(shop_rows)} Shops mit Sheet-Rating versehen")
    return updated
