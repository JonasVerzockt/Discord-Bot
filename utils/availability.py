"""
utils/availability.py – AntCheck-Verfügbarkeitsprüfung.

Liest die von grabber.py erzeugten products_shop_*.json-Dateien und
prüft ob eine Art/Gattung verfügbar ist.

Verwendung:
    from utils.availability import (
        check_availability_for_species, load_shop_data,
        species_exists, expand_regions, format_rating,
        split_availability_messages, normalize_species_name,
    )
"""
import os
import re
import json
import asyncio
import logging
from config import DATA_DIRECTORY, SHOPS_DATA_FILE, DB_FILE

logger = logging.getLogger(__name__)

# Globaler In-Memory-Cache für Shop-Daten
_shop_data_cache: dict | None = None


def normalize_species_name(name: str) -> str:
    """Normalisiert Artnamen (cf./sp./aff. entfernen, Leerzeichen reduzieren)."""
    name = re.sub(r"\s*\b(cf|sp|aff)\.?\s*", " ", name, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", name).strip().lower()


def format_rating(rating) -> str:
    """Formatiert eine Shopbewertung als '⭐ 4.75' oder '❌' wenn nicht vorhanden."""
    try:
        return f"⭐ {float(rating):.2f}"
    except (TypeError, ValueError):
        return "❌"


def split_availability_messages(entries: list[str], max_length: int = 2000) -> list[str]:
    """Teilt eine Liste von Availability-Einträgen in Discord-taugliche Chunks."""
    chunks, current, current_len = [], [], 0
    for entry in entries:
        entry_len = len(entry) + 2
        if current_len + entry_len > max_length:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(entry)
        current_len += entry_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


async def load_shop_data(bot) -> dict:
    """
    Lädt shops_data.json und ergänzt Bewertungen aus der DB.
    Gibt {shop_id_str: shop_dict} zurück.
    """
    from utils.db import execute_db

    def _read_json():
        with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
            return json.load(f)

    try:
        shops_json = await bot.loop.run_in_executor(None, _read_json)
    except FileNotFoundError:
        logger.error(f"shops_data.json nicht gefunden: {SHOPS_DATA_FILE}")
        return {}

    rows = await execute_db(bot, "SELECT id, average_rating FROM shops", fetch=True)
    ratings = {str(r["id"]): r["average_rating"] for r in rows}

    shop_data = {}
    for shop in shops_json:
        sid = str(shop["id"])
        shop_data[sid] = dict(shop)
        shop_data[sid]["average_rating"] = ratings.get(sid)
    return shop_data


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


def species_exists_sync(search_term: str) -> bool:
    """Synchrone Suche (wird in Executor ausgeführt)."""
    normalized = normalize_species_name(search_term)
    is_genus   = " " not in normalized
    try:
        files = os.listdir(DATA_DIRECTORY)
    except FileNotFoundError:
        logger.error(f"DATA_DIRECTORY nicht gefunden: {DATA_DIRECTORY}")
        return False

    for filename in files:
        if not (filename.startswith("products_shop_") and filename.endswith(".json")):
            continue
        try:
            with open(os.path.join(DATA_DIRECTORY, filename), encoding="utf-8") as f:
                for product in json.load(f):
                    title = normalize_species_name(product.get("title", ""))
                    if is_genus:
                        if title.startswith(normalized + " "):
                            return True
                    else:
                        if title == normalized:
                            return True
        except Exception as e:
            logger.error(f"Fehler beim Lesen von {filename}: {e}")
    return False


async def species_exists(bot, search_term: str) -> bool:
    """Async Wrapper für species_exists_sync."""
    return await bot.loop.run_in_executor(None, species_exists_sync, search_term)


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
        Liste von verfügbaren Produkten (dicts mit id, species, shop_name, etc.)
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

    def _sync():
        results = []
        normalized_search = normalize_species_name(species_or_genus)
        is_genus = " " not in species_or_genus.strip()
        region_set = {r.lower() for r in regions}

        try:
            files = os.listdir(DATA_DIRECTORY)
        except FileNotFoundError:
            logger.error(f"DATA_DIRECTORY nicht gefunden: {DATA_DIRECTORY}")
            return []

        for filename in files:
            if not (filename.startswith("products_shop_") and filename.endswith(".json")):
                continue
            try:
                shop_id_str = str(int(filename.split("_")[2].split(".")[0]))
            except (IndexError, ValueError):
                continue

            if str(shop_id_str) in blacklisted:
                continue

            if ch_mode:
                if ch_shops and shop_id_str not in ch_shops:
                    continue
            else:
                shop_info = shop_data.get(shop_id_str, {})
                if shop_info.get("country", "").lower() not in region_set:
                    continue

            try:
                with open(os.path.join(DATA_DIRECTORY, filename), encoding="utf-8") as f:
                    products = json.load(f)
            except Exception:
                continue

            for product in products:
                if not product.get("in_stock", False):
                    continue
                title      = product.get("title", "").strip()
                norm_title = normalize_species_name(title)
                match      = False
                if is_genus:
                    if norm_title.startswith(normalized_search + " "):
                        part = norm_title.split()[1] if len(norm_title.split()) > 1 else ""
                        if part not in excluded_species_list:
                            match = True
                else:
                    match = norm_title == normalized_search

                if match:
                    info = shop_data.get(shop_id_str, {})
                    results.append({
                        "id":           product.get("id"),
                        "species":      title,
                        "shop_name":    info.get("name", ""),
                        "min_price":    product.get("min_price"),
                        "max_price":    product.get("max_price"),
                        "currency_iso": product.get("currency_iso"),
                        "antcheck_url": product.get("antcheck_url"),
                        "shop_url":     info.get("url"),
                        "shop_id":      shop_id_str,
                        "rating":       info.get("average_rating"),
                    })
        return results

    return await bot.loop.run_in_executor(None, _sync)
