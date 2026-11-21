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
utils/forecast.py – Schwarmflug-Prognose aus Community-Meldungen + Wettervorhersage.

Ablauf
  1. Punkte: 35-km-Gitter über DACH (innerhalb der Regions-Umrisse) + Fundort-Hotspots
     (0,1°-Zellen mit ≥ 3 Meldungen, Zellmitte, nie der genaue Fundort). Alle Punkte lernen mit.
  2. Lernen (vor allem im Winter): Wetter ganzer Saisons (1.3.–31.10.) je Punkt und Jahr.
     Ab 2023 aus der Historical Forecast API (archivierte hochauflösende Vorhersagemodelle,
     dieselben wie die Prognose, z. B. ICON-D2 ≈ 2 km), davor aus der Historical Weather API
     (Standard ECMWF IFS 9 km ab 2017). Merkmale um 17 Uhr: Temperatur, Luftfeuchte, Taupunkt,
     Regen 24/48/72 h, Luftdruckänderung 12 h, Wind, Bewölkung, Sonnenstrahlung (10–16 Uhr),
     Bodentemperatur, Bodenfeuchte, Tageshöchstwert; dazu Saisonkurve und Meldeaktivität der Zelle.
     Ein Tag zählt für ein Taxon als „Flugtag“, wenn es im Umkreis von 15 km eine Meldung gab.
     Je Gattung/Art mit genug Flugtagen: logistische Regression (numpy, L2), Negativtage als
     Stichprobe (gewichtet). Die letzten zwei Jahre werden zurückgehalten und nachgerechnet
     (AUC, Anteil Flugtage bei sehr guter Chance vs. insgesamt); Verlauf in fc_model_hist.
     Ohne Modell gilt eine Regel aus den Steckbriefen (höchstens Stufe „gut“).
  3. Vorhersage: März bis Oktober um 6, 9, 12, 15 und 18 Uhr für heute und morgen
     (Forecast API, past_days=3), in Paketen zu 100 Orten.
  4. Stufen: 1 möglich / 2 gut / 3 sehr gut (Quantile 70/85/95 % der Modellwerte in der Saison).

Open-Meteo (https://open-meteo.com/en/terms): freie API nur nicht-kommerziell, < 10.000 Aufrufe
am Tag, 5.000/Stunde, 600/Minute. Mehrere Orte, > 10 Variablen oder lange Zeiträume zählen als
mehrere Aufrufe (https://open-meteo.com/en/pricing). Hier wird bewusst vorsichtig gezählt:
Orte × ⌈Tage/7⌉ × max(1, Variablen/10). Ein gemeinsamer Tageszähler (om_budget) teilt das Budget:
Prognose 5.000, Wetter-Nachtrag 1.500, Lernen 2.000 (außerhalb der Saison 5.000), Rest Puffer.
Übertragen werden nur Koordinaten der Punkte (auf 0,01° bzw. Zellmitte), keine Personendaten.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from utils.db import execute_db, execute_many
from utils.timez import BERLIN

logger = logging.getLogger(__name__)

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST = "https://api.open-meteo.com/v1/forecast"
HIST_FC_FROM = 2023                     # ab diesem Jahr Historical Forecast API (volle Saisons)
_BASE = "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,pressure_msl,wind_speed_10m,cloud_cover,shortwave_radiation"
VARS_FC = _BASE + ",soil_temperature_0cm,soil_moisture_0_to_1cm"          # Forecast / Historical Forecast
VARS_ARCH = _BASE + ",soil_temperature_0_to_7cm,soil_moisture_0_to_7cm"   # Historical Weather (Reanalyse)
FC_VARS = VARS_FC + ",precipitation_probability"
CAPS = {"forecast": 5000, "weather": 1500, "train": 2000}
TRAIN_CAP_OFFSEASON = 5000
SEASON = range(3, 11)                     # März bis Oktober
RUN_SLOTS = (6, 9, 12, 15, 18)
GRID_KM = 35
HOT_MAX = 500
NEG_SAMPLE = 150_000                      # Negativtage je Lernlauf (Stichprobe, gewichtet)
RADIUS_KM = 15
CHUNK = 100
MIN_POS = 40                              # so viele Flugtage braucht ein Modell
QUANTS = (0.70, 0.85, 0.95)
FEATS = ("season", "temp", "temp2", "hum", "dew", "rain24", "rain48", "rain72", "dp12", "wind", "cloud",
         "rad", "tmax", "soil_t", "soil_m", "act")
STATIC = Path(__file__).resolve().parent.parent / "static"
_UA = {"User-Agent": "AAM-Discord-Bot (Schwarmflug-Prognose)"}


# ── Budget ───────────────────────────────────────────────────────────────────
def cost(n_loc: int, n_days: int, n_vars: int) -> float:
    return n_loc * max(1, math.ceil(n_days / 7)) * max(1.0, n_vars / 10)


def _today() -> str:
    return datetime.now(BERLIN).date().isoformat()


async def budget_left(bot, purpose: str) -> float:
    from config import SWARM_OM_DAILY
    rows = await execute_db(bot, "SELECT purpose, used FROM om_budget WHERE day=?", (_today(),), fetch=True) or []
    used = {r["purpose"]: r["used"] for r in rows}
    cap = CAPS.get(purpose, 0)
    if purpose == "train" and datetime.now(BERLIN).month not in SEASON:
        cap = TRAIN_CAP_OFFSEASON
    return max(0.0, min(cap - used.get(purpose, 0), SWARM_OM_DAILY - sum(used.values())))


async def budget_use(bot, purpose: str, n: float) -> None:
    await execute_db(bot, "INSERT INTO om_budget (day, purpose, used) VALUES (?,?,?) "
                          "ON CONFLICT(day, purpose) DO UPDATE SET used=used+excluded.used",
                     (_today(), purpose, float(n)), commit=True)


async def budget_block(bot) -> None:
    """Nach HTTP 429: für heute nichts mehr abfragen."""
    from config import SWARM_OM_DAILY
    await budget_use(bot, "blocked", SWARM_OM_DAILY)


async def budget_today(bot) -> dict:
    rows = await execute_db(bot, "SELECT purpose, used FROM om_budget WHERE day=?", (_today(),), fetch=True) or []
    return {r["purpose"]: round(r["used"], 1) for r in rows}


async def _get(session, url, params):
    try:
        async with session.get(url, params=params) as r:
            if r.status == 429:
                return "limit"
            if r.status != 200:
                logger.info("🔮 Open-Meteo HTTP %s", r.status)
                return None
            return await r.json()
    except Exception as e:
        logger.info("🔮 Open-Meteo nicht erreichbar: %s", e)
        return None


# ── Geometrie ────────────────────────────────────────────────────────────────
def km(a_lat, a_lon, b_lat, b_lon) -> float:
    p = math.pi / 180
    x = (b_lon - a_lon) * p * math.cos((a_lat + b_lat) * p / 2)
    y = (b_lat - a_lat) * p
    return 6371 * math.hypot(x, y)


def _polys() -> list:
    out = []
    for f in ("de_bundeslaender", "at_bundeslaender", "ch_kantone", "li_land"):
        try:
            gj = json.loads((STATIC / f"{f}.geojson").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for ft in gj.get("features", []):
            pr = ft.get("properties") or {}
            iso = str(pr.get("shapeISO") or "")
            if iso == "LIE" or pr.get("shapeName") == "Liechtenstein":
                c, code = "li", "LI"
            elif "-" in iso:
                c, code = iso.split("-", 1)[0].lower(), iso.split("-", 1)[1].upper()
            else:
                continue
            g = ft.get("geometry") or {}
            polys = g.get("coordinates") or []
            if g.get("type") == "Polygon":
                polys = [polys]
            for poly in polys:
                ring = poly[0]
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                out.append((c, code, ring, (min(xs), min(ys), max(xs), max(ys))))
    return out


def _inside(lon, lat, ring) -> bool:
    ins = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            ins = not ins
        j = i
    return ins


def region_of(lat, lon, polys) -> tuple[str, str] | None:
    for c, code, ring, (x0, y0, x1, y1) in polys:
        if x0 <= lon <= x1 and y0 <= lat <= y1 and _inside(lon, lat, ring):
            return c, code
    return None


# ── Punkte ───────────────────────────────────────────────────────────────────
async def build_points(bot) -> dict:
    polys = await asyncio.to_thread(_polys)
    pts: dict = {}
    if polys:
        step_lat = GRID_KM / 111.0
        lat = 45.8
        while lat <= 55.1:
            step_lon = GRID_KM / (111.0 * math.cos(math.radians(lat)))
            lon = 5.9
            while lon <= 17.2:
                reg = region_of(lat, lon, polys)
                if reg:
                    k = f"{lat:.2f},{lon:.2f}"
                    pts[k] = {"lat": round(lat, 2), "lon": round(lon, 2), "country": reg[0], "region": reg[1],
                              "kind": "grid", "n": 0}
                lon += step_lon
            lat += step_lat
    rows = await execute_db(bot, "SELECT lat, lon, country, region FROM swarm_reports "
                                 "WHERE lat IS NOT NULL AND indoor=0", fetch=True) or []
    cells: dict = {}
    for r in rows:
        k = (round(r["lat"], 1), round(r["lon"], 1))
        c = cells.setdefault(k, {"n": 0, "reg": {}})
        c["n"] += 1
        if r["region"]:
            rk = (r["country"], r["region"])
            c["reg"][rk] = c["reg"].get(rk, 0) + 1
    hot = sorted(((v["n"], k, v) for k, v in cells.items() if v["n"] >= 3), reverse=True)[:HOT_MAX]
    for i, (n, (la, lo), v) in enumerate(hot):
        reg = region_of(la, lo, polys) if polys else None
        if not reg and v["reg"]:
            reg = max(v["reg"].items(), key=lambda x: x[1])[0]
        if not reg or reg[0] not in ("de", "at", "ch", "li"):
            continue
        k = f"{la:.2f},{lo:.2f}"
        pts[k] = {"lat": la, "lon": lo, "country": reg[0], "region": reg[1], "kind": "hot", "n": n}
    hot_keys = set(pts)                              # alle Punkte lernen mit (Reihenfolge: Hotspots zuerst)
    await execute_db(bot, "UPDATE fc_points SET active=0", commit=True)
    for k, p in pts.items():
        await execute_db(bot, "INSERT INTO fc_points (key, lat, lon, country, region, kind, n, train, active) "
                              "VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(key) DO UPDATE SET country=excluded.country, "
                              "region=excluded.region, kind=excluded.kind, n=excluded.n, train=excluded.train, active=1",
                         (k, p["lat"], p["lon"], p["country"], p["region"], p["kind"], p["n"],
                          1 if k in hot_keys else 0), commit=True)
    g = sum(1 for p in pts.values() if p["kind"] == "grid")
    logger.info("🔮 Prognosepunkte: %d Gitter + %d Hotspots (%d Lernpunkte)", g, len(pts) - g, len(hot_keys))
    return {"grid": g, "hot": len(pts) - g, "train": len(hot_keys)}


async def points(bot, train_only=False) -> list[dict]:
    sql = "SELECT * FROM fc_points WHERE active=1" + (" AND train=1" if train_only else "") + \
        " ORDER BY kind='grid', n DESC"
    return [dict(r) for r in await execute_db(bot, sql, fetch=True) or []]


# ── Tagesmerkmale aus Stundenwerten ──────────────────────────────────────────
def _pos(h: dict) -> dict:
    """Zeitstempel → Index (einmal je Stundenreihe berechnet)."""
    if "_pos" not in h:
        h["_pos"] = {t: i for i, t in enumerate(h.get("time") or [])}
    return h["_pos"]


def day_features(h: dict, day: str) -> dict | None:
    """Merkmale eines Tages um 17 Uhr aus einer Open-Meteo-Stundenreihe (Europe/Berlin).
    Fehlende Variablen (z. B. Boden in alten Jahren) bleiben None und werden beim Lernen ersetzt."""
    times = h.get("time") or []
    pos = _pos(h)
    key = f"{day}T17:00"
    if key not in pos:
        return None
    i = pos[key]

    def arr(*names):
        for n in names:
            if h.get(n):
                return h[n]
        return []

    def v(names, j):
        a = arr(*names) if isinstance(names, tuple) else arr(names)
        return a[j] if 0 <= j < len(a) else None

    def rain(hours):
        xs = [x for x in arr("precipitation")[max(0, i - hours):i] if x is not None]
        return round(sum(xs), 2) if len(xs) >= hours * 0.8 else None
    p_now, p_before = v("pressure_msl", i), v("pressure_msl", i - 12)
    temps = [x for x in arr("temperature_2m")[i - 7:i + 4] if x is not None]
    rad = [x for x in arr("shortwave_radiation")[i - 7:i] if x is not None]
    out = {"temp": v("temperature_2m", i), "hum": v("relative_humidity_2m", i), "dew": v("dew_point_2m", i),
           "rain24": rain(24), "rain48": rain(48), "rain72": rain(72),
           "dp12": round(p_now - p_before, 2) if p_now is not None and p_before is not None else None,
           "wind": v("wind_speed_10m", i), "cloud": v("cloud_cover", i),
           "rad": round(sum(rad) / len(rad), 1) if rad else None,
           "tmax": max(temps) if temps else v("temperature_2m", i),
           "soil_t": v(("soil_temperature_0cm", "soil_temperature_0_to_7cm"), i),
           "soil_m": v(("soil_moisture_0_to_1cm", "soil_moisture_0_to_7cm"), i)}
    if out["rain24"] is None:
        out["rain24"] = 0.0
    return out if out["temp"] is not None else None


def hours_of(h: dict, day: str) -> dict:
    """Stundenwerte 10–21 Uhr für Zeitfenster: {stunde: (temp, regen, regenwahrscheinlichkeit)}"""
    times = h.get("time") or []
    pos = _pos(h)
    out = {}
    for hr in range(10, 22):
        k = f"{day}T{hr:02d}:00"
        if k in pos:
            i = pos[k]
            pp = (h.get("precipitation_probability") or [None] * len(times))
            out[hr] = ((h.get("temperature_2m") or [None])[i], (h.get("precipitation") or [0])[i] or 0,
                       pp[i] if i < len(pp) and pp[i] is not None else 0)
    return out


# ── Lernen ───────────────────────────────────────────────────────────────────
_TCOLS = ("temp", "hum", "dew", "rain24", "rain48", "rain72", "dp12", "wind", "cloud", "rad", "tmax", "soil_t", "soil_m")


async def _first_year(bot) -> int:
    yrs = await execute_db(bot, "SELECT MIN(substr(flight_date,1,4)) AS a FROM swarm_reports", fetch=True)
    return int(yrs[0]["a"]) if yrs and yrs[0]["a"] else datetime.now(BERLIN).year


def _src_for(y: int) -> str:
    return "hf" if y >= HIST_FC_FROM else "ifs"


def _train_batch(h: dict, key: str, y: int, end: date, src: str) -> list:
    out = []
    d = date(y, 3, 1)
    while d <= end:
        f = day_features(h, d.isoformat())
        if f:
            out.append((key, d.isoformat(), *[f[c_] for c_ in _TCOLS], src))
        d += timedelta(days=1)
    return out


async def train_fetch(bot, max_requests: int = 200) -> int:
    """Wetter ganzer Saisons (1.3.–31.10.) je Punkt und Jahr nachladen. Hotspots zuerst, neueste Jahre
    zuerst. Jahre, die mit der älteren Quelle geladen wurden, werden mit der feineren erneut geholt."""
    pts = await points(bot, train_only=True)
    if not pts:
        return 0
    done = {(r["key"], r["year"]): r["src"] for r in
            await execute_db(bot, "SELECT key, year, src FROM fc_train_done", fetch=True) or []}
    first = await _first_year(bot)
    today = datetime.now(BERLIN).date()
    todo = []
    for y in range(today.year, first - 1, -1):
        src = _src_for(y)
        lag = 3 if src == "hf" else 6
        end = min(date(y, 10, 31), today - timedelta(days=lag))
        if end < date(y, 3, 1):
            continue
        for p in pts:
            if done.get((p["key"], y)) != src:
                todo.append((p, y, end, src))
    n = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90), headers=_UA) as s:
        for p, y, end, src in todo[:max_requests]:
            start = date(y, 2, 26)                       # 3 Tage Vorlauf für Regen 72 h
            nv = len((VARS_FC if src == "hf" else VARS_ARCH).split(","))
            c = cost(1, (end - start).days + 1, nv)
            if await budget_left(bot, "train") < c:
                break
            js = await _get(s, HIST_FC if src == "hf" else ARCHIVE,
                            {"latitude": p["lat"], "longitude": p["lon"],
                             "hourly": VARS_FC if src == "hf" else VARS_ARCH,
                             "start_date": start.isoformat(), "end_date": end.isoformat(),
                             "timezone": "Europe/Berlin"})
            await budget_use(bot, "train", c)
            if js == "limit":
                await budget_block(bot)
                break
            if not js:
                continue
            batch = await asyncio.to_thread(_train_batch, js.get("hourly") or {}, p["key"], y, end, src)
            await execute_many(bot, f"INSERT OR REPLACE INTO fc_train (key, date, {', '.join(_TCOLS)}, src) "
                                    f"VALUES (?,?,{','.join('?' * len(_TCOLS))},?)", batch)
            if end >= date(y, 10, 31):
                await execute_db(bot, "INSERT INTO fc_train_done (key, year, src) VALUES (?,?,?) ON CONFLICT(key, year) "
                                      "DO UPDATE SET src=excluded.src", (p["key"], y, src), commit=True)
            n += 1
            await asyncio.sleep(1.1)
    if n:
        logger.info("🔮 Lerndaten: %d Punkt-Jahre geladen (%d offen)", n, max(0, len(todo) - n))
    return n


async def train_pending(bot) -> int:
    pts = await points(bot, train_only=True)
    done = {(r["key"], r["year"]): r["src"] for r in
            await execute_db(bot, "SELECT key, year, src FROM fc_train_done", fetch=True) or []}
    first = await _first_year(bot)
    today = datetime.now(BERLIN).date()
    last = today.year if today >= date(today.year, 11, 6) else today.year - 1
    return sum(1 for y in range(first, last + 1) for p in pts if done.get((p["key"], y)) != _src_for(y))


def _season_curve(weeks: list[int]) -> list[float]:
    """Wöchentliche Saisonkurve (1–53), geglättet, Maximum 1, Boden 0,01."""
    c = [0.0] * 54
    for w in weeks:
        for d, wt in ((-2, .1), (-1, .2), (0, .4), (1, .2), (2, .1)):
            if 1 <= w + d <= 53:
                c[w + d] += wt
    mx = max(c) or 1
    return [max(0.01, x / mx) for x in c]


def _lg(x):
    return math.log1p(x) if x is not None else None


def _vec(f: dict, season: float, act: float) -> list:
    t = f.get("temp")
    return [math.log(season), t, (t * t) if t is not None else None, f.get("hum"), f.get("dew"),
            _lg(f.get("rain24")), _lg(f.get("rain48")), _lg(f.get("rain72")), f.get("dp12"), f.get("wind"),
            f.get("cloud"), f.get("rad"), f.get("tmax") if f.get("tmax") is not None else t,
            f.get("soil_t"), f.get("soil_m"), math.log1p(act)]


def _fit(X, y, sw=None, lam=1.0, iters=30):
    """L2-logistische Regression (Newton). Fehlende Werte → Mittelwert; sw = Stichprobengewichte."""
    import numpy as np
    X = np.array([[np.nan if v is None else v for v in row] for row in X], float)
    y = np.asarray(y, float)
    sw = np.ones(len(y)) if sw is None else np.asarray(sw, float)
    mu = np.nanmean(X, 0)
    mu = np.where(np.isnan(mu), 0, mu)
    X = np.where(np.isnan(X), mu, X)
    sd = X.std(0)
    sd[sd == 0] = 1
    Z = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = np.zeros(Z.shape[1])
    pos = (sw * y).sum()
    neg = (sw * (1 - y)).sum()
    cw = sw * np.where(y > 0, neg / max(pos, 1), 1.0)     # Ungleichgewicht ausgleichen
    R = np.eye(Z.shape[1]) * lam
    R[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (cw * (p - y)) + R @ w
        H = (Z * (cw * p * (1 - p))[:, None]).T @ Z + R
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return {"w": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist()}


def _prob(m: dict, x: list) -> float:
    z = m["w"][0] + sum(wi * ((mi if xi is None else xi) - mi) / si
                        for wi, xi, mi, si in zip(m["w"][1:], x, m["mu"], m["sd"]))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def _wquant(vals, weights, q):
    """Gewichtetes Quantil."""
    pairs = sorted(zip(vals, weights))
    tot = sum(weights) or 1
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot:
            return v
    return pairs[-1][0] if pairs else 0.0


def _auc(scores, labels) -> float | None:
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return None
    allv = sorted((s, l) for s, l in zip(scores, labels))
    rank_sum, i = 0.0, 0
    while i < len(allv):                     # Mann-Whitney mit Gleichstand-Mittelrang
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + j + 1) / 2
        rank_sum += r * sum(1 for k in range(i, j) if allv[k][1])
        i = j
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


async def _train_rows(bot, pos_keys: set) -> tuple[list[dict], float]:
    """Alle Flugtage + Stichprobe der übrigen Tage. Rückgabe (Zeilen, Gewicht der Negativ-Stichprobe)."""
    tot = await execute_db(bot, "SELECT COUNT(*) AS n FROM fc_train", fetch=True)
    total = tot[0]["n"] if tot else 0
    if not total:
        return [], 1.0
    rate = min(1.0, NEG_SAMPLE / total)
    per_mille = max(1, int(rate * 1000))
    rows = [dict(r) for r in await execute_db(bot, "SELECT * FROM fc_train WHERE abs(random()) % 1000 < ?",
                                              (per_mille,), fetch=True) or []]
    have = {(r["key"], r["date"]) for r in rows}
    for r in rows:
        r["_w"] = 1000 / per_mille
    keys = [k for k in pos_keys if k not in have]
    for i in range(0, len(keys), 400):
        part = keys[i:i + 400]
        q = " OR ".join("(key=? AND date=?)" for _ in part)
        for r in await execute_db(bot, f"SELECT * FROM fc_train WHERE {q}", tuple(x for k in part for x in k),
                                  fetch=True) or []:
            d = dict(r)
            d["_w"] = 1.0
            rows.append(d)
    for r in rows:
        if (r["key"], r["date"]) in pos_keys:
            r["_w"] = 1.0                          # Flugtage sind vollständig enthalten
    return rows, 1000 / per_mille


def _near_map(reps: list, pts: dict) -> dict:
    """(punkt, tag) → Taxa mit Meldung im Umkreis von RADIUS_KM."""
    near: dict = {}
    for r in reps:
        if r["lat"] is None:
            continue
        tx = {r["genus"]} | ({f"{r['genus']} {r['species']}"} if r["species"] and not r["qualifier"] else set())
        for k, p in pts.items():
            if abs(p["lat"] - r["lat"]) < 0.2 and abs(p["lon"] - r["lon"]) < 0.3 and \
                    km(p["lat"], p["lon"], r["lat"], r["lon"]) <= RADIUS_KM:
                near.setdefault((k, r["flight_date"]), set()).update(tx)
    return near


def _learn_one(name, lst, trows, weeks, near, pts, test_years, info) -> dict:
    """Ein Taxon lernen (läuft im Thread). Rückgabe: kind, params, Kennzahlen."""
    from utils import swarm as S
    curve = _season_curve([S._week(r["flight_date"]) for r in lst])
    regs = sorted({f"{r['country']}-{r['region']}" for r in lst if r["country"] and r["region"]})
    base = {"curve": curve, "h": [info.get("h_from"), info.get("h_to")],
            "t": [info.get("t_from"), info.get("t_to"), info.get("temp")], "regions": regs}
    X, y, w, yrs = [], [], [], []
    for r, wk in zip(trows, weeks):
        lab = 1 if name in near.get((r["key"], r["date"]), ()) else 0
        X.append(_vec(r, curve[wk], pts.get(r["key"], {}).get("n", 0)))
        y.append(lab)
        w.append(1.0 if lab else r["_w"])
        yrs.append(r["date"][:4])
    npos = sum(y)
    if npos < MIN_POS or len(X) < 500:
        return {"kind": "rule", "params": base, "n_pos": npos, "n_obs": len(X)}
    auc = hit_hi = hit_base = None
    if test_years:
        tr = [i for i, yy in enumerate(yrs) if yy not in test_years]
        te = [i for i, yy in enumerate(yrs) if yy in test_years]
        if sum(y[i] for i in tr) >= MIN_POS // 2 and sum(y[i] for i in te) >= 5:
            m0 = _fit([X[i] for i in tr], [y[i] for i in tr], [w[i] for i in tr])
            sc = [_prob(m0, X[i]) for i in te]
            lab = [y[i] for i in te]
            wt = [w[i] for i in te]
            auc = _auc(sc, lab)
            cut_ = _wquant(sc, wt, QUANTS[2])
            hp = sum(wi for s_, l, wi in zip(sc, lab, wt) if s_ >= cut_ and l)
            ha = sum(wi for s_, l, wi in zip(sc, lab, wt) if s_ >= cut_)
            hit_hi = hp / ha if ha else None
            hit_base = sum(wi for l, wi in zip(lab, wt) if l) / (sum(wt) or 1)
    m = _fit(X, y, w)
    ins = [(_prob(m, X[i]), w[i]) for i in range(len(X)) if 12 <= weeks[i] <= 42]
    m["levels"] = [_wquant([a for a, _ in ins], [b for _, b in ins], q) for q in QUANTS] if ins else [0.5, 0.7, 0.9]
    m.update(base)
    return {"kind": "model", "params": m, "auc": auc, "hit_hi": hit_hi, "hit_base": hit_base,
            "n_pos": npos, "n_obs": len(X)}


async def train_models(bot) -> dict:
    """Modelle je Gattung/Art aus fc_train + Meldungen; Regeln für den Rest. Verlauf in fc_model_hist."""
    from utils import swarm as S
    pts = {p["key"]: p for p in await points(bot, train_only=True)}
    reps = [dict(r) for r in await execute_db(bot, "SELECT * FROM swarm_reports WHERE indoor=0", fetch=True) or []]
    taxa: dict = {}
    for r in reps:
        taxa.setdefault(r["genus"], []).append(r)
        if r["species"] and not r["qualifier"]:
            taxa.setdefault(f"{r['genus']} {r['species']}", []).append(r)
    near = await asyncio.to_thread(_near_map, reps, pts)
    trows, _negw = await _train_rows(bot, set(near))
    summ = {x["name"]: x for x in (await asyncio.to_thread(S.summarize, reps))["taxa"]}
    out = {"model": 0, "rule": 0}
    years = sorted({r["date"][:4] for r in trows})
    test_years = set(years[-2:]) if len(years) >= 4 else set()
    weeks = [S._week(r["date"]) for r in trows]
    for name, lst in sorted(taxa.items(), key=lambda x: -len(x[1])):
        if len(lst) < 10:
            continue
        res = await asyncio.to_thread(_learn_one, name, lst, trows, weeks, near, pts, test_years,
                                      summ.get(name) or {})
        if res["kind"] == "rule":
            await execute_db(bot, "INSERT OR REPLACE INTO fc_models (taxon, kind, params, n_pos, n_obs, trained_at) "
                                  "VALUES (?,?,?,?,?,datetime('now'))",
                             (name, "rule", json.dumps(res["params"]), res["n_pos"], res["n_obs"]), commit=True)
            out["rule"] += 1
            continue
        await execute_db(bot, "INSERT OR REPLACE INTO fc_models (taxon, kind, params, auc, hit_hi, hit_base, n_pos, "
                              "n_obs, trained_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                         (name, "model", json.dumps(res["params"]), res["auc"], res["hit_hi"], res["hit_base"],
                          res["n_pos"], res["n_obs"]), commit=True)
        await execute_db(bot, "INSERT INTO fc_model_hist (taxon, auc, hit_hi, hit_base, n_pos) VALUES (?,?,?,?,?)",
                         (name, res["auc"], res["hit_hi"], res["hit_base"], res["n_pos"]), commit=True)
        out["model"] += 1
    logger.info("🔮 Modelle: %d gelernt, %d Regeln (%d Lernzeilen)", out["model"], out["rule"], len(trows))
    return out


async def previous_quality(bot) -> dict:
    """Trefferquote der Vorversion je Taxon (letzter Lernlauf, der mindestens 60 Tage älter ist)."""
    rows = await execute_db(bot,
        "SELECT h.taxon, h.auc, h.hit_hi, h.hit_base, h.trained_at FROM fc_model_hist h JOIN fc_models m "
        "ON m.taxon=h.taxon WHERE julianday(m.trained_at) - julianday(h.trained_at) >= 60 "
        "ORDER BY h.trained_at", fetch=True) or []
    return {r["taxon"]: dict(r) for r in rows}


async def models(bot) -> dict:
    return {r["taxon"]: {**dict(r), "params": json.loads(r["params"])}
            for r in await execute_db(bot, "SELECT * FROM fc_models", fetch=True) or []}


# ── Vorhersage ───────────────────────────────────────────────────────────────
def _rule_p(par: dict, f: dict, season: float) -> float:
    t_mid = par["t"][2] if par["t"][2] is not None else 24
    fit_t = math.exp(-((f["temp"] - t_mid) / 6) ** 2)
    dry = 0.4 if (f["rain24"] or 0) > 8 else 1.0
    return season * fit_t * dry


def score(mod: dict, f: dict, week: int, act: int) -> tuple[float, int]:
    par = mod["params"]
    season = par["curve"][week]
    if mod["kind"] == "model":
        p = _prob(par, _vec(f, season, act))
        lv = sum(1 for c in par["levels"] if p >= c)
    else:
        # Regel ohne Trefferprüfung: höchstens „gut“, nie „sehr gut“ (Kanal-Post nur mit Modell)
        p = _rule_p(par, f, season)
        lv = 2 if p >= 0.6 else 1 if p >= 0.35 else 0
    return p, lv


def window(par: dict, hrs: dict) -> tuple[int | None, int | None]:
    h0, h1 = par["h"][0], par["h"][1]
    if h0 is None:
        h0, h1 = 14, 20
    tmin = (par["t"][0] - 2) if par["t"][0] is not None else 16
    ok = [h for h, (t, r, pp) in sorted(hrs.items())
          if h0 - 1 <= h <= h1 + 1 and t is not None and t >= tmin and r < 0.3 and pp < 50]
    return (min(ok), max(ok)) if ok else (None, None)


def _score_chunk(part, res, days, mods, run_at):
    rows, got = [], set()
    for p, one in zip(part, res):
        h = one.get("hourly") or {}
        pkey = f"{p['country']}-{p['region']}"
        for d in days:
            f = day_features(h, d)
            if not f:
                continue
            hrs = hours_of(h, d)
            wk = date.fromisoformat(d).isocalendar()[1]
            got.add(p["key"])
            for name, mod in mods.items():
                rg = mod["params"].get("regions")
                if rg is not None and pkey not in rg:
                    continue
                pr, lv = score(mod, f, wk, p["n"])
                if lv < 1:
                    continue
                a, b = window(mod["params"], hrs)
                if a is None:
                    continue                 # zu kalt oder Regen in der typischen Flugzeit
                rows.append((p["key"], d, name, round(pr, 4), lv, a, b, f["temp"], run_at))
    return rows, got


async def run_forecast(bot, today: date | None = None) -> dict:
    """Vorhersage für heute und morgen an allen aktiven Punkten (today nur für Tests)."""
    pts = await points(bot)
    if not pts:
        await build_points(bot)
        pts = await points(bot)
    mods = await models(bot)
    if not pts or not mods:
        return {"points": len(pts), "models": len(mods), "pred": 0, "skipped": "keine Punkte/Modelle"}
    nvar = len(FC_VARS.split(","))
    need = cost(len(pts), 5, nvar)
    if await budget_left(bot, "forecast") < need:
        return {"points": len(pts), "pred": 0, "skipped": "Budget"}
    today = today or datetime.now(BERLIN).date()
    days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows: list = []
    got: set = set()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers=_UA) as s:
        for i in range(0, len(pts), CHUNK):
            part = pts[i:i + CHUNK]
            js = await _get(s, FORECAST, {"latitude": ",".join(f"{p['lat']:.2f}" for p in part),
                                          "longitude": ",".join(f"{p['lon']:.2f}" for p in part),
                                          "hourly": FC_VARS, "past_days": 3, "forecast_days": 2,
                                          "timezone": "Europe/Berlin"})
            await budget_use(bot, "forecast", cost(len(part), 5, nvar))
            if js == "limit":
                await budget_block(bot)
                break
            if not js:
                continue
            res = js if isinstance(js, list) else [js]
            r_, g_ = await asyncio.to_thread(_score_chunk, part, res, days, mods, run_at)
            rows += r_
            got |= g_
            if i + CHUNK < len(pts):
                await asyncio.sleep(12)               # ≤ 600 Aufrufe/Minute
    # Erst ersetzen, wenn der Lauf Daten hat (bei Netzfehlern bleibt die alte Prognose stehen)
    if got:
        await execute_many(bot, "DELETE FROM fc_pred WHERE key=? AND day=?", [(k, d) for k in got for d in days])
        await execute_many(bot, "INSERT OR REPLACE INTO fc_pred (key, day, taxon, p, level, h_from, h_to, temp, run_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    await execute_db(bot, "DELETE FROM fc_pred WHERE day<?", (days[0],), commit=True)
    logger.info("🔮 Prognose: %d Einträge für %d von %d Punkten", len(rows), len(got), len(pts))
    return {"points": len(pts), "models": len(mods), "pred": len(rows), "ok_points": len(got)}


# ── Auswertung für Board, PN und Kanal ───────────────────────────────────────
async def pred_days(bot) -> list[str]:
    """Tage mit Vorhersage (normal heute und morgen)."""
    return [r["day"] for r in await execute_db(bot, "SELECT DISTINCT day FROM fc_pred ORDER BY day LIMIT 2",
                                                fetch=True) or []]


async def by_region(bot, day: str, min_level: int = 1) -> list[dict]:
    """Je Region und Taxon die beste Stufe, Zeitfenster und Temperatur."""
    rows = await execute_db(bot,
        "SELECT p.country, p.region, f.taxon, MAX(f.level) AS level, MAX(f.p) AS p, MIN(f.h_from) AS h_from, "
        "MAX(f.h_to) AS h_to, ROUND(AVG(f.temp)) AS temp, COUNT(*) AS cells FROM fc_pred f "
        "JOIN fc_points p ON p.key=f.key WHERE f.day=? AND f.level>=? GROUP BY p.country, p.region, f.taxon "
        "ORDER BY level DESC, p DESC", (day, min_level), fetch=True) or []
    return [dict(r) for r in rows]


async def cells(bot, day: str, taxon: str | None = None) -> list[dict]:
    sql = ("SELECT p.lat, p.lon, p.kind, f.taxon, f.level, f.h_from, f.h_to, f.temp FROM fc_pred f "
           "JOIN fc_points p ON p.key=f.key WHERE f.day=?")
    params: tuple = (day,)
    if taxon:
        sql += " AND f.taxon=?"
        params += (taxon,)
    return [dict(r) for r in await execute_db(bot, sql, params, fetch=True) or []]


def condense(rows: list[dict], max_taxa: int = 6, max_regions: int = 5) -> list[dict]:
    """Regionsliste → je Taxon eine Zeile. Gibt es Arten einer Gattung, entfällt die Gattung."""
    from utils import swarm as S
    by: dict = {}
    for r in rows:
        e = by.setdefault(r["taxon"], {"taxon": r["taxon"], "level": 0, "p": 0, "regions": [], "h": [], "temp": []})
        e["level"] = max(e["level"], r["level"])
        e["p"] = max(e["p"], r["p"])
        e["regions"].append((r["level"], r["p"], S.region_name(r["country"], r["region"])))
        if r["h_from"] is not None:
            e["h"].append((r["h_from"], r["h_to"]))
        if r["temp"] is not None:
            e["temp"].append(r["temp"])
    species_genera = {t.split()[0] for t in by if " " in t}
    out = []
    for t, e in by.items():
        if " " not in t and t in species_genera:
            continue
        e["regions"] = [n for _, _, n in sorted(e["regions"], reverse=True)][:max_regions]
        hs = sorted(e.pop("h"))
        e["h_from"] = hs[len(hs) // 2][0] if hs else None
        e["h_to"] = sorted(b for _, b in hs)[len(hs) // 2] if hs else None
        tl = sorted(e.pop("temp"))
        e["temp"] = round(tl[len(tl) // 2]) if tl else None
        out.append(e)
    out.sort(key=lambda x: (-x["level"], -x["p"]))
    return out[:max_taxa]


def taxon_matches(alert_taxon: str, pred_taxon: str) -> bool:
    """Gattungs-Alarm passt zu Gattung und Arten der Gattung; Art-Alarm zur Art und zur Gattung."""
    a, p = alert_taxon.split(), pred_taxon.split()
    if a[0] != p[0]:
        return False
    return len(a) == 1 or len(p) == 1 or a[1] == p[1]


LEVEL_OF_PREF = {"often": 1, "normal": 2, "rare": 3}
