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
utils/availability.py - AntCheck-Verfügbarkeitspruefung.

Liest shops_data.json (erzeugt von grabber.py) und prüft ob eine Art/Gattung
verfügbar ist. Produkte sind direkt in shops_data.json eingebettet.
"""
import os
import re
import html
import json
import asyncio
import logging
import threading
from config import SHOPS_DATA_FILE


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text) -> str:
    """Entfernt HTML-Tags, dekodiert Entities und normalisiert Whitespace –
    schützt Anzeigenamen/Beschreibungen aus Shop-Daten vor rohem HTML."""
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", str(text))
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()

logger = logging.getLogger(__name__)


def normalize_species_name(name: str) -> str:
    """Normalisiert Artnamen (cf./sp./aff. entfernen, Leerzeichen reduzieren)."""
    name = re.sub(r"\s*\b(cf|sp|aff)\.?\s*", " ", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", name).strip().lower()


def format_rating(rating) -> str:
    """Formatiert eine Shopbewertung als 'Stern 4.75' oder 'kein Rating'."""
    try:
        return f"⭐ {float(rating):.2f}"
    except (TypeError, ValueError):
        return "❌"


def available_variants(product: dict) -> list:
    """
    Nur lagernde, aktive Varianten eines Produkts (aus shops_data.json).
    Steht allen Consumern zur Verfuegung (z.B. /sells fuer Einzelpreise).
    Leere Liste, wenn das Produkt (noch) keine Varianten hat.
    """
    return [
        v for v in (product.get("variants") or [])
        if v.get("in_stock") and v.get("is_active")
    ]


def _hard_split_entry(entry: str, max_length: int) -> list[str]:
    """Zerlegt EINEN (evtl. überlangen) Eintrag an Zeilenumbrüchen in Stücke <= max_length.
    Notfalls wird eine einzelne, zu lange Zeile hart geschnitten – so ist NIE ein Stück
    länger als max_length (verhindert Discord-HTTP-400 'content > 2000')."""
    if len(entry) <= max_length:
        return [entry]
    pieces, cur = [], ""
    for line in entry.split("\n"):
        while len(line) > max_length:
            if cur:
                pieces.append(cur.rstrip("\n")); cur = ""
            pieces.append(line[:max_length]); line = line[max_length:]
        if cur and len(cur) + len(line) + 1 > max_length:
            pieces.append(cur.rstrip("\n")); cur = ""
        cur += line + "\n"
    if cur.strip():
        pieces.append(cur.rstrip("\n"))
    return pieces


def split_availability_messages(entries: list[str], max_length: int = 2000) -> list[str]:
    """Teilt eine Liste von Availability-Einträgen in Discord-taugliche Chunks (jeder <= max_length)."""
    chunks, current, current_len = [], [], 0
    for entry in entries:
        for part in _hard_split_entry(entry, max_length):
            part_len = len(part) + 2
            if current and current_len + part_len > max_length:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            current.append(part)
            current_len += part_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# Cache: shops_data.json wird nur bei geänderter Datei (mtime+Größe) neu geparst.
# Die gecachte Struktur wird NICHT mutiert – Aufrufer, die Felder ergänzen
# (load_shop_data), kopieren die Shop-Dicts vorher flach.
_shops_cache_lock = threading.Lock()
_shops_cache: dict = {"key": None, "data": None}


def _load_shops_json() -> dict:
    """Lädt shops_data.json (gecacht per mtime+Größe) und gibt {shop_id: shop_dict}
    zurück (ohne _meta). Rückgabe ist der geteilte Cache – nicht mutieren!"""
    try:
        st = os.stat(SHOPS_DATA_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        logger.error(f"❌ shops_data.json nicht gefunden: {SHOPS_DATA_FILE}")
        return {}
    except OSError as e:
        logger.error(f"❌ Fehler beim Zugriff auf shops_data.json: {e}")
        return {}

    with _shops_cache_lock:
        if _shops_cache["data"] is not None and _shops_cache["key"] == key:
            return _shops_cache["data"]
        try:
            with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logger.error(f"❌ Fehler beim Laden von shops_data.json: {e}")
            return {}

        if isinstance(raw, dict):
            result = {k: v for k, v in raw.items() if k != "_meta" and isinstance(v, dict)}
        else:
            result = {}
            for shop in raw:
                if isinstance(shop, dict) and "id" in shop:
                    result[str(shop["id"])] = shop

        _shops_cache["key"] = key
        _shops_cache["data"] = result
        logger.debug("🗂️ shops_data.json neu geparst (%d Shops)", len(result))
        return result


async def load_shop_data(bot) -> dict:
    """
    Lädt shops_data.json und ergaenzt Bewertungen aus der DB.
    Gibt {shop_id_str: shop_dict} zurück.
    """
    from utils.db import execute_db

    cached = await bot.loop.run_in_executor(None, _load_shops_json)
    if not cached:
        return {}
    # Flache Kopie je Shop, damit der Rating/URL-Merge den Cache nicht verändert
    # (verschachtelte Listen wie "products" werden nur gelesen, bleiben geteilt).
    shops = {sid: dict(shop) for sid, shop in cached.items()}

    rows = await execute_db(bot, "SELECT id, average_rating, url_override FROM shops", fetch=True)
    for r in rows:
        sid = str(r["id"])
        if sid not in shops:
            continue
        if r["average_rating"] is not None:
            shops[sid]["average_rating"] = r["average_rating"]
        if r["url_override"]:
            shops[sid]["url"] = r["url_override"]

    return shops


async def expand_regions(bot, regions: list[str]) -> list[str]:
    """Ersetzt 'eu' durch alle EU-Ländercodes aus der DB."""
    from utils.db import execute_db

    regions = [r.strip().lower() for r in regions]
    if "eu" not in regions:
        return regions

    rows = await execute_db(bot, "SELECT code FROM eu_countries", fetch=True)
    eu_codes = [r["code"].lower() for r in rows]
    regions = [r for r in regions if r != "eu"] + eu_codes
    return list(set(regions))


async def species_exists(bot, search_term: str) -> bool:
    """Prüft ob eine Art/Gattung in shops_data.json vorkommt."""
    shops = await bot.loop.run_in_executor(None, _load_shops_json)
    normalized = normalize_species_name(search_term)
    is_genus = " " not in normalized.strip()

    for shop in shops.values():
        for product in shop.get("products", []):
            title = normalize_species_name(product.get("species", ""))
            if is_genus:
                if title.startswith(normalized + " "):
                    return True
            else:
                if title == normalized:
                    return True
    return False


async def check_availability_for_species(
    bot,
    species_or_genus: str,
    regions: list[str],
    user_id: str | None = None,
    ch_mode: bool = False,
    ch_shops: set | None = None,
    excluded_species_list: set | None = None,
) -> list[dict]:
    """
    Prüft Verfügbarkeit einer Art/Gattung in den gegebenen Regionen.

    Returns:
        Liste von verfügbaren Produkten (dicts mit species, shop_name, etc.)
    """
    from utils.db import execute_db

    if excluded_species_list is None:
        excluded_species_list = set()

    blacklisted = set()
    if user_id is not None:
        rows = await execute_db(
            bot,
            "SELECT shop_id FROM user_shop_blacklist WHERE user_id=?",
            (user_id,),
            fetch=True,
        )
        blacklisted = {str(r["shop_id"]) for r in rows}

    shop_data = await load_shop_data(bot)

    normalized_search = normalize_species_name(species_or_genus)
    is_genus = " " not in species_or_genus.strip()
    region_set = {r.lower() for r in regions}

    results = []
    for shop_id_str, shop_info in shop_data.items():
        if shop_id_str in blacklisted:
            continue

        if ch_mode:
            if ch_shops and shop_id_str not in ch_shops:
                continue
        else:
            if shop_info.get("country", "").lower() not in region_set:
                continue

        for product in shop_info.get("products", []):
            species = product.get("species", "").strip()
            norm_title = normalize_species_name(species)

            match = False
            if is_genus:
                if norm_title.startswith(normalized_search + " "):
                    part = norm_title.split()[1] if len(norm_title.split()) > 1 else ""
                    if part not in excluded_species_list:
                        match = True
            else:
                match = norm_title == normalized_search

            if match:
                # Nur lagerverfügbare, aktive Produkte beruecksichtigen
                if not product.get("in_stock", False):
                    logger.debug(
                        f"🔍 Produkt {product.get('id')} ({species}) bei "
                        f"{shop_info.get('name', shop_id_str)}: not in_stock – übersprungen"
                    )
                    continue
                if not product.get("is_active", False):
                    logger.debug(
                        f"🔍 Produkt {product.get('id')} ({species}) bei "
                        f"{shop_info.get('name', shop_id_str)}: not is_active – übersprungen"
                    )
                    continue

                results.append({
                    "id":           product.get("id"),
                    "species":      species,
                    "shop_name":    shop_info.get("name", ""),
                    "min_price":    product.get("min_price"),
                    "max_price":    product.get("max_price"),
                    "currency_iso": product.get("currency_iso"),
                    "antcheck_url": product.get("antcheck_url"),
                    "shop_url":     shop_info.get("url"),
                    "shop_id":      shop_id_str,
                    "rating":       shop_info.get("average_rating"),
                    "variants":     product.get("variants") or [],
                })

    return results
