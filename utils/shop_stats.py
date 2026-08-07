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
import math
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import SHOPS_DATA_FILE, DATA_DIRECTORY
from utils.availability import is_merch_product
from utils.currency import to_eur
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


def _entry_price_eur(p: dict) -> float | None:
    """Einstiegspreis eines Angebots in EUR: der NIEDRIGSTE positive Variantenpreis
    (0,00-Platzhalter für ausverkaufte Größen werden ignoriert). Gibt es keine
    positive Variante, Fallback auf min_price/max_price. None, wenn kein Kurs/Preis."""
    cur_p = p.get("currency_iso") or "EUR"
    best = None
    for v in (p.get("variants") or []):
        pv = _num(v.get("price"))
        if pv and pv > 0:
            e = to_eur(pv, v.get("currency_iso") or cur_p)
            if e and e > 0:
                best = e if best is None else min(best, e)
    if best is None:
        for fld in ("min_price", "max_price"):
            pv = _num(p.get(fld))
            if pv and pv > 0:
                e = to_eur(pv, cur_p)
                if e and e > 0:
                    best = e
                    break
    return best


def _median(xs: list) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2


def _percentile(sorted_xs: list, q: float) -> float:
    """Linear interpoliertes Perzentil (q in 0..100). Eingabe MUSS sortiert sein."""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return float(sorted_xs[0])
    idx = (len(sorted_xs) - 1) * q / 100.0
    lo = int(idx)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (idx - lo)


def _nice_bin(cap: float, target: int = 20) -> float:
    """„Runde" Bin-Breite, sodass ~target Balken bis cap entstehen."""
    raw = cap / target if cap > 0 else 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if step >= raw:
            return step
    return 1000


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
    genus_offers: Counter = Counter()               # Gattung -> Zahl der Angebote
    species_shops: dict[str, set] = defaultdict(set)  # Art -> Menge der Shops
    shop_name: dict = {}                             # Shop-ID -> Anzeigename
    shop_offers: Counter = Counter()                # Shop-ID -> Ameisen-Angebote
    shop_species: dict[str, set] = defaultdict(set)  # Shop-ID -> Menge Arten
    all_prices: list = []                            # Einstiegspreise (EUR) aller Angebote
    genus_prices: dict[str, list] = defaultdict(list)   # Gattung -> EUR-Preise
    species_prices: dict[str, list] = defaultdict(list)  # Art -> EUR-Preise

    for s in shops:
        ps = s.get("products") or []
        if ps:
            shops_with_products += 1
        shop_id = s.get("id") or s.get("name") or id(s)
        shop_name[shop_id] = s.get("name") or str(shop_id)
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
            shop_offers[shop_id] += 1
            cs = (p.get("canonical_species") or "").strip()
            if cs:
                canon_species.add(cs.lower())
                genus = cs.split()[0]
                genera.add(genus)
                genus_offers[genus] += 1
                species_shops[cs].add(shop_id)
                shop_species[shop_id].add(cs)
            # Einstiegspreis (niedrigster positiver Variantenpreis) in EUR.
            eur = _entry_price_eur(p)
            if eur is not None and eur > 0:
                all_prices.append(eur)
                if cs:
                    genus_prices[genus].append(eur)
                    species_prices[cs].append(eur)
            if _in_stock(p):
                instock_live += 1

    instock_pct = round(100 * instock_live / live_products, 1) if live_products else 0.0
    countries_sorted = sorted(countries.items(), key=lambda kv: (-kv[1], kv[0]))

    # ── Block 2: Arten & Gattungen ──────────────────────────────────────────
    genera_ranked = genus_offers.most_common()          # [(Gattung, Angebote)] absteigend
    reach = sorted(species_shops.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top_reach = [(sp, len(sh)) for sp, sh in reach[:10]]
    rarities = sorted(sp for sp, sh in species_shops.items() if len(sh) == 1)
    longtail = Counter(len(sh) for sh in species_shops.values())  # k Shops -> Zahl Arten
    longtail_ranked = sorted(longtail.items())           # [(Shops, Artenzahl)] aufsteigend

    # ── Block 3: Shop-Vergleich ─────────────────────────────────────────────
    exclusive: Counter = Counter()                       # Shop-ID -> Zahl exklusiver Arten
    for sp, sh in species_shops.items():
        if len(sh) == 1:
            exclusive[next(iter(sh))] += 1
    shops_by_offers = sorted(((shop_name[i], c) for i, c in shop_offers.items()),
                             key=lambda x: (-x[1], x[0]))
    shops_by_breadth = sorted(((shop_name[i], len(sp)) for i, sp in shop_species.items()),
                              key=lambda x: (-x[1], x[0]))
    shops_by_exclusive = sorted(((shop_name[i], exclusive.get(i, 0)) for i in shop_offers),
                                key=lambda x: (-x[1], x[0]))
    scatter = [{"shop": shop_name[i], "species": len(shop_species.get(i, ())), "offers": c}
               for i, c in shop_offers.items()]

    # ── Block 4: Preise (alles in EUR) ──────────────────────────────────────
    prices_sorted = sorted(all_prices)
    price_stats = {"n": len(prices_sorted)}
    hist = {"labels": [], "counts": []}
    genus_median = []
    spread = []
    if prices_sorted:
        price_stats.update({
            "median": round(_median(prices_sorted), 2),
            "mean": round(sum(prices_sorted) / len(prices_sorted), 2),
            "p25": round(_percentile(prices_sorted, 25), 2),
            "p75": round(_percentile(prices_sorted, 75), 2),
            "min": round(prices_sorted[0], 2),
            "max": round(prices_sorted[-1], 2),
        })
        # Histogramm: bis zum 99. Perzentil, Rest in einen „≥ X"-Balken bündeln.
        cap = _percentile(prices_sorted, 99)
        binw = _nice_bin(cap)
        nbins = max(1, int(math.ceil(cap / binw))) if cap > 0 else 1
        counts = [0] * (nbins + 1)                    # letzter Eintrag = Überlauf (≥ nbins*binw)
        for p in prices_sorted:
            b = int(p // binw)
            counts[b if b < nbins else nbins] += 1
        labels = [f"{int(k * binw)}–{int((k + 1) * binw)}" for k in range(nbins)]
        labels.append(f"≥ {int(nbins * binw)}")
        hist = {"labels": labels, "counts": counts}
        # Median-Preis je Top-10-Gattung (Reihenfolge = Angebots-Ranking)
        genus_median = [(g, round(_median(genus_prices.get(g, [])), 2))
                        for g, _ in genera_ranked[:10] if genus_prices.get(g)]
        # Größte Preisspanne je Art (nur Arten in ≥ 2 Shops), Top 10 nach Spanne
        for sp, pl in species_prices.items():
            if len(species_shops.get(sp, ())) >= 2 and pl:
                mn, mx = min(pl), max(pl)
                spread.append((sp, round(mn, 2), round(mx, 2), round(mx - mn, 2),
                               len(species_shops[sp])))
        spread.sort(key=lambda x: -x[3])
        spread = spread[:10]

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
        "species": {
            "genera": genera_ranked,             # [(Gattung, Angebote)]
            "reach": top_reach,                  # [(Art, Shop-Anzahl)] Top 10
            "rarities_count": len(rarities),
            "rarities_sample": rarities[:60],    # Arten in nur 1 Shop (Auszug)
            "longtail": longtail_ranked,         # [(Shops, Artenzahl)]
        },
        "shops": {
            "by_offers": shops_by_offers,        # [(Shop, Angebote)]
            "by_breadth": shops_by_breadth,      # [(Shop, versch. Arten)]
            "by_exclusive": shops_by_exclusive,  # [(Shop, exkl. Arten)]
            "scatter": scatter,                  # [{shop, species, offers}] alle Shops
        },
        "prices": {
            "stats": price_stats,                # n, median, mean, p25, p75, min, max (EUR)
            "hist": hist,                        # {labels, counts}
            "genus_median": genus_median,        # [(Gattung, Median-EUR)] Top 10
            "spread": spread,                    # [(Art, min, max, spanne, shops)] Top 10
        },
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
