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
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import SHOPS_DATA_FILE, DATA_DIRECTORY
from utils.availability import is_merch_product, normalize_species_name
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
    genus_instock: Counter = Counter()               # Gattung -> lagernde Angebote
    country_live: Counter = Counter()                # Land -> Angebote (Lebendtiere)
    country_instock: Counter = Counter()             # Land -> lagernde Angebote
    shop_instock: Counter = Counter()                # Shop -> lagernde Angebote
    species_offers: Counter = Counter()              # Art -> Angebote
    species_instock: Counter = Counter()             # Art -> lagernde Angebote
    q_with = q_uncanon = q_adjusted = 0              # Datenqualität: gesamt-Zähler
    shop_canon: Counter = Counter()                  # Shop -> Angebote mit canonical
    shop_uncanon: Counter = Counter()                # Shop -> Angebote OHNE canonical
    shop_adjusted: Counter = Counter()               # Shop -> angepasste Namen (Tippf./Synonym)
    species_rawforms: dict[str, set] = defaultdict(set)  # canonical -> Menge Roh-Schreibweisen
    uncanon_raw: Counter = Counter()                 # unaufgelöster Rohname -> Häufigkeit

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
            instock = _in_stock(p)
            country_live[c] += 1
            if instock:
                instock_live += 1
                shop_instock[shop_id] += 1
                country_instock[c] += 1
            cs = (p.get("canonical_species") or "").strip()
            if cs:
                canon_species.add(cs.lower())
                genus = cs.split()[0]
                genera.add(genus)
                genus_offers[genus] += 1
                species_shops[cs].add(shop_id)
                shop_species[shop_id].add(cs)
                species_offers[cs] += 1
                if instock:
                    genus_instock[genus] += 1
                    species_instock[cs] += 1
            # Einstiegspreis (niedrigster positiver Variantenpreis) in EUR.
            eur = _entry_price_eur(p)
            if eur is not None and eur > 0:
                all_prices.append(eur)
                if cs:
                    genus_prices[genus].append(eur)
                    species_prices[cs].append(eur)
            # Datenqualität: canonical-Abdeckung, Anpassungen (Tippf./Synonym), Roh-Schreibweisen.
            raw = (p.get("species") or "").strip()
            if cs:
                q_with += 1
                shop_canon[shop_id] += 1
                nr = normalize_species_name(raw)
                species_rawforms[cs].add(nr or raw.lower())
                if nr and nr != cs.lower():      # echte Korrektur (kein reines cf./sp.-Entfernen)
                    q_adjusted += 1
                    shop_adjusted[shop_id] += 1
            else:
                q_uncanon += 1
                shop_uncanon[shop_id] += 1
                key = raw or (p.get("title") or "").strip()
                if key:
                    uncanon_raw[key] += 1

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
    spread_small = []
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
        # Preisspanne je Art (nur Arten in ≥ 5 Shops): größte UND kleinste
        spread_all = []
        for sp, pl in species_prices.items():
            if len(species_shops.get(sp, ())) >= 5 and pl:
                mn, mx = min(pl), max(pl)
                spread_all.append((sp, round(mn, 2), round(mx, 2), round(mx - mn, 2),
                                   len(species_shops[sp])))
        spread = sorted(spread_all, key=lambda x: -x[3])[:10]              # größte Spanne
        # kleinste ECHTE Spanne (> 0): perfekt identische Preise (Δ 0) ignorieren
        spread_small = sorted([s for s in spread_all if s[3] > 0],
                              key=lambda x: (x[3], x[0]))[:10]

    # ── Block 5: Verfügbarkeit (Lagerquoten, Snapshot) ──────────────────────
    def _rate(num, den):
        return round(100 * num / den, 1) if den else 0.0
    avail_genus = [(g, _rate(genus_instock.get(g, 0), genus_offers[g]))
                   for g, _ in genera_ranked[:10]]
    avail_country = sorted(
        [(c, _rate(country_instock.get(c, 0), country_live[c]), country_live[c])
         for c in country_live if country_live[c] >= 20],
        key=lambda x: -x[1])[:10]
    shop_rates = [(shop_name[i], _rate(shop_instock.get(i, 0), shop_offers[i]), shop_offers[i])
                  for i in shop_offers if shop_offers[i] >= 20]
    shop_best = sorted(shop_rates, key=lambda x: (-x[1], -x[2]))[:10]
    shop_worst = sorted(shop_rates, key=lambda x: (x[1], -x[2]))[:10]
    hardest = sorted(
        [(sp, _rate(species_instock.get(sp, 0), species_offers[sp]),
          len(species_shops[sp]), species_offers[sp])
         for sp in species_offers if len(species_shops.get(sp, ())) >= 5],
        key=lambda x: (x[1], -x[2]))[:10]

    # ── Block 6: Datenqualität (canonical) ──────────────────────────────────
    q_live = q_with + q_uncanon
    quality = {
        "coverage_pct": _rate(q_with, q_live),
        "with_canon": q_with,
        "uncanon": q_uncanon,
        "adjusted": q_adjusted,
        "adjusted_pct": _rate(q_adjusted, q_with),
        "shop_uncanon": sorted(((shop_name[i], shop_uncanon[i]) for i in shop_uncanon),
                               key=lambda x: -x[1])[:10],
        "shop_adjusted": sorted(
            [(shop_name[i], _rate(shop_adjusted.get(i, 0), shop_canon.get(i, 0)),
              shop_adjusted.get(i, 0), shop_canon.get(i, 0))
             for i in shop_offers if shop_offers[i] >= 20 and shop_canon.get(i, 0) > 0],
            key=lambda x: -x[2])[:10],   # nach absoluter Anzahl angepasster Namen
        "variants": sorted(((sp, len(f)) for sp, f in species_rawforms.items() if len(f) > 1),
                           key=lambda x: (-x[1], x[0]))[:10],
        "uncanon_raw": uncanon_raw.most_common(40),
    }

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
            "spread": spread,                    # [(Art, min, max, spanne, shops)] größte
            "spread_small": spread_small,        # [(Art, min, max, spanne, shops)] kleinste
        },
        "availability": {
            "by_genus": avail_genus,             # [(Gattung, Quote%)] Top-10-Gattungen
            "by_country": avail_country,         # [(ISO, Quote%, Angebote)] ab 20 Angeboten
            "shop_best": shop_best,              # [(Shop, Quote%, Angebote)] ab 20 Angeboten
            "shop_worst": shop_worst,            # [(Shop, Quote%, Angebote)]
            "hardest": hardest,                  # [(Art, Quote%, Shops, Angebote)] ab 5 Shops
        },
        "quality": quality,                      # canonical-Abdeckung, Anpassungen, Roh-Schreibweisen
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


# ── Block 7: Zeitverläufe aus price_history.db ──────────────────────────────────
# Umschaltbarer Zeitraum: 3 / 12 Monate oder gesamte Historie.
RANGE_MONTHS = {"3": 3, "12": 12, "all": None}
_ts_lock = threading.Lock()
_ts_cache: dict = {}                                  # range_key -> {"at":, "data":}
_pid_cache: dict = {"at": 0.0, "map": None}


def _pid_species() -> dict:
    """{product_id: Artname} für lebende (Nicht-Merch-)Angebote – für Labels/Mapping
    der Preis-Historie (die nur product_id kennt). 15-min-Cache."""
    now = time.time()
    if _pid_cache["map"] is not None and now - _pid_cache["at"] < _TTL:
        return _pid_cache["map"]
    m: dict = {}
    try:
        with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        for s in _iter_shops(d):
            for p in (s.get("products") or []):
                pid = p.get("id")
                sp = (p.get("canonical_species") or p.get("species") or "").strip()
                if pid is not None and sp and not is_merch_product(p):
                    m[pid] = sp
    except Exception:
        pass
    _pid_cache["map"] = m
    _pid_cache["at"] = now
    return m


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s)).replace(tzinfo=timezone.utc)


def _month_end(y: int, m: int) -> datetime:
    return datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12 else datetime(y, m + 1, 1, tzinfo=timezone.utc)


def _months_list(months: int | None, earliest: datetime | None) -> list:
    """Liste der (Jahr, Monat)-Paare bis zum aktuellen Monat. months=None -> ab earliest."""
    now = datetime.now(timezone.utc)
    if months:
        y, m, out = now.year, now.month, []
        for _ in range(months):
            out.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        return list(reversed(out))
    if not earliest:
        return [(now.year, now.month)]
    out, y, m = [], earliest.year, earliest.month
    while (y, m) <= (now.year, now.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _carry_forward_monthly(rows, months_list, pidmap, valuefn):
    """Generisch: rows=[(pid, wert, recorded_at)] zeitlich sortiert. Trägt je Produkt
    den letzten Wert fort und liefert je Monat aggregierte Werte via valuefn(dict)."""
    current: dict = {}
    idx, N = 0, len(rows)
    series = []
    for (y, m) in months_list:
        end = _month_end(y, m)
        while idx < N and _parse_dt(rows[idx][2]) < end:
            pid, val, _ = rows[idx]
            idx += 1
            if pid in pidmap and val is not None:
                current[pid] = val
        series.append((f"{y:04d}-{m:02d}", valuefn(current), len(current)))
    return series


def _trim_leading(series: list, is_empty) -> list:
    """Entfernt FÜHRENDE datenlose Monate (bevor überhaupt Historie vorlag), damit
    Linien/Balken dort beginnen, wo echte Daten anfangen – statt irreführender Nullen.
    Innenliegende Werte bleiben erhalten."""
    i = 0
    while i < len(series) and is_empty(series[i]):
        i += 1
    return series[i:]


def _compute_timeseries(months: int | None) -> dict:
    if not PRICE_HISTORY_DB.exists():
        return {"available": False}
    pidmap = _pid_species()
    out = {"available": True, "has_stock": False}
    cutoff = None
    if months:
        now = datetime.now(timezone.utc)
        y, m = now.year, now.month - months + 1
        while m <= 0:
            m += 12
            y -= 1
        cutoff = f"{y:04d}-{m:02d}-01 00:00:00"
    try:
        conn = sqlite3.connect(f"file:{PRICE_HISTORY_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return {"available": False}
    try:
        cur = conn.cursor()

        def _q(sql, params=()):
            try:
                return cur.execute(sql, params).fetchall()
            except sqlite3.Error:
                return []

        # frühester Monat (für „gesamte Historie")
        earliest = None
        r = _q("SELECT MIN(recorded_at) FROM product_price_history")
        if r and r[0][0]:
            try:
                earliest = _parse_dt(r[0][0])
            except Exception:
                earliest = None
        months_list = _months_list(months, earliest)

        # 1) Preisentwicklung: Median-Einstiegspreis (EUR) je Monat, fortgeschrieben.
        rows = [(pid, to_eur(mn, ci or "EUR"), rec)
                for pid, mn, ci, rec in _q(
                    "SELECT product_id, min_price, currency_iso, recorded_at "
                    "FROM product_price_history ORDER BY recorded_at")]
        rows = [(pid, e, rec) for pid, e, rec in rows if e and e > 0]
        price_series = _carry_forward_monthly(
            rows, months_list, pidmap,
            lambda cur_d: round(_median(list(cur_d.values())), 2) if cur_d else 0.0)
        out["price_over_time"] = _trim_leading(price_series, lambda r: r[2] == 0)

        # 2) Preisänderungen je Monat (Senkungen vs. Erhöhungen) aus Folge-Diffs.
        chg: dict = {}
        prev: dict = {}
        for pid, mn, rec in _q("SELECT product_id, min_price, recorded_at "
                               "FROM product_price_history ORDER BY product_id, recorded_at"):
            if pid not in pidmap:
                continue
            if pid in prev and abs(mn - prev[pid]) > 1e-9:
                dt = _parse_dt(rec)
                b = chg.setdefault((dt.year, dt.month), [0, 0])
                b[0 if mn < prev[pid] else 1] += 1
            prev[pid] = mn
        changes = [(f"{y:04d}-{m:02d}", chg.get((y, m), [0, 0])[0], chg.get((y, m), [0, 0])[1])
                   for (y, m) in months_list]
        out["changes_per_month"] = _trim_leading(changes, lambda r: r[1] == 0 and r[2] == 0)

        # 3) Aktuelle größte Preis-Senkungen/-Erhöhungen (letzte Änderungs-Ursache).
        rq = ("SELECT product_id, old_price, new_price, currency_iso, recorded_at "
              "FROM product_price_reason WHERE old_price IS NOT NULL AND new_price IS NOT NULL")
        params = ()
        if cutoff:
            rq += " AND recorded_at >= ?"
            params = (cutoff,)
        drops, incs = [], []
        for pid, op, np, ci, rec in _q(rq, params):
            if pid not in pidmap or not op or op <= 0:
                continue
            pct = round((np - op) / op * 100, 1)
            # Unplausible Ausreißer aussortieren (z.B. Platzhalterpreis 1,20 € -> 1.199 €).
            if abs(pct) > 500:
                continue
            item = (pidmap[pid], to_eur(op, ci or "EUR"), to_eur(np, ci or "EUR"), pct)
            if np < op:
                drops.append(item)
            elif np > op:
                incs.append(item)

        def _dedupe(items):
            """Je Art nur den stärksten Eintrag behalten (Liste ist bereits sortiert)."""
            seen, res = set(), []
            for it in items:
                if it[0] in seen:
                    continue
                seen.add(it[0])
                res.append(it)
            return res
        drops.sort(key=lambda x: x[3])
        incs.sort(key=lambda x: -x[3])
        out["price_drops"] = _dedupe(drops)[:10]
        out["price_increases"] = _dedupe(incs)[:10]

        # 4) Verfügbarkeit über Zeit (aus Bestands-Historie; anfangs leer).
        srows = _q("SELECT product_id, in_stock, recorded_at "
                   "FROM product_stock_history ORDER BY recorded_at")
        out["has_stock"] = bool(srows)
        out["avail_over_time"] = _trim_leading(_carry_forward_monthly(
            srows, months_list, pidmap,
            lambda cur_d: round(100 * sum(cur_d.values()) / len(cur_d), 1) if cur_d else 0.0),
            lambda r: r[2] == 0) if srows else []
        return out
    finally:
        conn.close()


def compute_timeseries(range_key: str = "12") -> dict:
    """Zeitreihen aus price_history.db mit umschaltbarem Zeitraum (3/12/all),
    je Zeitraum 15-min-Cache. Synchron -> via asyncio.to_thread aufrufen."""
    if range_key not in RANGE_MONTHS:
        range_key = "12"
    now = time.time()
    with _ts_lock:
        c = _ts_cache.get(range_key)
        if c and now - c["at"] < _TTL:
            return c["data"]
    data = _compute_timeseries(RANGE_MONTHS[range_key])
    with _ts_lock:
        _ts_cache[range_key] = {"at": now, "data": data}
    return data
