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
utils/shop_stats.py – Aggregation der Shop-/Produkt-Statistiken für die /stats-Seite
des Feedback-Boards (cogs/board.py).

Datenquellen (nur lesend):
  • SHOPS_DATA_FILE (shops_data.json) – vom Grabber stündlich erzeugt.
  • price_history.db – Preis-Historie (für spätere Zeitverlauf-Blöcke).

Alle Zahlen sind SPRACHUNABHÄNGIG (reine Werte/Schlüssel); die Beschriftung
übernimmt das Template via utils/board_i18n. Ergebnis wird bis zu 15 Minuten im
Speicher gecacht (der Grabber läuft stündlich, häufiger Neuberechnen bringt nichts).

Währungsumrechnung nach EUR über utils/currency.to_eur (dieselbe Logik wie im Bot:
EZB/Frankfurter + Fallback). Voraussetzung: der Aufrufer hat vorher
``await utils.currency.ensure_rates()`` ausgeführt (macht der Board-Handler).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import SHOPS_DATA_FILE, DATA_DIRECTORY
from utils.availability import is_merch_product
from utils.timez import BERLIN

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"

_TTL = 15 * 60          # Sekunden
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


# ── kleine Helfer ──────────────────────────────────────────────────────────────
def _num(x):
    """Robuste Float-Konvertierung ('34,49' / '34.49' / None) -> float | None."""
    try:
        v = float(str(x).replace(",", "."))
        return v
    except (TypeError, ValueError):
        return None


def _iter_shops(d: dict):
    """Iteriert über die Shop-Dicts (überspringt den _meta-Eintrag)."""
    for k, v in d.items():
        if k == "_meta" or not isinstance(v, dict):
            continue
        yield v


def _in_stock(p: dict) -> bool:
    return bool(p.get("in_stock") and p.get("is_active"))


def _fmt_ts(iso: str | None) -> str:
    """ISO-Zeitstempel -> 'YYYY-MM-DD HH:MM MEZ/MESZ' in Berliner Zeit (ohne Sekunden).
    Zeitzonenlose Werte werden als UTC interpretiert. Label je nach Sommerzeit."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(BERLIN)
        label = "MESZ" if local.dst() else "MEZ"
        return local.strftime("%Y-%m-%d %H:%M ") + label
    except (ValueError, TypeError):
        return str(iso)[:16].replace("T", " ")


# ── Aggregation ────────────────────────────────────────────────────────────────
def _compute(d: dict) -> dict:
    """Reine Aggregation über die geladene shops_data-Struktur (ohne I/O)."""
    meta = d.get("_meta", {}) if isinstance(d.get("_meta"), dict) else {}
    shops = list(_iter_shops(d))

    shops_total = len(shops)
    shops_with_products = 0
    products_total = live_products = merch_products = instock_live = 0
    canon_species: set[str] = set()
    genera: set[str] = set()
    countries: dict[str, int] = {}

    for s in shops:
        ps = s.get("products") or []
        if ps:
            shops_with_products += 1
        c = (s.get("country") or "??").lower()
        countries[c] = countries.get(c, 0) + 1
        for p in ps:
            products_total += 1
            # Merch/Zubehör (Sticker, Poster, Sets …) exakt wie im Bot erkennen und
            # aus den Lebendtier-Kennzahlen ausschließen. Produkte ganz ohne Artnamen
            # zählen ebenfalls als Nicht-Lebendtier.
            species = (p.get("canonical_species") or p.get("species") or "").strip()
            if is_merch_product(p) or not species:
                merch_products += 1
                continue
            live_products += 1
            cs = (p.get("canonical_species") or "").strip()
            if cs:
                canon_species.add(cs.lower())
                genera.add(cs.split()[0])
            if _in_stock(p):
                instock_live += 1

    instock_pct = round(100 * instock_live / live_products, 1) if live_products else 0.0
    countries_sorted = sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))

    overview = {
        "shops_total": shops_total,
        "shops_with_products": shops_with_products,
        "products_total": products_total,
        "live_products": live_products,
        "merch_products": merch_products,
        "species_total": len(canon_species),
        "genera_total": len(genera),
        "instock_live": instock_live,
        "out_of_stock_live": live_products - instock_live,
        "instock_pct": instock_pct,
        "countries": countries_sorted,          # [(iso, count), …] absteigend
    }

    return {
        "meta": {
            "fetched_at": _fmt_ts(meta.get("fetched_at")),
            "generated_at": _fmt_ts(datetime.now(timezone.utc).isoformat()),
            "shop_count": meta.get("shop_count"),
            "product_count": meta.get("product_count"),
            "variant_count": meta.get("variant_count"),
        },
        "overview": overview,
    }


def compute(force: bool = False) -> dict:
    """Lädt shops_data.json und aggregiert – mit 15-min-In-Memory-Cache.
    Synchron (Datei-I/O + CPU); der Board-Handler ruft dies via asyncio.to_thread auf."""
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < _TTL:
            return _cache["data"]

    with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
        d = json.load(f)
    data = _compute(d)

    with _lock:
        _cache["data"] = data
        _cache["at"] = now
    return data
