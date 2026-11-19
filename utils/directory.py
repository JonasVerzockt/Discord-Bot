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
utils/directory.py – Daten für Artensteckbrief (/art) und Händlerseite (/shop),
gemeinsam genutzt vom Discord-Cog (cogs/directory.py) und vom Board (cogs/board.py).

Quellen (nur lesend):
  • shops_data.json      – Angebote, Preise, Bestand (Grabber, stündlich)
  • price_history.db     – Preisverlauf je Produkt
  • Bewertungs-Sheet     – nur aus dem bereits geladenen SheetCache (kein eigener Abruf)

Schlüssel: Shops über die Domain (ohne https:// und www., wie in der Bewertungstabelle),
Arten über den kanonischen Namen in Kleinschreibung.

Alles synchron; Aufrufer nutzen asyncio.to_thread. Vorher utils.currency.ensure_rates()
ausführen (EUR-Umrechnung). Index wird 15 Minuten gecacht.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from config import SHOPS_DATA_FILE, DATA_DIRECTORY
from utils.availability import is_merch_product
from utils.shop_stats import _entry_price_eur, _is_unspecific, _iter_shops
from utils.shop import clean_shop_name

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"
_TTL = 15 * 60
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}

# Mindestzahl Shops je Art für den Preisniveau-Vergleich eines Shops
_LEVEL_MIN_SHOPS = 3


def domain_of(url_or_name: str) -> str:
    """'https://www.antstore.net/shop/de/' → 'antstore.net' (inkl. SHOP_ALIASES)."""
    return clean_shop_name(url_or_name or "").lower()


def is_domain(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}", s or ""))


def slug(species: str) -> str:
    """'Camponotus nicobarensis' → 'Camponotus_nicobarensis' (für URLs)."""
    return re.sub(r"\s+", "_", (species or "").strip())


def unslug(s: str) -> str:
    return re.sub(r"[_+]+", " ", s or "").strip()


# ── Index aus shops_data.json ─────────────────────────────────────────────────
def _orig_price(p: dict) -> tuple[float | None, str]:
    """Niedrigster positiver Preis in der Originalwährung (für Anzeige ohne Kurs)."""
    from utils.shop_stats import _num
    cur = (p.get("currency_iso") or "EUR").upper()
    vals = []
    variants = p.get("variants") or []
    active = [v for v in variants if v.get("is_active", True)]
    for v in (active or variants):
        x = _num(v.get("price"))
        if x and x > 0:
            vals.append((x, (v.get("currency_iso") or cur).upper()))
    if not vals:
        for fld in ("min_price", "max_price"):
            x = _num(p.get(fld))
            if x and x > 0:
                vals.append((x, cur))
                break
    if not vals:
        return None, cur
    x, c = min(vals)
    return round(x, 2), c


def fmt_price(o: dict) -> str:
    """'23,50 €' · fremde Währung: '23,50 € (34,49 CAD)' · ohne Kurs: '34,49 CAD'."""
    def f(v, c):
        return f"{v:.2f}".replace(".", ",") + (" €" if c == "EUR" else f" {c}")
    eur, orig, cur = o.get("price"), o.get("price_orig"), o.get("currency") or "EUR"
    if eur is not None and (cur == "EUR" or orig is None):
        return f(eur, "EUR")
    if eur is not None:
        return f"{f(eur, 'EUR')} ({f(orig, cur)})"
    return f(orig, cur) if orig is not None else "–"


def _build(d: dict) -> dict:
    shops: dict[str, dict] = {}
    species: dict[str, dict] = {}
    fx_missing = 0
    for s in _iter_shops(d):
        dom = domain_of(s.get("url") or "") or (s.get("name") or "").strip().lower()
        if not dom:
            continue
        sh = shops.setdefault(dom, {
            "domain": dom, "name": (s.get("name") or dom).strip(),
            "url": s.get("url") or f"https://{dom}/", "country": (s.get("country") or "").lower(),
            "offers": [], "species": set(),
        })
        for p in s.get("products") or []:
            if not p.get("is_active") or is_merch_product(p):
                continue
            cs_raw = (p.get("canonical_species") or "").strip()
            name = cs_raw or (p.get("species") or "").strip()
            if not name:
                continue
            key = cs_raw.lower() if cs_raw else ""
            eur = _entry_price_eur(p)
            orig, cur = _orig_price(p)
            if eur is None and orig is not None:
                fx_missing += 1                     # Kurs fehlte beim Aufbau
            offer = {
                "pid": p.get("id"), "species": name, "key": key,
                "shop": sh["name"], "domain": dom,
                "price": round(eur, 2) if eur else None,
                "price_orig": orig, "currency": cur,
                "in_stock": bool(p.get("in_stock") and p.get("is_active")),
                "url": p.get("shop_url") or p.get("antcheck_url") or sh["url"],
            }
            sh["offers"].append(offer)
            if key and not _is_unspecific(key):
                sh["species"].add(key)
                sp = species.setdefault(key, {"key": key, "name": cs_raw,
                                              "genus": cs_raw.split()[0], "offers": []})
                sp["offers"].append(offer)

    # Seltenheit + Preisniveau
    for sp in species.values():
        sp["shops"] = sorted({o["domain"] for o in sp["offers"]})
    for sh in shops.values():
        sh["exclusive"] = sorted(species[k]["name"] for k in sh["species"]
                                 if len(species[k]["shops"]) == 1)
        ratios = []
        for k in sh["species"]:
            per_shop: dict[str, float] = {}
            for o in species[k]["offers"]:
                if o["price"]:
                    per_shop[o["domain"]] = min(o["price"], per_shop.get(o["domain"], o["price"]))
            if len(per_shop) >= _LEVEL_MIN_SHOPS and sh["domain"] in per_shop:
                med = median(per_shop.values())
                if med > 0:
                    ratios.append(per_shop[sh["domain"]] / med)
        sh["price_level"] = round((median(ratios) - 1) * 100) if ratios else None
        sh["price_level_n"] = len(ratios)
        sh["species_count"] = len(sh["species"])
        sh["offers_count"] = len(sh["offers"])
        sh["instock_count"] = sum(1 for o in sh["offers"] if o["in_stock"])
        sh["species"] = sorted(species[k]["name"] for k in sh["species"])
    meta = d.get("_meta", {}) if isinstance(d.get("_meta"), dict) else {}
    return {"shops": shops, "species": species, "fetched_at": meta.get("fetched_at"),
            "fx_missing": fx_missing}


def index(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        data = _cache["data"]
        # Fehlten beim letzten Aufbau Wechselkurse, nach 60 s neu rechnen statt 15 min
        ttl = 60 if (data and data.get("fx_missing")) else _TTL
        if not force and data is not None and now - _cache["at"] < ttl:
            return data
    try:
        with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    data = _build(d)
    with _lock:
        _cache.update(at=now, data=data)
    return data


# ── Suche ─────────────────────────────────────────────────────────────────────
def find_species(query: str) -> dict | None:
    """Exakter Treffer (auch über AntCat-Synonym) im Angebots-Index."""
    q = unslug(query).lower()
    idx = index()["species"]
    if q in idx:
        return idx[q]
    try:
        from utils import species_catalog
        c = species_catalog.canonical(q)
        if c and c.lower() in idx:
            return idx[c.lower()]
    except Exception:
        pass
    return None


def canonical_name(query: str) -> str | None:
    """Gültiger AntCat-Name, auch wenn die Art gerade nirgends angeboten wird."""
    try:
        from utils import species_catalog
        return species_catalog.canonical(unslug(query))
    except Exception:
        return None


def search_species(query: str, limit: int = 25) -> list[str]:
    q = unslug(query).lower()
    names = [v["name"] for v in index()["species"].values()]
    if not q:
        return sorted(names)[:limit]
    starts = sorted(n for n in names if n.lower().startswith(q))
    contains = sorted(n for n in names if q in n.lower() and not n.lower().startswith(q))
    return (starts + contains)[:limit]


def all_shops(reviews: dict | None = None) -> dict:
    """Shops aus shops_data PLUS Shops, die nur in der Bewertungstabelle stehen."""
    out = dict(index()["shops"])
    for dom, r in (reviews or {}).items():
        if dom not in out and is_domain(dom):
            out[dom] = {"domain": dom, "name": dom, "url": f"https://{dom}/", "country": "",
                        "offers": [], "species": [], "exclusive": [], "price_level": None,
                        "price_level_n": 0, "species_count": 0, "offers_count": 0,
                        "instock_count": 0}
    return out


def find_shop(query: str, reviews: dict | None = None) -> dict | None:
    q = (query or "").strip().lower()
    shops = all_shops(reviews)
    dom = domain_of(q)
    if dom in shops:
        return shops[dom]
    for sh in shops.values():
        if sh["name"].lower() == q:
            return sh
    return None


def search_shops(query: str, reviews: dict | None = None, limit: int = 25) -> list[tuple[str, str]]:
    """[(Anzeigename, Domain)]"""
    def norm(t: str) -> str:
        t = (t or "").lower()
        for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            t = t.replace(a, b)
        return re.sub(r"[^a-z0-9]", "", t)
    q = norm(query)
    items = sorted(((sh["name"], d) for d, sh in all_shops(reviews).items()),
                   key=lambda x: x[0].lower())
    if q:
        items = [x for x in items if q in norm(x[0]) or q in norm(x[1])]
    return items[:limit]


# ── Bewertungen (aus dem geladenen Sheet) ─────────────────────────────────────
def reviews_by_shop(rows: list | None) -> dict:
    """{domain: {count, avg, last, years:{YYYY:(n, avg)}, dist:{1..10:n}, recent:[…]}}
    Spalten: A Datum, C Shop, G Bewertung, H Positiv, I Negativ."""
    out: dict = {}
    acc: dict = defaultdict(list)
    for r in (rows or [])[1:]:
        if len(r) < 7 or not (r[0] or "").strip():
            continue
        try:
            dt = datetime.strptime(r[0].strip(), "%d.%m.%Y")
        except ValueError:
            continue
        dom = domain_of(r[2])
        if not dom:
            continue
        try:
            val = float(str(r[6]).replace(",", "."))
        except ValueError:
            val = None
        acc[dom].append((dt, val, (r[7] if len(r) > 7 else "").strip(),
                         (r[8] if len(r) > 8 else "").strip()))
    for dom, lst in acc.items():
        lst.sort(key=lambda x: x[0])
        vals = [v for _, v, _, _ in lst if v is not None]
        years: dict = defaultdict(list)
        dist: dict = defaultdict(int)
        for dt, v, _, _ in lst:
            if v is not None:
                years[dt.year].append(v)
                dist[int(round(v))] += 1
        out[dom] = {
            "count": len(vals),
            "avg": round(sum(vals) / len(vals), 2) if vals else None,
            "last": lst[-1][0].strftime("%d.%m.%Y"),
            "first": lst[0][0].strftime("%d.%m.%Y"),
            "years": {y: (len(v), round(sum(v) / len(v), 2)) for y, v in sorted(years.items())},
            "dist": dict(sorted(dist.items())),
            "recent": [{"date": dt.strftime("%d.%m.%Y"), "rating": v, "pos": p, "neg": n}
                       for dt, v, p, n in reversed(lst[-5:])],
        }
    return out


def loaded_reviews() -> dict:
    """Bewertungen aus dem bereits geladenen SheetCache – nie ein eigener Netzabruf."""
    try:
        from utils.sheet import sheet
        return reviews_by_shop(getattr(sheet, "_rows", None))
    except Exception:
        return {}


# ── Preisverlauf einer Art ────────────────────────────────────────────────────
def price_trend(sp: dict, months: int = 12) -> dict:
    """Günstigster Preis (EUR) der Art: heute, vor 3 und vor 12 Monaten + Monatsreihe.
    Nur Produkte, die heute noch gelistet sind (wie /price_history)."""
    from utils.currency import to_eur
    pids = [o["pid"] for o in sp.get("offers", []) if o.get("pid") is not None]
    out = {"now": None, "m3": None, "m12": None, "labels": [], "values": []}
    instock = [o["price"] for o in sp.get("offers", []) if o["in_stock"] and o["price"]]
    out["now"] = min(instock) if instock else None
    if not pids or not PRICE_HISTORY_DB.exists():
        return out
    con = sqlite3.connect(f"file:{PRICE_HISTORY_DB}?mode=ro", uri=True)
    try:
        q = ",".join("?" * len(pids))
        rows = con.execute(
            f"SELECT product_id, recorded_at, min_price, currency_iso FROM product_price_history "
            f"WHERE product_id IN ({q}) ORDER BY recorded_at", pids).fetchall()
    finally:
        con.close()
    series: dict = defaultdict(list)          # pid -> [(dt, eur)]
    for pid, ts, mn, cur in rows:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", ""))
        except ValueError:
            continue
        e = to_eur(float(mn), cur or "EUR") if mn and float(mn) > 0 else None
        series[pid].append((dt, e))

    def cheapest_at(t: datetime):
        best = None
        for pts in series.values():
            val = None
            for dt, e in pts:
                if dt <= t:
                    val = e
                else:
                    break
            if val:
                best = val if best is None else min(best, val)
        return round(best, 2) if best else None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out["m3"] = cheapest_at(now - timedelta(days=91))
    out["m12"] = cheapest_at(now - timedelta(days=365))
    for i in range(months, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        end = datetime(y + (m == 12), (m % 12) + 1, 1) - timedelta(seconds=1)
        end = min(end, now)
        out["labels"].append(f"{m:02d}/{y % 100:02d}")
        out["values"].append(cheapest_at(end))
    while out["values"] and out["values"][0] is None:      # leere Monate vorne weg
        out["values"].pop(0)
        out["labels"].pop(0)
    return out


# ── Rabattcodes / Partner / Warnhinweise je Shop ──────────────────────────────
async def active_codes(bot, dom: str) -> list[dict]:
    """Gültige Rabattcodes eines Shops (gleiche Logik wie /codes)."""
    from datetime import date
    from utils.db import execute_db
    from cogs.discount_codes import _state, _UNDATED_MAX_AGE_DAYS, _domain
    rows = await execute_db(
        bot, "SELECT shop, shop_url, code, discount, valid_until, is_permanent, min_order, "
             "status_override, message_date FROM discount_codes WHERE lower(shop_url) LIKE ?",
        (f"%{dom}%",), fetch=True) or []
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=_UNDATED_MAX_AGE_DAYS)).isoformat()
    best: dict = {}
    for r in sorted(rows, key=lambda r: r["message_date"] or "", reverse=True):
        if domain_of(_domain(r["shop_url"])) != dom or _state(r, today, cutoff) != "valid":
            continue
        k = r["code"].lower()
        if k in best:
            b = best[k]
            b["discount"] = b["discount"] or (r["discount"] or "")
            b["min_order"] = b["min_order"] or r["min_order"]
            continue
        best[k] = {"code": r["code"], "discount": r["discount"] or "",
                   "min_order": r["min_order"], "permanent": bool(r["is_permanent"]),
                   "until": r["valid_until"]}
    return sorted(best.values(), key=lambda c: c["code"].lower())


def partner_of(dom: str) -> dict | None:
    try:
        from cogs.board import _load_partners
        for p in _load_partners():
            if domain_of(p.get("url", "")) == dom:
                return p
    except Exception:
        pass
    return None


def warnings_of(dom: str) -> list[dict]:
    try:
        from utils.sheet import get_shop_warnings
        return get_shop_warnings(url=f"https://{dom}/")
    except Exception:
        return []
