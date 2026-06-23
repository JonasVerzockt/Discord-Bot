"""
grabber.py – AntCheck API Datenabholer.

Wird als Cron-Job oder per Hand ausgeführt (NICHT Teil des Bots selbst).
Lädt die aktuelle Verfügbarkeitsliste von der AntCheck API herunter
und speichert sie als shops_data.json im DATA_DIRECTORY.

Typischer Aufruf (crontab):
  */5 * * * * cd /opt/antcheckbot && /opt/antcheckbot/.venv/bin/python grabber.py

Umgebungsvariablen (aus .env oder Shell):
  DATA_DIRECTORY   – Zielverzeichnis für shops_data.json
  ANTCHECK_API_URL – Basis-URL der AntCheck API (optional, hat Standardwert)
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("grabber")

# ── Konfiguration ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATA_DIRECTORY  = os.getenv("DATA_DIRECTORY", str(Path(__file__).parent))
OUTPUT_FILE     = Path(DATA_DIRECTORY) / "shops_data.json"
API_BASE        = os.getenv(
    "ANTCHECK_API_URL",
    "https://api.antcheck.info",
)
API_OFFERS      = f"{API_BASE}/offers"
API_TIMEOUT     = int(os.getenv("ANTCHECK_TIMEOUT", "30"))
API_RETRIES     = int(os.getenv("ANTCHECK_RETRIES", "3"))
API_RETRY_DELAY = float(os.getenv("ANTCHECK_RETRY_DELAY", "5"))


def _fetch_json(url: str, timeout: int = API_TIMEOUT) -> dict | list:
    """Holt JSON von der URL mit Retry-Logik."""
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"Versuch {attempt}/{API_RETRIES} fehlgeschlagen: {e}")
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)
    raise RuntimeError(f"API nach {API_RETRIES} Versuchen nicht erreichbar: {url}")


def transform_offers(raw: list[dict]) -> dict:
    """
    Wandelt die AntCheck API-Antwort in das Format um, das der Bot erwartet:
    {
      "<shop_id>": {
        "id":             "<shop_id>",
        "name":           "Shop Name",
        "country":        "de",
        "url":            "https://...",
        "average_rating": 8.5,
        "products": [
          {
            "id":          123,
            "species":     "Messor barbarus",
            "genus":       "Messor",
            "min_price":   "12.90",
            "max_price":   "15.00",
            "currency_iso":"EUR",
            "antcheck_url":"https://...",
            "shop_url":    "https://...",
          },
          ...
        ]
      }
    }
    """
    shops: dict = {}
    for offer in raw:
        try:
            shop_id  = str(offer.get("shopId") or offer.get("shop_id") or "")
            if not shop_id:
                continue

            if shop_id not in shops:
                shops[shop_id] = {
                    "id":             shop_id,
                    "name":           offer.get("shopName") or offer.get("shop_name", ""),
                    "country":        (offer.get("shopCountry") or offer.get("country", "")).lower(),
                    "url":            offer.get("shopUrl") or offer.get("shop_url", ""),
                    "average_rating": offer.get("shopRating") or offer.get("average_rating"),
                    "products":       [],
                }

            species_name = (
                offer.get("speciesName")
                or offer.get("species_name")
                or offer.get("name")
                or ""
            ).strip()
            genus = species_name.split()[0] if " " in species_name else species_name

            shops[shop_id]["products"].append({
                "id":           offer.get("id"),
                "species":      species_name,
                "genus":        genus,
                "min_price":    str(offer.get("minPrice") or offer.get("min_price") or "0"),
                "max_price":    str(offer.get("maxPrice") or offer.get("max_price") or "0"),
                "currency_iso": offer.get("currencyIso") or offer.get("currency_iso") or "EUR",
                "antcheck_url": offer.get("antcheckUrl") or offer.get("antcheck_url") or "",
                "shop_url":     offer.get("productUrl") or offer.get("product_url") or "",
            })
        except Exception as e:
            logger.warning(f"Fehler beim Verarbeiten von Angebot: {e} – {offer!r}")

    return shops


def main():
    start = time.monotonic()
    logger.info(f"Starte AntCheck Grabber – Ziel: {OUTPUT_FILE}")

    try:
        raw     = _fetch_json(API_OFFERS)
        offers  = raw if isinstance(raw, list) else raw.get("offers", raw.get("data", []))
        shops   = transform_offers(offers)

        # Metadaten hinzufügen
        output = {
            "_meta": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "shop_count": len(shops),
                "offer_count": len(offers),
            },
            **shops,
        }

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUTPUT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUTPUT_FILE)  # atomarer Schreibvorgang

        elapsed = time.monotonic() - start
        logger.info(
            f"✅ {len(shops)} Shops / {len(offers)} Angebote → {OUTPUT_FILE} "
            f"({elapsed:.1f}s)"
        )
    except Exception as e:
        logger.error(f"Grabber fehlgeschlagen: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
