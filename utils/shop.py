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
from pathlib import Path

import discord
from rapidfuzz import process as fuzz_proc, fuzz

from config import MAPPING_FILE, FUZZY_THRESHOLD
from utils.sheet import sheet


# ── Eigene Exception ────────────────────────────────────────────────────────────
class UnresolvableShop(Exception):
    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"Unaufgelöst: '{identifier}'")


# ── Interne Helfer ──────────────────────────────────────────────────────────────
_CSV_COLS = ["identifier", "shop_url", "message_id", "hinweis"]
_URL_RE   = re.compile(r'[\w\-]+\.(de|com|net|fr|store|eu|shop|at|ch|nl|pl|es|co\.uk)', re.I)

_map_cache: dict | None = None   # None = noch nicht geladen


def _is_url(s: str) -> bool:
    return bool(_URL_RE.search(s))


def _fuzzy(identifier: str) -> str | None:
    result = fuzz_proc.extractOne(
        identifier, sheet.known_shops,
        scorer=fuzz.WRatio, score_cutoff=FUZZY_THRESHOLD, processor=str.casefold,
    )
    if result:
        print(f"🔍 Fuzzy: '{identifier}' → '{result[0]}' ({result[1]:.0f}%)")
        return result[0]
    return None


def _read_csv() -> dict:
    if not Path(MAPPING_FILE).exists():
        return {}
    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        return {
            r["identifier"].strip(): r["shop_url"].strip()
            for r in csv.DictReader(f)
            if r.get("shop_url", "").strip()
        }


def _write_csv_row(identifier: str, shop_url: str, msg_id: str, hint: str) -> bool:
    """Schreibt eine Zeile wenn Identifier noch nicht vorhanden. Gibt True bei Neueinträgen."""
    exists = Path(MAPPING_FILE).exists()
    if exists:
        with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
            if any(r.get("identifier", "").strip() == identifier for r in csv.DictReader(f)):
                return False
    with open(MAPPING_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        if not exists:
            w.writeheader()
        w.writerow({"identifier": identifier, "shop_url": shop_url,
                    "message_id": msg_id, "hinweis": hint})
    return True


# ── Öffentliche API ─────────────────────────────────────────────────────────────
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
        print(f"📋 CSV: '{identifier}' zum Ausfüllen hinzugefügt")


def learn_shop(identifier: str, shop_url: str) -> None:
    """Aus Reconcile gelernt: Identifier → Shop dauerhaft speichern."""
    global _map_cache
    mapping = load_mapping()
    if identifier in mapping:
        return
    if _write_csv_row(identifier, shop_url, "auto", "auto-gelernt"):
        mapping[identifier] = shop_url
        print(f"📚 Gelernt: '{identifier}' → '{shop_url}'")


def extract_identifier(content: str) -> str | None:
    """Extrahiert den rohen Identifier (Discord-ID oder Text nach 'Shop:') ohne Auflösung."""
    ids = re.findall(r"<@!?(\d+)>", content)
    if ids:
        return ids[0]
    m = re.search(r"Shop:\s*(.+?)(?:\n|\r|$)", content)
    return m.group(1).strip() if m else None


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
        raw = m.group(1).strip()
        if raw in mapping:
            return mapping[raw]
        if _is_url(raw):
            return raw
        fuzzy = _fuzzy(raw)
        if fuzzy:
            return fuzzy
        raise UnresolvableShop(raw)

    raise UnresolvableShop("(kein Shop erkannt)")
