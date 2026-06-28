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
grabber.py - AntCheck API Datenabholer.

Wird als Cron-Job oder per Hand ausgefuehrt (NICHT Teil des Bots selbst).
Laedt Shops + Produkte von der AntCheck API und speichert das Ergebnis
als shops_data.json im DATA_DIRECTORY.

Typischer Aufruf (crontab):
  0 * * * * cd /opt/discord-bot && .venv/bin/python grabber.py

Umgebungsvariablen:
  ANTCHECK_API_KEY   - API-Key (Pflicht)
  ANTCHECK_API_URL   - Basis-URL (Standard: https://antcheck.info)
  ANTCHECK_VERIFY_SSL- SSL-Zertifikat pruefen (Standard: true)
  DATA_DIRECTORY     - Zielverzeichnis fuer shops_data.json
"""
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("grabber")

# -- Konfiguration -------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY         = os.getenv("ANTCHECK_API_KEY", "")
API_BASE        = os.getenv("ANTCHECK_API_URL", "https://antcheck.info").rstrip("/")
API_TIMEOUT     = int(os.getenv("ANTCHECK_TIMEOUT", "30"))
API_RETRIES     = int(os.getenv("ANTCHECK_RETRIES", "3"))
API_RETRY_DELAY = float(os.getenv("ANTCHECK_RETRY_DELAY", "5"))
API_VERIFY_SSL  = os.getenv("ANTCHECK_VERIFY_SSL", "true").lower() not in ("0", "false", "no")
DATA_DIRECTORY    = os.getenv("DATA_DIRECTORY", str(Path(__file__).parent))
OUTPUT_FILE       = Path(DATA_DIRECTORY) / "shops_data.json"
PRICE_HISTORY_DB  = Path(DATA_DIRECTORY) / "price_history.db"

SHOPS_URL    = f"{API_BASE}/api/v2/ecommerce/shops?online=true&crawler_active=true&page=0&limit=-1&api_key={API_KEY}"
PRODUCTS_URL = f"{API_BASE}/api/v2/ecommerce/products?shop_id={{shop_id}}&product_type=ants&page=0&limit=-1&api_key={API_KEY}"

if not API_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# -- HTTP-Helfer ---------------------------------------------------------------

def _fetch_json(url: str) -> dict | list:
    """Holt JSON von der URL mit Retry-Logik."""
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=API_TIMEOUT, verify=API_VERIFY_SSL)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"Versuch {attempt}/{API_RETRIES} fehlgeschlagen: {e}")
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)
    raise RuntimeError(f"API nach {API_RETRIES} Versuchen nicht erreichbar: {url.split('?')[0]}")


# -- Datenverarbeitung ---------------------------------------------------------

def build_shop_map(shops_raw: list) -> dict:
    """Baut die Shop-Map aus der Shops-API-Antwort auf."""
    result = {}
    for shop in shops_raw:
        sid = str(shop.get("id", ""))
        if not sid:
            continue
        result[sid] = {
            "id":             sid,
            "name":           shop.get("name", ""),
            "country":        (shop.get("country") or shop.get("country_code") or "").lower(),
            "url":            shop.get("url") or shop.get("website") or "",
            "average_rating": shop.get("rating") or shop.get("average_rating"),
            "products":       [],
        }
    return result


def add_products(shop_map: dict, shop_id: str, products_raw: list) -> None:
    """Fuegt Produkte zu einem Shop in der Map hinzu."""
    if shop_id not in shop_map:
        return
    for p in products_raw:
        species_name = (
            p.get("species_name") or p.get("name") or p.get("title") or ""
        ).strip()
        genus = species_name.split()[0] if " " in species_name else species_name
        shop_map[shop_id]["products"].append({
            "id":            p.get("id"),
            "species":       species_name,
            "genus":         genus,
            "min_price":     str(p.get("min_price") or p.get("price") or "0"),
            "max_price":     str(p.get("max_price") or p.get("price") or "0"),
            "currency_iso":  p.get("currency_iso") or p.get("currency") or "EUR",
            "antcheck_url":  p.get("antcheck_url") or p.get("url") or "",
            "shop_url":      p.get("product_url") or p.get("shop_url") or "",
            "in_stock":      bool(p.get("in_stock", False)),
            "is_active":     bool(p.get("is_active", False)),
        })


# -- Preis-Tracking ------------------------------------------------------------

_PRICE_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    min_price    REAL    NOT NULL,
    max_price    REAL    NOT NULL,
    currency_iso TEXT    NOT NULL DEFAULT 'EUR',
    recorded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pph_product
    ON product_price_history(product_id, recorded_at DESC);
"""


def _track_prices(shop_map: dict) -> tuple[int, int]:
    """
    Vergleicht aktuelle Preise mit dem letzten Eintrag in price_history.db.
    Schreibt nur einen neuen Eintrag wenn sich der Preis geaendert hat.
    Produkte mit Preis 0 werden ignoriert.
    Gibt (neue Eintraege, gecheckte Produkte) zurueck.
    """
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        conn.executescript(_PRICE_HISTORY_SCHEMA)
        conn.commit()
        cur = conn.cursor()

        new_entries = 0
        checked = 0

        for shop in shop_map.values():
            for p in shop.get("products", []):
                pid = p.get("id")
                if pid is None:
                    continue
                try:
                    min_p = float(p.get("min_price") or 0)
                    max_p = float(p.get("max_price") or 0)
                except (TypeError, ValueError):
                    continue
                # 0€ ignorieren
                if min_p == 0.0 and max_p == 0.0:
                    continue

                currency = p.get("currency_iso") or "EUR"
                checked += 1

                # Letzten Eintrag holen
                cur.execute(
                    "SELECT min_price, max_price FROM product_price_history "
                    "WHERE product_id=? ORDER BY recorded_at DESC LIMIT 1",
                    (pid,),
                )
                last = cur.fetchone()

                if last is None or last[0] != min_p or last[1] != max_p:
                    cur.execute(
                        "INSERT INTO product_price_history "
                        "(product_id, min_price, max_price, currency_iso) VALUES (?,?,?,?)",
                        (pid, min_p, max_p, currency),
                    )
                    new_entries += 1

        conn.commit()
        return new_entries, checked
    finally:
        conn.close()


# -- Hauptprogramm -------------------------------------------------------------

def main():
    if not API_KEY:
        logger.error("ANTCHECK_API_KEY ist nicht gesetzt – abbruch.")
        sys.exit(1)

    start = time.monotonic()
    logger.info(f"Starte AntCheck Grabber – Ziel: {OUTPUT_FILE}")

    try:
        # 1. Shops laden
        logger.info("Lade Shops...")
        shops_raw = _fetch_json(SHOPS_URL)
        if not isinstance(shops_raw, list):
            shops_raw = shops_raw.get("data", shops_raw.get("shops", []))
        shop_map = build_shop_map(shops_raw)
        logger.info(f"  {len(shop_map)} Shops gefunden")

        # 2. Produkte pro Shop laden
        total_products = 0
        for i, (shop_id, shop) in enumerate(shop_map.items(), 1):
            try:
                url = PRODUCTS_URL.format(shop_id=shop_id)
                products_raw = _fetch_json(url)
                if not isinstance(products_raw, list):
                    products_raw = products_raw.get("data", products_raw.get("products", []))
                add_products(shop_map, shop_id, products_raw)
                count = len(shop_map[shop_id]["products"])
                total_products += count
                logger.info(f"  [{i}/{len(shop_map)}] Shop {shop['name']}: {count} Produkte")
            except Exception as e:
                logger.warning(f"  Shop {shop_id} Produkte fehlgeschlagen: {e}")

        # 3. Ausgabe schreiben
        output = {
            "_meta": {
                "fetched_at":    datetime.now(timezone.utc).isoformat(),
                "shop_count":    len(shop_map),
                "product_count": total_products,
            },
            **shop_map,
        }
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUTPUT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUTPUT_FILE)

        # 4. Preis-History tracken
        try:
            new_entries, checked = _track_prices(shop_map)
            logger.info(
                f"Preis-Tracking: {checked} Produkte gecheckt, "
                f"{new_entries} neue Eintraege -> {PRICE_HISTORY_DB}"
            )
        except Exception as e:
            logger.warning(f"Preis-Tracking fehlgeschlagen (nicht kritisch): {e}")

        elapsed = time.monotonic() - start
        logger.info(
            f"Fertig: {len(shop_map)} Shops / {total_products} Produkte "
            f"-> {OUTPUT_FILE} ({elapsed:.1f}s)"
        )

    except Exception as e:
        logger.error(f"Grabber fehlgeschlagen: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
