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
utils/year_review.py – Jahresrückblick: Community-Summen (Post am 31.12., Board-Seite)
und persönliche Zahlen (/rueckblick, nur für den Aufrufenden sichtbar).

Community: nur Summen und Shop-/Artnamen, keine Personen. Zahlen unter 3 werden in der
Anzeige als „< 3“ dargestellt (wie Karte und Nutzungsstatistik).
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

from utils.db import execute_db
from utils import directory as D

MIN_REVIEWS_BEST = 4        # „bestbewertet“ erst ab so vielen Bewertungen im Jahr
MIN_DELIVERY = 3            # „schnellster Versand“ erst ab so vielen Angaben


async def _one(bot, sql: str, params: tuple = ()) -> int:
    try:
        r = await execute_db(bot, sql, params, fetch=True)
        return int(r[0][0] or 0) if r else 0
    except Exception:
        return 0


def _reviews(year: int) -> dict:
    """Bewertungen des Jahres aus dem geladenen Sheet: Anzahl, Ø, meistbewertet, bestbewertet."""
    try:
        from utils.sheet import sheet
        rows = getattr(sheet, "_rows", None) or []
    except Exception:
        rows = []
    per: dict[str, list] = defaultdict(list)
    total = 0
    for r in rows[1:]:
        d = (r[0] if r else "").strip()
        if len(d) != 10 or d[6:10] != str(year):
            continue
        total += 1
        dom = D.domain_of(r[2] if len(r) > 2 else "")
        try:
            per[dom].append(float(str(r[6]).replace(",", ".")))
        except (IndexError, ValueError):
            per[dom].append(None)
    vals = [v for lst in per.values() for v in lst if v is not None]
    most = sorted(((d, len(l)) for d, l in per.items() if d), key=lambda x: (-x[1], x[0]))[:3]
    best = []
    for d, l in per.items():
        v = [x for x in l if x is not None]
        if d and len(v) >= MIN_REVIEWS_BEST:
            best.append((d, round(sum(v) / len(v), 2), len(v)))
    best.sort(key=lambda x: (-x[1], -x[2]))
    return {"count": total, "avg": round(sum(vals) / len(vals), 2) if vals else None,
            "most": most, "best": best[:3]}


def _price_drops(year: int, limit: int = 5) -> list:
    """Größte Preisstürze des Jahres (heute noch gelistete Angebote): erster Preis im Jahr
    gegenüber dem aktuellen Preis, in EUR. [(Art, Shop, von, auf, Prozent)]"""
    from utils.currency import to_eur
    from utils.directory import PRICE_HISTORY_DB
    idx = D.index()
    offers = {o["pid"]: o for sp in idx["species"].values() for o in sp["offers"]
              if o.get("pid") is not None and o["price"]}
    if not offers or not PRICE_HISTORY_DB.exists():
        return []
    con = sqlite3.connect(f"file:{PRICE_HISTORY_DB}?mode=ro", uri=True)
    try:
        q = ",".join("?" * len(offers))
        rows = con.execute(
            f"SELECT product_id, min_price, currency_iso FROM product_price_history "
            f"WHERE product_id IN ({q}) AND substr(recorded_at,1,4)=? AND min_price > 0 "
            f"ORDER BY recorded_at", (*offers.keys(), str(year))).fetchall()
    finally:
        con.close()
    first: dict = {}
    for pid, mn, cur in rows:
        if pid not in first:
            first[pid] = to_eur(float(mn), cur or "EUR")
    out = []
    for pid, start in first.items():
        now = offers[pid]["price"]
        if start and now and now < start:
            pct = round((start - now) / start * 100)
            if pct >= 10:
                out.append((offers[pid]["species"], offers[pid]["shop"], round(start, 2), now, pct))
    out.sort(key=lambda x: -x[4])
    seen, res = set(), []
    for x in out:                                   # je Art nur einmal
        if x[0] not in seen:
            seen.add(x[0])
            res.append(x)
    return res[:limit]


async def community(bot, year: int) -> dict:
    from utils.currency import ensure_rates
    await ensure_rates()
    y = str(year)
    rv = await asyncio.to_thread(_reviews, year)
    drops = await asyncio.to_thread(_price_drops, year)
    new_shops = await execute_db(
        bot, "SELECT name FROM known_shops WHERE substr(first_seen,1,4)=? ORDER BY first_seen",
        (y,), fetch=True) or []
    fastest = None
    try:
        rows = await execute_db(
            bot, "SELECT shop, AVG(days) AS a, COUNT(*) AS n FROM review_delivery "
                 "WHERE days IS NOT NULL AND substr(review_date,1,4)=? GROUP BY shop "
                 "HAVING COUNT(*) >= ? ORDER BY a ASC LIMIT 1", (y, MIN_DELIVERY), fetch=True)
        if rows:
            fastest = (rows[0]["shop"], round(rows[0]["a"], 1), rows[0]["n"])
    except Exception:
        pass
    return {
        "year": year,
        "reviews": rv,
        "new_species": await _one(bot, "SELECT COUNT(*) FROM known_species WHERE substr(first_seen,1,4)=?", (y,)),
        "new_shops": [r["name"] for r in new_shops if r["name"]],
        "drops": drops,
        "fastest": fastest,
        "codes": await _one(bot, "SELECT COUNT(DISTINCT lower(code)) FROM discount_codes "
                                 "WHERE substr(message_date,1,4)=?", (y,)),
        "keepers": await _one(bot, "SELECT COUNT(*) FROM map_entries WHERE substr(consent_at,1,4)=?", (y,)),
        "events": await _one(bot, "SELECT COUNT(*) FROM map_events WHERE status='approved' "
                                  "AND substr(start_at,1,4)=?", (y,)),
        "achievements": await _one(bot, "SELECT COUNT(*) FROM achievements WHERE substr(unlocked_at,1,4)=?", (y,)),
        "commands": await _one(bot, "SELECT COALESCE(SUM(value),0) FROM usage_monthly "
                                    "WHERE metric='calls' AND substr(month,1,4)=?", (y,)),
        "notifications": await _one(bot, "SELECT COUNT(*) FROM notifications WHERE substr(created_at,1,4)=?", (y,)),
    }


async def personal(bot, user_id: str, year: int) -> dict:
    y, uid = str(year), str(user_id)
    cmds = await execute_db(
        bot, "SELECT command, COUNT(*) AS n, MIN(created_at) AS first FROM command_log "
             "WHERE user_id=? AND substr(created_at,1,4)=? AND (status IS NULL OR status<>'text') "
             "GROUP BY command ORDER BY n DESC", (uid, y), fetch=True) or []
    ach = await execute_db(
        bot, "SELECT achievement_id FROM achievements WHERE user_id=? AND substr(unlocked_at,1,4)=? "
             "ORDER BY unlocked_at", (uid, y), fetch=True) or []
    revs = await execute_db(
        bot, "SELECT shop, rating, review_date FROM review_authors WHERE user_id=? "
             "AND substr(review_date,1,4)=? ORDER BY review_date", (uid, y), fetch=True) or []
    ratings = [r["rating"] for r in revs if r["rating"] is not None]
    shops = Counter(r["shop"] for r in revs if r["shop"])
    return {
        "year": year,
        "reviews": len(revs),
        "reviews_avg": round(sum(ratings) / len(ratings), 1) if ratings else None,
        "reviews_top": shops.most_common(3),
        "reviews_first": revs[0]["review_date"] if revs else None,
        "commands": sum(r["n"] for r in cmds),
        "top_commands": [(r["command"], r["n"]) for r in cmds[:3]],
        "first": min((r["first"] for r in cmds), default=None),
        "achievements": [r["achievement_id"] for r in ach],
        "notifications": await _one(bot, "SELECT COUNT(*) FROM notifications WHERE user_id=? "
                                         "AND substr(created_at,1,4)=?", (uid, y)),
        "watches": await _one(bot, "SELECT COUNT(*) FROM user_species_watch WHERE user_id=? "
                                   "AND substr(created_at,1,4)=?", (uid, y)),
        "tracked": await _one(bot, "SELECT COUNT(*) FROM user_price_tracking WHERE user_id=? "
                                   "AND substr(added_at,1,4)=?", (uid, y)),
        "rsvps": await _one(bot, "SELECT COUNT(*) FROM map_event_rsvp WHERE user_id=? "
                                 "AND substr(occ_date,1,4)=?", (uid, y)),
        "inat": await _inat(bot, uid, year),
    }


async def _inat(bot, uid: str, year: int) -> dict:
    try:
        from utils.inat import user_stats
        st = await user_stats(bot, uid, year)
        return {"obs": st["obs"], "species": st["species"]}
    except Exception:
        return {"obs": 0, "species": 0}


def mask(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    return "< 3" if 0 < n < 3 else f"{n:,}".replace(",", ".")


def current_year() -> int:
    return datetime.now(timezone.utc).year


# ── Zeitfenster: Rückblick nur vom 1. November bis 31. Januar ───────────────────
OPEN_MONTHS = (11, 12, 1)
OPENS_MONTH = 11


def is_open(now: datetime | None = None) -> bool:
    """True vom 1. November bis 31. Januar (Berliner Zeit)."""
    from utils.timez import BERLIN
    now = now or datetime.now(BERLIN)
    return now.month in OPEN_MONTHS


def review_year(now: datetime | None = None) -> int:
    """Jahr, um das es gerade geht: Nov/Dez das laufende, im Januar das Vorjahr.
    Außerhalb des Fensters das zuletzt abgeschlossene Jahr."""
    from utils.timez import BERLIN
    now = now or datetime.now(BERLIN)
    return now.year if now.month >= OPENS_MONTH else now.year - 1


def next_open(now: datetime | None = None) -> datetime:
    """Nächster Öffnungstag (1. November)."""
    from utils.timez import BERLIN
    now = now or datetime.now(BERLIN)
    return datetime(now.year, OPENS_MONTH, 1)


_FIRST_YEAR: list = []


async def first_year(bot) -> int:
    """Erstes Jahr mit Daten (älteste Bewertung im Sheet oder ältester Befehl)."""
    if _FIRST_YEAR:
        return _FIRST_YEAR[0]
    years = []
    try:
        from utils.sheet import sheet
        for r in (getattr(sheet, "_rows", None) or [])[1:]:
            d = (r[0] if r else "").strip()
            if len(d) == 10 and d[6:10].isdigit():
                years.append(int(d[6:10]))
    except Exception:
        pass
    try:
        r = await execute_db(bot, "SELECT MIN(substr(created_at,1,4)) FROM command_log", fetch=True)
        if r and r[0][0]:
            years.append(int(r[0][0]))
    except Exception:
        pass
    y = max(2020, min(years)) if years else review_year()
    if years:
        _FIRST_YEAR.append(y)
    return y
