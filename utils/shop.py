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
utils/shop.py – Shop-Auflösung und CSV-Mapping für den AAM Review Bot.

Auflösungs-Reihenfolge:
  1. shop_mapping.csv  (manuell / auto-gelernt)
  2. Discord display_name wenn URL-artig
  3. Fuzzy-Match gegen bestehende Sheet-Shopnamen (≥80 %)
  4. UnresolvableShop-Exception

Verwendung:
    from utils.shop import resolve_shop, extract_identifier, learn_shop, UnresolvableShop
"""
import re
import csv
import logging
from pathlib import Path

import discord
from rapidfuzz import process as fuzz_proc, fuzz

from config import MAPPING_FILE, FUZZY_THRESHOLD
from utils.sheet import sheet

logger = logging.getLogger(__name__)


# ── Eigene Exception ──────────────────────────────────────────────────────────
class UnresolvableShop(Exception):
    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"Unaufgelöst: '{identifier}'")


# ── Interne Helfer ────────────────────────────────────────────────────────────
_CSV_COLS    = ["identifier", "shop_url", "message_id", "hinweis"]
_URL_RE      = re.compile(r'[\w\-]+\.(de|com|net|fr|store|eu|shop|at|ch|nl|pl|es|co\.uk)', re.I)
_MARKDOWN_RE = re.compile(r'\[([^\]]*)\]\(https?://(?:www\.)?([^/)\s]+)[^)]*\)')

_map_cache: dict | None = None   # None = noch nicht geladen


_MENTION_RE = re.compile(r'^<@!?(\d+)>$')


def _strip_markdown_url(s: str) -> str:
    """
    Wandelt Discord-Markdown-Links in saubere Domains um:
      [www.antpire.net](https://www.antpire.net) → antpire.net
      [Antpire](https://www.antpire.net/shop)   → antpire.net
    Ohne Match: Originalstring zurück.
    """
    m = _MARKDOWN_RE.search(s)
    return m.group(2) if m else s


def _normalize_identifier(s: str) -> str:
    """Vereinheitlicht einen Identifier für Speichern UND Nachschlagen.

    Eine Discord-Mention `<@123>`/`<@!123>` wird zur nackten ID `123` – genau so,
    wie resolve_shop() sie beim Auflösen nachschlägt. Sonst wie _strip_markdown_url.
    Verhindert, dass `/shopmap set <@123> …` unter `<@123>` landet, während die
    Review-Auflösung `123` sucht (→ 🟡 Unaufgelöst).
    """
    s = (s or "").strip()
    m = _MENTION_RE.match(s)
    if m:
        return m.group(1)
    return _strip_markdown_url(s)


def _is_url(s: str) -> bool:
    return bool(_URL_RE.search(s))


def _fuzzy(identifier: str) -> str | None:
    result = fuzz_proc.extractOne(
        identifier, sheet.known_shops,
        scorer=fuzz.WRatio, score_cutoff=FUZZY_THRESHOLD, processor=str.casefold,
    )
    if result:
        logger.info(f"🔍 Fuzzy: '{identifier}' → '{result[0]}' ({result[1]:.0f}%)")
        return result[0]
    return None


def _read_csv() -> dict:
    if not Path(MAPPING_FILE).exists():
        return {}
    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        result = {}
        for r in csv.DictReader(f):
            raw_url = r.get("shop_url", "").strip()
            if not raw_url:
                continue
            identifier = _normalize_identifier(r["identifier"])
            shop_url   = _strip_markdown_url(raw_url)
            result[identifier] = shop_url
        return result


def _write_csv_row(identifier: str, shop_url: str, msg_id: str, hint: str) -> bool:
    """Schreibt eine Zeile wenn Identifier noch nicht vorhanden. Gibt True bei Neueinträgen."""
    identifier = _normalize_identifier(identifier)
    exists = Path(MAPPING_FILE).exists()
    if exists:
        with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
            if any(_normalize_identifier(r.get("identifier", "")) == identifier for r in csv.DictReader(f)):
                return False
    with open(MAPPING_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        if not exists:
            w.writeheader()
        w.writerow({"identifier": identifier, "shop_url": shop_url,
                    "message_id": msg_id, "hinweis": hint})
    return True


# ── Öffentliche API ───────────────────────────────────────────────────────────
def load_mapping() -> dict:
    """Gibt {identifier: shop_url} zurück – gecacht, liest CSV nur einmal."""
    global _map_cache
    if _map_cache is None:
        _map_cache = _read_csv()
    return _map_cache


def reload_mapping() -> dict:
    """Erzwingt Neu-Einlesen der CSV (z.B. nach manuellem Ausfüllen durch User)."""
    global _map_cache
    _map_cache = None
    return load_mapping()


def add_to_csv(identifier: str, msg_id: str, hint: str = "") -> None:
    """Unbekannter Shop: leer eintragen zum manuellen Ausfüllen."""
    if _write_csv_row(identifier, "", msg_id, hint):
        logger.info(f"📋 CSV: '{identifier}' zum Ausfüllen hinzugefügt")


def _read_all_rows() -> list:
    """Liest ALLE CSV-Zeilen (auch mit leerem shop_url) als Liste von Dicts."""
    if not Path(MAPPING_FILE).exists():
        return []
    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_all_rows(rows: list) -> None:
    """Schreibt die komplette CSV neu (Header + alle Zeilen)."""
    with open(MAPPING_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c, "") or "") for c in _CSV_COLS})


def set_mapping(identifier: str, shop_url: str,
                msg_id: str = "manual", hint: str = "manuell gesetzt") -> None:
    """
    Setzt/aktualisiert die Zuordnung identifier → shop_url (Upsert) und lädt
    den In-Memory-Cache neu, damit die Änderung sofort greift (ohne Neustart).
    """
    identifier = _normalize_identifier(identifier)
    shop_url   = _strip_markdown_url(shop_url.strip())
    rows = _read_all_rows()
    for r in rows:
        if _normalize_identifier(r.get("identifier", "")) == identifier:
            r["identifier"] = identifier          # ggf. alte `<@…>`-Schreibweise heilen
            r["shop_url"]   = shop_url
            r["hinweis"]    = hint or r.get("hinweis", "")
            break
    else:
        rows.append({"identifier": identifier, "shop_url": shop_url,
                     "message_id": msg_id, "hinweis": hint})
    _write_all_rows(rows)
    reload_mapping()


def remove_mapping(identifier: str) -> bool:
    """Entfernt eine Zuordnung. True wenn etwas entfernt wurde. Lädt Cache neu."""
    identifier = _normalize_identifier(identifier)
    rows = _read_all_rows()
    kept = [r for r in rows if _normalize_identifier(r.get("identifier", "")) != identifier]
    if len(kept) == len(rows):
        return False
    _write_all_rows(kept)
    reload_mapping()
    return True


def all_mappings() -> list:
    """Alle Zeilen als [(identifier, shop_url), …] (auch leere shop_url)."""
    return [(r.get("identifier", "").strip(), (r.get("shop_url", "") or "").strip())
            for r in _read_all_rows()]


def learn_shop(identifier: str, shop_url: str) -> None:
    """Aus Reconcile gelernt: Identifier → Shop dauerhaft speichern."""
    global _map_cache
    # Mention → nackte ID / Markdown-Links bereinigen (konsistent mit resolve_shop)
    identifier = _normalize_identifier(identifier)
    shop_url   = _strip_markdown_url(shop_url)
    if not shop_url:
        return
    mapping = load_mapping()
    if identifier in mapping:
        return
    if _write_csv_row(identifier, shop_url, "auto", "auto-gelernt"):
        mapping[identifier] = shop_url
        logger.info(f"📚 Gelernt: '{identifier}' → '{shop_url}'")


def extract_identifier(content: str) -> str | None:
    """Extrahiert den rohen Identifier (Discord-ID oder Text nach 'Shop:') ohne Auflösung."""
    ids = re.findall(r"<@!?(\d+)>", content)
    if ids:
        return ids[0]
    m = re.search(r"Shop:\s*(.+?)(?:\n|\r|$)", content)
    if m:
        return _strip_markdown_url(m.group(1).strip())
    return None


def resolve_shop(content: str, guild: discord.Guild) -> str:
    """
    Löst den Shop aus dem Nachrichteninhalt auf.

    Auflösungs-Reihenfolge:
      1. shop_mapping.csv
      2. Discord display_name (wenn URL-artig)
      3. Fuzzy-Match gegen bekannte Sheet-Shopnamen
      4. → UnresolvableShop
    """
    mapping = load_mapping()

    # Variante A: Discord-Mention (@User)
    ids = re.findall(r"<@!?(\d+)>", content)
    if ids:
        uid = ids[0]
        if uid in mapping:
            return mapping[uid]
        member = guild.get_member(int(uid))
        name   = (member.display_name or member.global_name or member.name) if member else ""
        if _is_url(name):
            return name
        fuzzy = _fuzzy(uid) or (name and _fuzzy(name))
        if fuzzy:
            return fuzzy
        raise UnresolvableShop(uid)

    # Variante B: "Shop: <name>"
    m = re.search(r"Shop:\s*(.+?)(?:\n|\r|$)", content)
    if m:
        raw = _strip_markdown_url(m.group(1).strip())
        if raw in mapping:
            return mapping[raw]
        if _is_url(raw):
            return raw
        fuzzy = _fuzzy(raw)
        if fuzzy:
            return fuzzy
        raise UnresolvableShop(raw)

    raise UnresolvableShop("(kein Shop erkannt)")
