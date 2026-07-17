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
Lädt Shops + Produkte von der AntCheck API und speichert das Ergebnis
als shops_data.json im DATA_DIRECTORY.

Typischer Aufruf (crontab):
  0 * * * * cd /opt/discord-bot && .venv/bin/python grabber.py

Umgebungsvariablen:
  ANTCHECK_API_KEY   - API-Key (Pflicht)
  ANTCHECK_API_URL   - Basis-URL (Standard: https://antcheck.info)
  ANTCHECK_VERIFY_SSL- SSL-Zertifikat prüfen (Standard: true)
  DATA_DIRECTORY     - Zielverzeichnis für shops_data.json
"""
import json
import logging
import os
import sqlite3
import sys
import time
import re
import html
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("grabber")

# ── Konfiguration ─────────────────────────────────────────────────────────────
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
# Varianten werden global (nicht pro Shop) geladen und nach product_id gruppiert.
VARIANTS_URL = f"{API_BASE}/api/v2/ecommerce/variants?page=0&limit=-1&api_key={API_KEY}"

if not API_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── HTTP-Helfer ───────────────────────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text) -> str:
    """Entfernt HTML-Tags/Entities aus Shop-Texten (Titel/Beschreibung)."""
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", str(text))
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _fetch_json(url: str) -> dict | list:
    """Holt JSON von der URL mit Retry-Logik."""
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=API_TIMEOUT, verify=API_VERIFY_SSL)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"⚠️ Versuch {attempt}/{API_RETRIES} fehlgeschlagen: {e}")
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)
    raise RuntimeError(f"API nach {API_RETRIES} Versuchen nicht erreichbar: {url.split('?')[0]}")


# ── Datenverarbeitung ─────────────────────────────────────────────────────────

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


def _variant_entry(v: dict, product_currency: str) -> dict:
    """Normalisiert einen Varianten-Datensatz aus /ecommerce/variants."""
    price = v.get("price")
    if price is None:
        price = v.get("min_price") or v.get("amount") or "0"
    return {
        "id":           v.get("id"),
        "title":        _strip_html(v.get("title") or ""),
        "description":  _strip_html(v.get("description") or ""),
        "price":        str(price),
        "currency_iso": v.get("currency_iso") or v.get("currency") or product_currency,
        "url":          v.get("url") or v.get("antcheck_url") or "",
        "in_stock":     bool(v.get("in_stock", False)),
        "is_active":    bool(v.get("is_active", False)),
    }


def _variant_span(variants: list, fb_min: float, fb_max: float):
    """min/max aus lagernden, aktiven Varianten mit Preis>0; sonst Fallback (AntCheck)."""
    prices = []
    for v in variants:
        if not (v.get("in_stock") and v.get("is_active")):
            continue
        try:
            p = float(str(v.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if p > 0:
            prices.append(p)
    if prices:
        return min(prices), max(prices)
    return fb_min, fb_max


def fetch_variants_by_product() -> dict:
    """
    Holt alle Produkt-Varianten global (/ecommerce/variants?limit=-1) und
    gruppiert sie nach product_id. Faellt der Endpoint aus, wird eine leere Map
    zurueckgegeben (Produkte werden trotzdem geschrieben – abwaertskompatibel).
    """
    try:
        raw = _fetch_json(VARIANTS_URL)
    except Exception as e:
        logger.warning(f"⚠️ Varianten-Abruf fehlgeschlagen (nicht kritisch): {e}")
        return {}
    if not isinstance(raw, list):
        raw = raw.get("data", raw.get("variants", []))
    by_pid: dict = {}
    for v in raw:
        if not isinstance(v, dict):
            continue
        pid = v.get("product_id")
        if pid is None:
            continue
        by_pid.setdefault(pid, []).append(v)
    return by_pid


def add_products(shop_map: dict, shop_id: str, products_raw: list,
                 variants_by_pid: dict | None = None) -> None:
    """Fuegt Produkte (inkl. Varianten) zu einem Shop in der Map hinzu."""
    if shop_id not in shop_map:
        return
    variants_by_pid = variants_by_pid or {}
    for p in products_raw:
        species_name = (
            p.get("species_name") or p.get("name") or p.get("title") or ""
        ).strip()
        # Varianteninfo: description/comment falls vorhanden, sonst Artname
        description = _strip_html(p.get("description") or p.get("comment") or "")
        product_title = _strip_html(p.get("name") or p.get("title") or species_name)
        genus = species_name.split()[0] if " " in species_name else species_name
        currency = p.get("currency_iso") or p.get("currency") or "EUR"
        pid = p.get("id")
        variants = [_variant_entry(v, currency) for v in variants_by_pid.get(pid, [])]
        try:
            _fb_min = float(p.get("min_price") or p.get("price") or 0)
        except (TypeError, ValueError):
            _fb_min = 0.0
        try:
            _fb_max = float(p.get("max_price") or p.get("price") or 0)
        except (TypeError, ValueError):
            _fb_max = 0.0
        # Preisspanne bevorzugt aus lagernden Varianten (schließt 0€/ausverkauft aus)
        _span_min, _span_max = _variant_span(variants, _fb_min, _fb_max)
        shop_map[shop_id]["products"].append({
            "id":            pid,
            "species":       species_name,
            "title":         product_title,
            "description":   description,
            "genus":         genus,
            "min_price":     str(_span_min),
            "max_price":     str(_span_max),
            "currency_iso":  currency,
            "antcheck_url":  p.get("antcheck_url") or p.get("url") or "",
            "shop_url":      p.get("product_url") or p.get("shop_url") or "",
            "in_stock":      bool(p.get("in_stock", False)),
            "is_active":     bool(p.get("is_active", False)),
            "variants":      variants,
        })


# ── Preis-Tracking ────────────────────────────────────────────────────────────

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

CREATE TABLE IF NOT EXISTS variant_price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id   INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    price        REAL    NOT NULL,
    currency_iso TEXT    NOT NULL DEFAULT 'EUR',
    recorded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vph_variant
    ON variant_price_history(variant_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS variant_snapshot (
    product_id INTEGER NOT NULL,
    variant_id INTEGER NOT NULL,
    title      TEXT,
    price      REAL,
    PRIMARY KEY (product_id, variant_id)
);

CREATE TABLE IF NOT EXISTS product_price_reason (
    product_id    INTEGER PRIMARY KEY,
    recorded_at   TEXT,
    direction     TEXT,
    code          TEXT,
    variant_title TEXT,
    old_price     REAL,
    new_price     REAL,
    currency_iso  TEXT
);
"""

def _instock_variants(product: dict) -> dict:
    """{variant_id: (title, price)} nur lagernde, aktive Varianten mit Preis>0."""
    out = {}
    for v in product.get("variants", []):
        if not (v.get("in_stock") and v.get("is_active")):
            continue
        vid = v.get("id")
        if vid is None:
            continue
        try:
            price = float(str(v.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[vid] = ((v.get("title") or v.get("description") or "").strip(), price)
    return out


def _write_snapshot(cur, pid, product) -> None:
    cur.execute("DELETE FROM variant_snapshot WHERE product_id=?", (pid,))
    rows = [(pid, vid, t, pr) for vid, (t, pr) in _instock_variants(product).items()]
    if rows:
        cur.executemany(
            "INSERT INTO variant_snapshot (product_id, variant_id, title, price) VALUES (?,?,?,?)",
            rows,
        )


def _classify_reason(cur, pid, product, old_min, old_max, new_min, new_max):
    """
    Bestimmt den Grund einer Spannen-Aenderung durch Diff des lagernden
    Varianten-Satzes (alt=Snapshot, neu=aktuell). Rueckgabe (code, title, old, new) oder None.
    """
    cur.execute("SELECT variant_id, title, price FROM variant_snapshot WHERE product_id=?", (pid,))
    old = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    if not old:
        return None
    new = _instock_variants(product)
    if not new:
        return None
    common = set(old) & set(new)
    up = (new_min + new_max) > (old_min + old_max)
    if up:
        inc = [(vid, new[vid][0], old[vid][1], new[vid][1]) for vid in common if new[vid][1] > old[vid][1] + 1e-6]
        if inc:
            vid, title, op, np = max(inc, key=lambda x: x[3] - x[2])
            return ("price_up", title, op, np)
        cheapest = min(old.items(), key=lambda kv: kv[1][1])
        if cheapest[0] not in new:
            return ("cheapest_gone", cheapest[1][0], None, None)
        newcomers = [vid for vid in new if vid not in old]
        if newcomers:
            vid = max(newcomers, key=lambda v: new[v][1])
            return ("new_expensive", new[vid][0], None, None)
        return None
    else:
        dec = [(vid, new[vid][0], old[vid][1], new[vid][1]) for vid in common if new[vid][1] < old[vid][1] - 1e-6]
        if dec:
            vid, title, op, np = min(dec, key=lambda x: x[3] - x[2])
            return ("price_down", title, op, np)
        newcomers = [vid for vid in new if vid not in old]
        if newcomers:
            vid = min(newcomers, key=lambda v: new[v][1])
            return ("new_cheaper", new[vid][0], None, None)
        dearest = max(old.items(), key=lambda kv: kv[1][1])
        if dearest[0] not in new:
            return ("expensive_gone", dearest[1][0], None, None)
        return None


def _store_reason(cur, pid, reason, currency) -> None:
    code, title, op, np = reason
    direction = "down" if code in ("price_down", "new_cheaper", "expensive_gone") else "up"
    cur.execute(
        "INSERT OR REPLACE INTO product_price_reason "
        "(product_id, recorded_at, direction, code, variant_title, old_price, new_price, currency_iso) "
        "VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?)",
        (pid, direction, code, title, op, np, currency),
    )


def _track_prices(shop_map: dict) -> tuple[int, int]:
    """
    Vergleicht aktuelle Preise mit dem letzten Eintrag in price_history.db.
    Schreibt nur einen neuen Eintrag wenn sich der Preis geaendert hat.
    Produkte mit Preis 0 werden ignoriert.
    Gibt (neue Einträge, gecheckte Produkte) zurück.
    """
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        conn.executescript(_PRICE_HISTORY_SCHEMA)
        conn.commit()
        cur = conn.cursor()

        new_entries = 0
        checked = 0
        new_variant_entries = 0
        checked_variants = 0

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

                if last is None:
                    cur.execute(
                        "INSERT INTO product_price_history "
                        "(product_id, min_price, max_price, currency_iso) VALUES (?,?,?,?)",
                        (pid, min_p, max_p, currency),
                    )
                    _write_snapshot(cur, pid, p)
                    new_entries += 1
                elif last[0] != min_p or last[1] != max_p:
                    cur.execute(
                        "INSERT INTO product_price_history "
                        "(product_id, min_price, max_price, currency_iso) VALUES (?,?,?,?)",
                        (pid, min_p, max_p, currency),
                    )
                    reason = _classify_reason(cur, pid, p, last[0], last[1], min_p, max_p)
                    if reason:
                        _store_reason(cur, pid, reason, currency)
                    _write_snapshot(cur, pid, p)
                    new_entries += 1
                else:
                    # Unveraenderte Produkte: Snapshot einmalig seeden (Erststart
                    # nach Deploy), damit schon die ERSTE kuenftige Aenderung einen
                    # Grund liefern kann. Existiert bereits einer -> Baseline behalten.
                    cur.execute("SELECT 1 FROM variant_snapshot WHERE product_id=? LIMIT 1", (pid,))
                    if cur.fetchone() is None:
                        _write_snapshot(cur, pid, p)

                # Varianten-Historie (Einzelpreise) – nur bei Preisaenderung
                for v in p.get("variants", []):
                    vid = v.get("id")
                    if vid is None:
                        continue
                    try:
                        vprice = float(v.get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if vprice == 0.0:
                        continue
                    vcur = v.get("currency_iso") or currency
                    checked_variants += 1
                    cur.execute(
                        "SELECT price FROM variant_price_history "
                        "WHERE variant_id=? ORDER BY recorded_at DESC LIMIT 1",
                        (vid,),
                    )
                    vlast = cur.fetchone()
                    if vlast is None or vlast[0] != vprice:
                        cur.execute(
                            "INSERT INTO variant_price_history "
                            "(variant_id, product_id, price, currency_iso) VALUES (?,?,?,?)",
                            (vid, pid, vprice, vcur),
                        )
                        new_variant_entries += 1

        conn.commit()
        return new_entries, checked, new_variant_entries, checked_variants
    finally:
        conn.close()


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        logger.error("❌ ANTCHECK_API_KEY ist nicht gesetzt – abbruch.")
        sys.exit(1)

    start = time.monotonic()
    logger.info(f"🚀 Starte AntCheck Grabber – Ziel: {OUTPUT_FILE}")

    try:
        # 1. Shops laden
        logger.info("🏪 Lade Shops...")
        shops_raw = _fetch_json(SHOPS_URL)
        if not isinstance(shops_raw, list):
            shops_raw = shops_raw.get("data", shops_raw.get("shops", []))
        shop_map = build_shop_map(shops_raw)
        logger.info(f"✅ {len(shop_map)} Shops gefunden")

        # 1b. Varianten global laden (nach product_id gruppiert)
        logger.info("🔖 Lade Produkt-Varianten...")
        variants_by_pid = fetch_variants_by_product()
        total_variants  = sum(len(v) for v in variants_by_pid.values())
        logger.info(f"✅ {total_variants} Varianten für {len(variants_by_pid)} Produkte")

        # 2. Produkte pro Shop laden
        total_products = 0
        for i, (shop_id, shop) in enumerate(shop_map.items(), 1):
            try:
                url = PRODUCTS_URL.format(shop_id=shop_id)
                products_raw = _fetch_json(url)
                if not isinstance(products_raw, list):
                    products_raw = products_raw.get("data", products_raw.get("products", []))
                add_products(shop_map, shop_id, products_raw, variants_by_pid)
                count = len(shop_map[shop_id]["products"])
                total_products += count
                logger.info(f"  📦 [{i}/{len(shop_map)}] Shop {shop['name']}: {count} Produkte")
            except Exception as e:
                logger.warning(f"  ⚠️ Shop {shop_id} Produkte fehlgeschlagen: {e}")

        # 3. Ausgabe schreiben
        output = {
            "_meta": {
                "fetched_at":    datetime.now(timezone.utc).isoformat(),
                "shop_count":    len(shop_map),
                "product_count": total_products,
                "variant_count": total_variants,
            },
            **shop_map,
        }
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUTPUT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUTPUT_FILE)

        # 4. Preis-History tracken
        try:
            new_entries, checked, v_new, v_checked = _track_prices(shop_map)
            logger.info(
                f"💶 Preis-Tracking: {checked} Produkte ({new_entries} neu), "
                f"{v_checked} Varianten ({v_new} neu) -> {PRICE_HISTORY_DB}"
            )
        except Exception as e:
            logger.warning(f"⚠️ Preis-Tracking fehlgeschlagen (nicht kritisch): {e}")

        elapsed = time.monotonic() - start
        logger.info(
            f"✅ Fertig: {len(shop_map)} Shops / {total_products} Produkte "
            f"-> {OUTPUT_FILE} ({elapsed:.1f}s)"
        )

    except Exception as e:
        logger.error(f"❌ Grabber fehlgeschlagen: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
