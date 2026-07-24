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
import logging
import gspread
from config import SPREADSHEET_ID, SHEET_NAME

logger = logging.getLogger(__name__)

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

    # ── Verbindung ────────────────────────────────────────────────────────────
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
        logger.info(f"📥 Sheet geladen: {len(self._rows) - 1} Einträge (von {len(all_rows)} Zeilen)")

    # ── Lese-Helfer ───────────────────────────────────────────────────────────
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

    # ── Schreib-Operationen ───────────────────────────────────────────────────
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

    def clear_row(self, row_num: int) -> None:
        """Leert die Zellen A–I einer Zeile. Die Zeilennummer bleibt stabil
        (nichts rutscht nach), damit bestehende Tracking-Zuordnungen gueltig
        bleiben. Haelt den Cache aktuell."""
        self.ws.batch_clear([f"A{row_num}:I{row_num}"])
        if 1 <= row_num <= len(self._rows):
            self._rows[row_num - 1] = [""] * 26


# Singleton – wird von allen Cogs und Utils geteilt
sheet = SheetCache()


import re as _re
from urllib.parse import urlparse as _urlparse


def _extract_domain(url: str) -> str:
    """Extrahiert den Domainnamen (ohne www.) aus einer URL oder gibt '' zurück."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        domain = _urlparse(url).netloc.lower()
        return _re.sub(r'^www\.', '', domain)
    except Exception:
        return ""


def _normalize_sheet_key(raw: str) -> str:
    """
    Normalisiert einen Sheet-Spalte-A-Eintrag für den Domain-Exact-Match.
    Entfernt fuehrendes 'www.' und abschliessende Slashes,
    damit 'www.antandco.fr' dasselbe trifft wie extrahiertes 'antandco.fr'.
    """
    k = raw.strip().lower().rstrip("/")
    k = _re.sub(r'^www\.', '', k)
    return k


def _normalize_for_fuzzy(name: str) -> str:
    """
    Normalisiert einen Namen für Fuzzy-Fallback-Matching.
    Entfernt generische TLDs, ersetzt Sonderzeichen durch Leerzeichen.
    Laendercodes (.at, .de etc.) werden NICHT entfernt damit
    sie nicht als Fallback-Match verwechselt werden.
    """
    # Nur generische TLDs entfernen
    name = _re.sub(
        r'\.(com|net|org|shop|store|info)$', '', name, flags=_re.IGNORECASE,
    )
    name = _re.sub(r'[-.]', ' ', name)
    return _re.sub(r'\s+', ' ', name).strip().lower()


async def sync_ratings_from_sheet(bot) -> int:
    """
    Liest Durchschnittsbewertungen aus dem 'Haendler A-Z' Sheet (Spalte A = Domain/Name,
    Spalte C = Durchschnitt) und schreibt sie in shops.average_rating.

    Matching-Strategie (in Reihenfolge):
      1. Exakter Domain-Match: Shop-URL aus DB gegen Sheet-Domain (z.B. antstore.at → antstore.at)
      2. Fuzzy-Fallback: normalisierter Name gegen normalisierte Sheet-Einträge (>=80%)
         für Shops ohne Domain-Eintrag (z.B. 'asama', 'bambulab')

    So können zwei Shops mit gleicher Basis-Domain aber unterschiedlicher TLD
    (antstore.at vs. antstore.net) korrekt getrennt bewertet werden.
    """
    import logging
    from rapidfuzz import process, fuzz
    from utils.db import execute_db, execute_many

    logger = logging.getLogger(__name__)

    def _read():
        ws = _gc.open_by_key(SPREADSHEET_ID).worksheet("Händler A-Z")
        rows = ws.get_all_values()
        logger.debug(f"🔍 sync_ratings: {len(rows)} Zeilen gelesen")
        # {normalisierter_sheet_eintrag: rating}
        # Schlüssel: www. und Trailing-Slash entfernt, lowercase
        # Beispiel: 'www.antandco.fr' → 'antandco.fr', 'anthillshop.es/' → 'anthillshop.es'
        result = {}
        for row in rows[1:]:
            if len(row) >= 3 and row[0].strip() and row[2].strip():
                try:
                    rating_str = row[2].replace(",", ".").strip()
                    key = _normalize_sheet_key(row[0])
                    result[key] = float(rating_str)
                except ValueError:
                    logger.debug(f"🔍 sync_ratings: Parsing fehlgeschlagen: '{row[2]}' für '{row[0]}'")
        return result

    try:
        sheet_ratings = await bot.loop.run_in_executor(None, _read)
    except Exception as e:
        logger.error(f"❌ sync_ratings: Sheet-Lesefehler: {e}")
        return 0

    if not sheet_ratings:
        logger.warning("⚠️ sync_ratings: Keine Ratings im Sheet gefunden")
        return 0

    shop_rows = await execute_db(
        bot, "SELECT id, name, url, url_override FROM shops WHERE name IS NOT NULL", fetch=True
    )

    # Fuzzy-Fallback: normalisierte Sheet-Einträge
    sheet_fuzzy = {_normalize_for_fuzzy(k): (k, v) for k, v in sheet_ratings.items()}
    sheet_fuzzy_keys = list(sheet_fuzzy.keys())

    updated = 0
    update_params: list = []
    for row in shop_rows:
        rating = None
        match_info = ""

        # 1. Exakter Domain-Match gegen Shop-URL
        effective_url = row["url_override"] or row["url"] or ""
        domain = _extract_domain(effective_url)
        if domain and domain in sheet_ratings:
            rating = sheet_ratings[domain]
            match_info = f"domain-exact '{domain}'"

        # 2. Fuzzy-Fallback (normalisierter Name)
        if rating is None:
            shop_norm = _normalize_for_fuzzy(row["name"] or "")
            if shop_norm:
                m = process.extractOne(
                    shop_norm, sheet_fuzzy_keys,
                    scorer=fuzz.token_sort_ratio, score_cutoff=81,
                )
                if m:
                    orig_key, orig_rating = sheet_fuzzy[m[0]]
                    rating = orig_rating
                    match_info = f"fuzzy '{orig_key}' ({m[1]:.0f}%)"

        if rating is not None:
            update_params.append((rating, row["id"]))
            updated += 1
            logger.info(f"  📊 Rating: '{row['name']}' [{match_info}] = {rating:.2f}")

    if update_params:
        await execute_many(bot, "UPDATE shops SET average_rating=? WHERE id=?", update_params)

    logger.info(f"📊 sync_ratings: {updated}/{len(shop_rows)} Shops mit Sheet-Rating versehen")
    return updated


# ── Community-Warnhinweise (Tab "Übersicht", ab Zeile 40) ─────────────────────
# Spalten: A = Stufe (1–3), B = Hinweis, C = Shop (Domain), D = Eintragungsdatum.

_WARNINGS: dict[str, list] = {}          # normalisierte Domain → [ {level,text,shop,date}, … ]
_WARNINGS_FUZZY: dict[str, str] = {}     # Fuzzy-Name → Domain-Key
_WARN_EMOJI = {1: "🟡", 2: "🟠", 3: "🔴"}


def warn_emoji(level) -> str:
    """Emoji je Warnstufe (1/2/3); unbekannt → ⚠️."""
    try:
        return _WARN_EMOJI.get(int(level), "⚠️")
    except (TypeError, ValueError):
        return "⚠️"


def _warn_level(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def sync_warnings_from_sheet(bot) -> int:
    """Liest den Community-Warnhinweis-Block aus dem 'Übersicht'-Tab (ab Zeile 40)
    und aktualisiert den Cache. Rückgabe: Anzahl Einträge (–1 = Lesefehler,
    Cache bleibt unverändert)."""
    import logging
    logger = logging.getLogger(__name__)

    def _read():
        ws = _gc.open_by_key(SPREADSHEET_ID).worksheet("Übersicht")
        rows = ws.get_all_values()
        data: dict[str, list] = {}
        for row in rows[39:]:            # Zeile 40 = Index 39
            if not row:
                continue
            level = _warn_level(row[0]) if len(row) > 0 and row[0].strip() else None
            shop  = row[2].strip() if len(row) > 2 else ""
            if level is None or level < 1 or not shop:
                continue
            text = row[1].strip() if len(row) > 1 else ""
            date = row[3].strip() if len(row) > 3 else ""
            key = _extract_domain(shop) or _normalize_sheet_key(shop)
            data.setdefault(key, []).append(
                {"level": level, "text": text, "shop": shop, "date": date}
            )
        return data

    try:
        data = await bot.loop.run_in_executor(None, _read)
    except Exception as e:
        logger.error(f"❌ sync_warnings: Sheet-Lesefehler: {e}")
        return -1

    global _WARNINGS, _WARNINGS_FUZZY
    _WARNINGS = data
    _WARNINGS_FUZZY = {_normalize_for_fuzzy(k): k for k in data if _normalize_for_fuzzy(k)}
    total = sum(len(v) for v in data.values())
    logger.info(f"⚠️ sync_warnings: {total} Warnhinweis(e) für {len(data)} Shop(s) geladen")
    return total


def get_shop_warnings(url: str = "", name: str = "") -> list:
    """Warnhinweise für einen Shop: Domain-Exact-Match, sonst strenger Fuzzy-Name."""
    if not _WARNINGS:
        return []
    dom = _extract_domain(url or "")
    if dom and dom in _WARNINGS:
        return _WARNINGS[dom]
    norm = _normalize_for_fuzzy(name or "")
    if norm and _WARNINGS_FUZZY:
        from rapidfuzz import process, fuzz
        m = process.extractOne(
            norm, list(_WARNINGS_FUZZY.keys()),
            scorer=fuzz.token_sort_ratio, score_cutoff=90,
        )
        if m:
            return _WARNINGS.get(_WARNINGS_FUZZY[m[0]], [])
    return []


def all_warnings() -> list:
    """Alle Warnhinweise, nach Stufe (absteigend) und Shop sortiert."""
    out: list = []
    for lst in _WARNINGS.values():
        out.extend(lst)
    out.sort(key=lambda w: (-int(w.get("level", 0) or 0), str(w.get("shop", "")).lower()))
    return out
