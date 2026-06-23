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
