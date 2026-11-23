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
utils/board_swarm.py – Board-Seite /schwarmflug (Template + Daten) und der Kasten auf /art/<Art>.

Öffentlich: Zahlen je Region, Saisonverlauf, Steckbriefe, letzte Meldungen ohne Ort.
Eingeloggte AAM-Mitglieder: zusätzlich genaue Punkte, Ortstext und Link zur Discord-Nachricht.
Namen der Meldenden werden nirgends angezeigt.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from utils import swarm as S
from utils.db import execute_db
from utils.timez import BERLIN

RANGES = ("h48", "d7", "saison", "alle")
# Okabe-Ito (farbsehschwächen-tauglich), wie auf der Halter-Karte
PALETTE = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#CC79A7", "#D55E00", "#0072B2"]



def _week(d: str) -> int:
    try:
        return date.fromisoformat(d).isocalendar()[1]
    except ValueError:
        return 0


def _since(z: str, today: date) -> str | None:
    if z == "h48":
        return (today - timedelta(days=2)).isoformat()
    if z == "d7":
        return (today - timedelta(days=7)).isoformat()
    if z == "saison":
        return f"{today.year if today.month >= 3 else today.year - 1}-01-01"
    return None


def _taxon_key(r) -> str:
    return r["genus"] + (" " + r["species"] if r["species"] else "")


def _doy(d: str) -> int:
    try:
        return date.fromisoformat(d).timetuple().tm_yday
    except ValueError:
        return 0


def _med(xs):
    from statistics import median
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None


def identify(rows: list[dict], d: date, hour: int | None, region: str, temp: float | None, t) -> list[dict]:
    """„Was fliegt hier gerade?“ Rangliste der Arten (nur bis zur Art bestimmte Meldungen, ab 5).
    Gewichtet: Meldungen ±7 Tage um den Kalendertag (alle Jahre), stärker in der gewählten
    Region, dann wie gut Uhrzeit und Temperatur zu den bisherigen Meldungen passen."""
    c, _, reg = (region or "").partition("-")
    doy = d.timetuple().tm_yday
    by: dict = {}
    for r in rows:
        if r["species"] and not r["qualifier"]:
            by.setdefault(_taxon_key(r), []).append(r)
    out = []
    for name, lst in by.items():
        if len(lst) < 5:
            continue
        win = [r for r in lst if abs(_doy(r["flight_date"]) - doy) <= 7]
        if not win:
            continue
        loc = [r for r in win if c and r["country"] == c and (not reg or r["region"] == reg)]
        base = len(loc) * 3 + len(win) * (0.3 if c else 1)
        fh = ft = 1.0
        hrs = [int(r["flight_time"][:2]) for r in lst if r["flight_time"]]
        if hour is not None and len(hrs) >= 3:
            fh = 0.15 + sum(1 for h in hrs if abs(h - hour) <= 2) / len(hrs)
        tmp = [x for x in (S.temp_of(r) for r in lst) if x is not None]
        if temp is not None and len(tmp) >= 3:
            ft = 0.15 + sum(1 for x in tmp if abs(x - temp) <= 3) / len(tmp)
        why = t("sw_id_why_season", n=len(win))
        if c:
            why += " " + t("sw_id_why_region", n=len(loc))
        if len(hrs) >= 4:
            hs = sorted(hrs)
            why += " " + t("sw_id_why_hour", a=hs[len(hs) // 4], b=hs[(len(hs) * 3) // 4])
        out.append({"name": name, "score": base * fh * ft, "why": why})
    out.sort(key=lambda x: -x["score"])
    out = out[:8]
    tot = sum(x["score"] for x in out) or 1
    for x in out:
        x["pct"] = max(1, round(100 * x["score"] / tot))
    return out


def weather_table(rows: list[dict], t) -> tuple[list[dict], int]:
    ok = [r for r in rows if r.get("w_state") == 1]
    by: dict = {}
    for r in ok:
        by.setdefault(r["genus"], []).append(r)
        if r["species"]:
            by.setdefault(_taxon_key(r), []).append(r)
    out = []
    for name, lst in sorted(by.items(), key=lambda x: -len(x[1])):
        if len(lst) < 8:
            continue

        def rng(k, unit, nd=0):
            xs = sorted(x[k] for x in lst if x[k] is not None)
            if len(xs) < 4:
                return "?"
            a, b = xs[len(xs) // 4], xs[(len(xs) * 3) // 4]
            f = (lambda v: f"{v:.{nd}f}")
            return f"{f(a)}–{f(b)} {unit}" if f(a) != f(b) else f"{f(a)} {unit}"
        rain = [x["w_rain24"] for x in lst if x["w_rain24"] is not None]
        out.append({"name": name, "n": len(lst), "hum": rng("w_hum", "%"), "press": rng("w_press", "hPa"),
                    "wind": rng("w_wind", "km/h"), "cloud": rng("w_cloud", "%"),
                    "rain": f"{round(100 * sum(1 for x in rain if x >= 1.0) / len(rain))} %" if rain else "?"})
        if len(out) >= 14:
            break
    return out, len(ok)


def years_chart(rows: list[dict]) -> dict:
    """Mittlerer Flugtag je Jahr für die häufigsten Arten (je Jahr mind. 3 Meldungen)."""
    years = sorted({r["flight_date"][:4] for r in rows})
    by: dict = {}
    for r in rows:
        if r["species"] and not r["qualifier"]:
            by.setdefault(_taxon_key(r), {}).setdefault(r["flight_date"][:4], []).append(_doy(r["flight_date"]))
    cands = []
    for name, ys in by.items():
        good = {y: v for y, v in ys.items() if len(v) >= 3}
        if len(good) >= 3:
            cands.append((sum(len(v) for v in good.values()), name, good))
    cands.sort(reverse=True)
    series = []
    for i, (_n, name, good) in enumerate(cands[:6]):
        series.append({"label": name, "color": PALETTE[i % len(PALETTE)],
                       "data": [round(_med(good[y])) if y in good else None for y in years]})
    return {"years": years, "series": series}


CMP_MIN = 5                                # so viele Meldungen braucht jedes Jahr für eine Aussage


def compare_years(rows: list[dict], years: list[str], q: dict, lang: str, t) -> dict:
    """Zwei Jahre nebeneinander: Saisonkurve als Anteil der Jahresmeldungen je Woche (die Community
    ist gewachsen, absolute Zahlen wären nicht vergleichbar) und Kennzahlen."""
    if len(years) < 2:
        return {}
    cnt: dict = {}
    for r in rows:
        cnt[_taxon_key(r)] = cnt.get(_taxon_key(r), 0) + 1
        cnt[r["genus"] + " *"] = cnt.get(r["genus"] + " *", 0) + 1
    opts = [k for k, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= 2 * CMP_MIN and " " in k][:60]
    tx = (q.get("yt") or "").strip()[:60]
    if tx not in opts:
        tx = ""
    ya = q.get("ya") if q.get("ya") in years else years[0]
    yb = q.get("yb") if q.get("yb") in years else next((y for y in years if y != ya), years[-1])

    def match(r):
        if not tx:
            return True
        if tx.endswith(" *"):
            return r["genus"] == tx[:-2]
        return _taxon_key(r) == tx
    weeks = list(range(14, 45))
    out = {"ya": ya, "yb": yb, "tx": tx, "opts": [{"v": k, "l": (k[:-2] + " (" + t("sw_cmp_genus") + ")")
                                                   if k.endswith(" *") else k} for k in opts],
           "years": years, "cols": [], "data": {"weeks": [f"KW {w}" for w in weeks]}}
    name = (tx[:-2] if tx.endswith(" *") else tx) if tx else ""
    meds = {}
    for y, dash in ((ya, False), (yb, True)):
        lst = [r for r in rows if r["flight_date"][:4] == y and match(r)]
        n = len(lst)
        per = [0] * len(weeks)
        for r in lst:
            w = _week(r["flight_date"])
            if w in weeks:
                per[weeks.index(w)] += 1
        share = [round(100 * c / n, 1) for c in per] if n else [0] * len(weeks)
        ds = sorted(r["flight_date"] for r in lst)
        temps = [v for v in (S.temp_of(r) for r in lst) if v is not None]
        peak = weeks[per.index(max(per))] if n and max(per) else None
        col = {"year": y, "n": n,
               "first": date.fromisoformat(ds[0]).strftime("%d.%m.") if ds else "?",
               "last": date.fromisoformat(ds[-1]).strftime("%d.%m.") if ds else "?",
               "peak": S.week_label(peak, lang) if peak else "?",
               "temp": f"≈ {sum(temps) / len(temps):.0f} °C" if temps else "?",
               "temp_n": len(temps)}
        out["cols"].append(col)
        out["data"].setdefault("sets", []).append({"label": y, "data": share, "dash": dash,
                                                   "color": PALETTE[0] if not dash else PALETTE[1]})
        meds[y] = (_med([_doy(d) for d in ds]), n)
    (ma, na), (mb, nb) = meds[ya], meds[yb]
    subj = name or ""
    if na < CMP_MIN or nb < CMP_MIN:
        out["say"] = t("sw_cmp_few", a=ya, b=yb, na=na, nb=nb, m=CMP_MIN)
    else:
        d = round(mb - ma)                     # > 0: Jahr A früher
        key = "same" if abs(d) <= 2 else ("earlier" if d > 0 else "later")
        out["say"] = t(f"sw_cmp_{'tx' if subj else 'all'}_{key}", x=subj, a=ya, b=yb, d=abs(d))
    out["data"]["t"] = {"axis": t("sw_cmp_axis")}
    return out


def _abroad_ctx(rows: list, g: str, inr, member: bool, link, lang: str) -> dict:
    """Kasten „Aus dem Urlaub“: Meldungen aus Ländern außerhalb DACH (nicht in der Auswertung)."""
    sel = [r for r in rows if (not g or r["genus"] == g) and inr(r["flight_date"])]
    if not sel:
        sel = [r for r in rows if not g or r["genus"] == g]
    seen, out = set(), []
    for r in sel:
        k = (r["message_id"], r["label"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"label": r["label"], "country_name": S.country_label(r["country"], lang),
                    "flight_date": r["flight_date"], "place": r["place"] if member else "",
                    "link": link(r["message_id"]) if member else ""})
    return {"abroad": out[:25], "abroad_more": max(0, len(out) - 25)}


WR_VARS = ("temp", "rain", "hum", "press", "wind")


def weather_regions(rows: list[dict]) -> tuple[dict, dict]:
    """Wetter beim Flug je Region: {key: [n, temp, rain %, hum, press, wind]} (Median, Regen = Anteil ab 1 mm).
    Regionen mit weniger als 3 Meldungen nur mit n (Werte None)."""
    by: dict = {}
    names: dict = {}
    for r in rows:
        if not (r["country"] and r["region"]):
            continue
        k = f"{r['country']}-{r['region']}"
        names[k] = S.region_name(r["country"], r["region"])
        by.setdefault(k, []).append(r)
    out = {}
    for k, lst in by.items():
        n = len(lst)
        if n < 3:
            out[k] = [n, None, None, None, None, None]
            continue
        def med(xs):
            xs = [x for x in xs if x is not None]
            return round(_med(xs), 1) if len(xs) >= 3 else None
        rain = [r["w_rain24"] for r in lst if r.get("w_rain24") is not None]
        out[k] = [n, med([S.temp_of(r) for r in lst]),
                  round(100 * sum(1 for x in rain if x >= 1) / len(rain)) if len(rain) >= 3 else None,
                  med([r.get("w_hum") for r in lst]), med([r.get("w_press") for r in lst]),
                  med([r.get("w_wind") for r in lst])]
    return out, names


async def climate_ctx(bot, ct: str, g: str, member: bool) -> dict:
    """Flugwetter-Kalender: Anteil der Tage mit gutem Flugwetter je Lernpunkt und Woche (alle Jahre)."""
    try:
        taxa = [r["taxon"] for r in await execute_db(bot,
            "SELECT c.taxon FROM fc_climate c LEFT JOIN fc_models m ON m.taxon=c.taxon GROUP BY c.taxon "
            "ORDER BY MAX(m.n_pos) DESC", fetch=True) or []]
    except Exception:
        taxa = []
    if not taxa:
        return {"ok": False}
    tx = ct if ct in taxa else (g if g in taxa else taxa[0])
    rows = await execute_db(bot,
        "SELECT c.key, c.week, c.days, c.good, p.lat, p.lon, p.kind FROM fc_climate c JOIN fc_points p "
        "ON p.key=c.key WHERE c.taxon=? AND c.week BETWEEN 14 AND 44", (tx,), fetch=True) or []
    pts: dict = {}
    for r in rows:
        if r["kind"] == "hot" and not member:
            continue
        e = pts.setdefault(r["key"], [r["lat"], r["lon"], 1 if r["kind"] == "hot" else 0, [None] * 31])
        e[3][r["week"] - 14] = round(100 * r["good"] / r["days"]) if r["days"] else None
    return {"ok": True, "taxa": taxa, "taxon": tx, "data": {"taxon": tx, "pts": list(pts.values())}}


async def progress(bot, t) -> dict:
    """Stand der Datensammlung (für den Kasten und /schwarmflug/status.json)."""
    from utils import forecast as FC
    from config import SWARM_OM_DAILY

    async def one(sql, params=()):
        r = await execute_db(bot, sql, params, fetch=True)
        return (r[0][0] or 0) if r else 0
    q_out = "indoor=0 AND country IN ('de','at','ch','li')"
    places_t = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0 AND place<>''")
    places_o = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0 AND place<>'' "
                         "AND lat IS NULL AND geo_src IS NULL")
    clar_t = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0")
    clar_o = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0 AND ("
                       "(country='' AND ((geo_src IS NULL AND place='') OR geo_src='none')) OR taxon_check='unknown')")
    wx_t = await one(f"SELECT COUNT(*) FROM swarm_reports WHERE {q_out} AND lat IS NOT NULL")
    wx_o = await one(f"SELECT COUNT(*) FROM swarm_reports WHERE {q_out} AND lat IS NOT NULL AND w_state IS NULL")
    try:
        pend = await FC.train_pending(bot)
        done = await one("SELECT COUNT(*) FROM fc_train_done")
    except Exception:
        pend, done = 0, 0
    bud = await FC.budget_today(bot)
    used = sum(v for k, v in bud.items() if k != "blocked")

    def bar(key, total, open_):
        d = max(0, total - open_)
        return {"key": key, "done": d, "total": total, "pct": round(100 * d / total) if total else 100}
    bars = [bar("places", places_t, places_o), bar("clarify", clar_t, clar_o), bar("weather", wx_t, wx_o),
            bar("learn", done + pend, pend)]
    cool = FC.cooling() if hasattr(FC, "cooling") else 0
    if getattr(FC, "_BLOCK_DAY", "") == FC._today():
        state = t("sw_prog_blocked")
    elif cool > 0:
        state = t("sw_prog_pause", m=int(cool // 60) + 1)
    elif any(b["done"] < b["total"] for b in bars):
        state = t("sw_prog_running")
    else:
        state = t("sw_prog_done")
    eta_txt = ""
    try:
        eta = await FC.learn_eta(bot) if hasattr(FC, "learn_eta") else None
    except Exception:
        eta = None
    if eta:
        dd = date.fromisoformat(eta["date"]).strftime("%d.%m.%Y")
        eta_txt = t("sw_prog_eta_today") if eta["days"] == 0 else t("sw_prog_eta", d=eta["days"], date=dd,
                                                                      n=eta["per_day"])
    elif wx_o:
        eta_txt = t("sw_prog_eta_wx", d=max(1, -(-wx_o // 1500)))
    return {"bars": bars, "state": state, "eta": eta_txt,
            "budget": t("sw_prog_budget", u=f"{used:,.0f}".replace(",", "."), m=f"{SWARM_OM_DAILY:,}".replace(",", "."))}


def season_info(lang: str) -> dict | None:
    r = S.season_now()
    if not r:
        return None
    return {**r, "where": S.region_name(r["country"], r["region"]) if r["region"] else "?"}


def regions_select(t, lang: str) -> list[tuple[str, str]]:
    from utils import geo
    names = {"de": "Deutschland", "at": "Österreich", "ch": "Schweiz", "li": "Liechtenstein"}
    out = [("", t("sw_id_anywhere"))]
    for c, regs in geo.all_regions().items():
        out.append((c, names.get(c, c.upper())))
        if c != "li":
            out += [(f"{c}-{k}", f"  · {n}") for k, n in sorted(regs.items(), key=lambda x: x[1])]
    return out


async def forecast_ctx(bot, member: bool, q: dict, t) -> dict:
    """Prognose-Abschnitt: Tage, Taxa, Regionsstufen, Liste, Zellen (nur Mitglieder), Trefferquote."""
    from config import SWARM_FORECAST
    from utils import forecast as FC
    if not SWARM_FORECAST:
        return {"show": False}
    try:
        days = await FC.pred_days(bot)
        mods = await FC.models(bot)
    except Exception:
        return {"show": False}
    quality = []
    try:
        prev = await FC.previous_quality(bot)
    except Exception:
        prev = {}

    def _lift(m):
        return (m["hit_hi"] / m["hit_base"]) if m and m.get("hit_hi") is not None and m.get("hit_base") else None
    for name, m in sorted(mods.items(), key=lambda x: -(x[1].get("n_pos") or 0)):
        if m["kind"] != "model":
            continue
        lift, pl = _lift(m), _lift(prev.get(name))
        quality.append({"taxon": name, "n_pos": m.get("n_pos") or 0,
                        "prev": t("fc_q_times", x=f"{pl:.1f}".replace(".", ",")) if pl else "",
                        "lift": t("fc_q_times", x=f"{lift:.1f}".replace(".", ",")) if lift else "?",
                        "auc": f"{m['auc']:.2f}".replace(".", ",") if m.get("auc") is not None else "?"})
    rules = sum(1 for m in mods.values() if m["kind"] == "rule")
    try:
        pending = await FC.train_pending(bot)
    except Exception:
        pending = 0
    out = {"show": bool(mods), "active": bool(days), "days": days, "quality": quality[:20], "rules": rules,
           "pending": pending, "di": 0, "taxon": "", "taxa": [], "list": [], "run_at": ""}
    if not days:
        return out
    di = 1 if str(q.get("fd")) == "1" and len(days) > 1 else 0
    day = days[di]
    rows = await FC.by_region(bot, day, 1)
    taxa = sorted({r["taxon"] for r in rows}, key=lambda x: (" " not in x, x))
    tx = (q.get("ft") or "")[:60]
    if tx not in taxa:
        tx = ""
    sel = [r for r in rows if not tx or r["taxon"] == tx]
    reg: dict = {}
    info: dict = {}
    for r in sel:
        k = f"{r['country']}-{r['region']}"
        if r["level"] > reg.get(k, 0):
            reg[k] = r["level"]
        info.setdefault(k, []).append((r["level"], r["p"], r["taxon"]))
    reg_info = {k: ", ".join(f"{n} ({t('fc_lv_' + str(lv))})" for lv, _p, n in sorted(v, reverse=True)[:4])
                for k, v in info.items()}
    cells = []
    if member:
        best: dict = {}
        for c in await FC.cells(bot, day, tx or None):
            k = (c["lat"], c["lon"])
            if k not in best or c["level"] > best[k]["level"]:
                best[k] = c
        for c in best.values():
            win = t("fc_hours", a=c["h_from"], b=c["h_to"]) if c["h_from"] is not None else ""
            cells.append([c["lat"], c["lon"], c["level"], c["kind"], c["taxon"],
                          " · ".join(x for x in (t("fc_lv_" + str(c["level"])), win,
                                                 f"≈ {c['temp']:.0f} °C" if c["temp"] is not None else "") if x)])
    ra = await execute_db(bot, "SELECT MAX(run_at) AS r FROM fc_pred", fetch=True)
    run_at = ""
    if ra and ra[0]["r"]:
        from utils.timez import berlin_from_utc_naive
        run_at = berlin_from_utc_naive(ra[0]["r"], "%Y-%m-%d %H:%M:%S", "%d.%m. %H:%M")
    out.update(di=di, taxon=tx, taxa=taxa[:60], list=FC.condense(sel, 10, 5), run_at=run_at,
               data={"reg": reg, "regInfo": reg_info, "cells": cells})
    return out


async def page(bot, z: str, g: str, member: bool, lang: str, art_slug, month_names, t,
               q: dict | None = None) -> dict:
    """Kontext für das Template. art_slug(name) -> Slug oder None (Art im Verzeichnis?)."""
    from config import SWARM_CHANNEL_ID, SWARM_OSM_GEOCODING
    today = datetime.now(BERLIN).date()
    all_rows = await S.rows(bot, include_indoor=True)
    outdoor = [r for r in all_rows if not r["indoor"]]
    genera_cnt: dict = {}
    for r in outdoor:
        genera_cnt[r["genus"]] = genera_cnt.get(r["genus"], 0) + 1
    genera = sorted(genera_cnt, key=lambda x: -genera_cnt[x])
    if g and g not in genera_cnt:
        g = ""
    year_list = sorted({r["flight_date"][:4] for r in outdoor}, reverse=True)
    if z not in RANGES and z not in year_list:
        season_n = sum(1 for r in outdoor if r["flight_date"] >= (_since("saison", today) or ""))
        z = "saison" if season_n >= 20 else "alle"
    since = _since(z, today) if z in RANGES else f"{z}-01-01"
    until = f"{z}-12-31" if z in year_list else None

    def inr(d: str) -> bool:
        return (not since or d >= since) and (not until or d <= until)
    base = [r for r in outdoor if not g or r["genus"] == g]
    sel = [r for r in base if inr(r["flight_date"])]

    # Regionen (Karte)
    regions, names, top = {}, {}, {}
    per_reg: dict = {}
    for r in sel:
        if r["country"] and r["region"]:
            k = f"{r['country']}-{r['region']}"
            regions[k] = regions.get(k, 0) + 1
            names[k] = S.region_name(r["country"], r["region"])
            per_reg.setdefault(k, {}).setdefault(r["label"], 0)
            per_reg[k][r["label"]] += 1
    for k, d in per_reg.items():
        top[k] = ", ".join(f"{n} ({c})" for n, c in sorted(d.items(), key=lambda x: -x[1])[:3])

    # Farben je Gattung (Top 6 der Auswahl, Rest grau)
    gtop = genera[:6] if not g else [g]
    gcol = {gg: PALETTE[i % len(PALETTE)] for i, gg in enumerate(gtop)}

    guild_id = None
    ch = bot.get_channel(SWARM_CHANNEL_ID) if SWARM_CHANNEL_ID and hasattr(bot, "get_channel") else None
    guild_id = getattr(getattr(ch, "guild", None), "id", None)

    def link(mid):
        return f"https://discord.com/channels/{guild_id}/{SWARM_CHANNEL_ID}/{mid}" if guild_id else ""

    pins = []
    if member:
        for r in sel:
            if r["lat"] is not None and r["lon"] is not None:
                when = date.fromisoformat(r["flight_date"]).strftime("%d.%m.%Y") + \
                    (f" · {r['flight_time']}" if r["flight_time"] else "") + \
                    (f" · {r['temp_c']:g} °C" if r["temp_c"] is not None else
                     (f" · ≈ {r['w_temp']:.0f} °C" if r.get("w_temp") is not None else ""))
                pins.append([r["lat"], r["lon"], r["genus"], r["label"], when, r["place"], link(r["message_id"]),
                             _week(r["flight_date"])])

    # Jetzt gerade (48 h)
    fly: dict = {}
    since48 = (today - timedelta(days=2)).isoformat()
    for r in base:
        if r["flight_date"] >= since48:
            e = fly.setdefault(r["label"], {"label": r["label"], "n": 0, "regions": set()})
            e["n"] += 1
            if r["region"]:
                e["regions"].add(S.region_name(r["country"], r["region"]))
    flying = sorted(fly.values(), key=lambda x: -x["n"])[:12]
    for f in flying:
        f["regions"] = sorted(f["regions"])[:4]

    # Saisonverlauf (alle Jahre, je Gattung bzw. je Art der gewählten Gattung)
    weeks = list(range(14, 45))
    groups: dict = {}
    for r in base:
        key = r["genus"] if not g else _taxon_key(r)
        groups.setdefault(key, []).append(r)
    series = []
    for i, (key, lst) in enumerate(sorted(groups.items(), key=lambda x: -len(x[1]))[:6]):
        cnt = [0] * len(weeks)
        years = {r["flight_date"][:4] for r in lst}
        for r in lst:
            w = _week(r["flight_date"])
            if w in weeks:
                cnt[weeks.index(w)] += 1
        n_years = max(1, len(years))
        series.append({"label": key, "color": PALETTE[i % len(PALETTE)],
                       "data": [round(c / n_years, 2) for c in cnt]})
    week_titles = [f"KW {w} · {S.week_label(w, lang)}" for w in weeks]
    week_ticks = []
    last_m = None
    for w in weeks:
        m = date.fromisocalendar(2025, w, 3).month
        week_ticks.append(month_names[m - 1][:3] if m != last_m else "")
        last_m = m

    hours = [0] * 24
    for r in base:
        if r["flight_time"]:
            hours[int(r["flight_time"][:2])] += 1
    lo = next((i for i, v in enumerate(hours) if v), 6)
    hi = 23 - next((i for i, v in enumerate(reversed(hours)) if v), 1)
    hrs = {"labels": [f"{h}" for h in range(lo, hi + 1)], "data": hours[lo:hi + 1]}
    tb = list(range(5, 41, 3))
    tcnt = [0] * len(tb)
    for r in base:
        tv = S.temp_of(r)
        if tv is not None:
            for i, b in enumerate(tb):
                if b <= tv < b + 3:
                    tcnt[i] += 1
    temps = {"labels": [f"{b}–{b + 2}" for b in tb], "data": tcnt}

    # Steckbriefe (alle Jahre)
    summ = S.summarize(base)
    taxa = []
    for x in summ["taxa"][:40]:
        win = f"{S.week_label(x['w_from'], lang)} {t('sw_to')} {S.week_label(x['w_to'], lang)}" if x["w_from"] else "?"
        peak = S.week_label(x["w_peak"], lang) if x["w_peak"] else "?"
        hours_s = (f"{x['h_from']}–{x['h_to']} {t('sw_oclock')}" if x["h_from"] is not None and x["h_from"] != x["h_to"]
                   else (f"≈ {x['hour']} {t('sw_oclock')}" if x["hour"] is not None else ""))
        temps_s = (f"{x['t_from']}–{x['t_to']} °C" if x["t_from"] is not None and x["t_from"] != x["t_to"]
                   else (f"≈ {x['temp']} °C" if x["temp"] is not None else ""))
        mx = max(x["months"]) or 1
        taxa.append({"name": x["name"], "n": x["n"], "win": win, "peak": peak,
                     "months": [m / mx for m in x["months"]], "hours": hours_s, "temps": temps_s,
                     "regions": [n for n, _ in x["regions"]],
                     "art": art_slug(x["name"]) if " " in x["name"] else None})

    abroad_all = await S.rows(bot, include_indoor=True, scope="abroad")
    unclear_n = len({r["message_id"] for r in await S.rows(bot, include_indoor=True, scope="unclear")
                     if not r["indoor"]}) if member else 0   # wie die Admin-Seite: ohne Zimmerflüge
    pool = sorted([r for r in all_rows + abroad_all if not g or r["genus"] == g],
                  key=lambda r: (r["flight_date"], r["flight_time"] or ""), reverse=True)
    inside = [r for r in pool if inr(r["flight_date"])]
    recent = []
    for r in (inside[:40] if inside else pool[:15]):
        recent.append({**r, "abroad": r["country"] not in S.DACH,
                       "region_name": S.region_name(r["country"], r["region"]) if r["country"] else "",
                       "link": link(r["message_id"]) if member else "", "place": r["place"] if member else ""})

    k = {"total": len(sel), "taxa": len({_taxon_key(r) for r in sel if r["species"]}),
         "genera": len({r["genus"] for r in sel}), "regions": len(regions),
         "reporters": (lambda n: "< 3" if 0 < n < 3 else n)(len({r["author_id"] for r in sel if r["author_id"]}))}
    legend = [(c, lbl) for c, lbl in zip(["#1b2a1f", "#1f4d2c", "#2a7a3e", "#3fb950", "#a6f0b5"],
                                         ["1–2", "3–8", "9–20", "21–50", "> 50"])]
    data = {"regions": regions, "names": names, "top": top, "pins": pins, "gcol": gcol,
            "series": series, "weeks": [f"KW {w}" for w in weeks], "weekTitles": week_titles,
            "weekTicks": week_ticks, "hours": hrs, "temps": temps,
            "t": {"reports": t("sw_kpi_reports"), "perWeek": t("sw_per_week")}, "lang": lang}
    # Zeitraffer: Meldungen je Region und Kalenderwoche (Auswahl)
    tl: dict = {}
    for r in sel:
        if r["country"] and r["region"]:
            w = _week(r["flight_date"])
            if w in weeks:
                kk = f"{r['country']}-{r['region']}"
                tl.setdefault(w, {}).setdefault(kk, 0)
                tl[w][kk] += 1
    data.update(tl=tl, wkNum=weeks, yrs=years_chart(base))
    data["t"].update(tlAll=t("sw_tl_all"), tlPlay=t("sw_tl_play"), tlPause=t("sw_tl_pause"),
                     median=t("sw_years_axis"))
    # Bestimmungshilfe
    q = q or {}
    ident = {"asked": False, "date": today.isoformat(), "hour": None, "region": "", "temp": None, "cands": []}
    try:
        if q.get("wd"):
            ident["date"] = date.fromisoformat(q["wd"][:10]).isoformat()
            ident["asked"] = True
        if (q.get("wt") or "").isdigit():
            ident["hour"] = max(0, min(23, int(q["wt"])))
        if q.get("wtemp") not in (None, ""):
            ident["temp"] = max(-5.0, min(45.0, float(str(q["wtemp"]).replace(",", "."))))
    except ValueError:
        pass
    ident["region"] = (q.get("wr") or "")[:8]
    if ident["asked"]:
        ident["cands"] = identify(outdoor, date.fromisoformat(ident["date"]), ident["hour"], ident["region"],
                                  ident["temp"], t)
        for c in ident["cands"]:
            c["art"] = art_slug(c["name"])
    cmp = compare_years(base, year_list, q, lang, t)
    if cmp:
        data["cmp"] = cmp.pop("data")
    wx, wx_n = weather_table(base, t)
    from utils import forecast as FC
    try:
        data["learn"] = [p for p in await FC.learn_map(bot) if member or not p[2]]   # Hotspots nur für Mitglieder
    except Exception:
        data["learn"] = []
    data["wreg"], wreg_names = weather_regions(base)
    data["names"] = {**wreg_names, **data["names"]}
    clim = await climate_ctx(bot, (q or {}).get("ct", ""), g, member)
    if clim.get("data"):
        data["clim"] = clim.pop("data")
    data["t"].update(lrnHot=t("sw_lrn_hot"), lrnGrid=t("sw_lrn_grid"), lrnOf=t("sw_lrn_of"),
                     lrnFine=t("sw_lrn_fine"), wrVars={k: t("sw_wr_" + k) for k in WR_VARS},
                     wrFew=t("sw_wr_few"), wrN=t("sw_wr_n"), clDays=t("sw_cl_days"))
    prog = await progress(bot, t)
    fc = await forecast_ctx(bot, member, q, t)
    if fc.get("data"):
        data["fc"] = fc.pop("data")
    data["t"]["fcNone"] = t("fc_region_none")
    return {"z": z, "g": g, "ranges": RANGES, "genera": genera, "flying": flying, "k": k, "data": data,
            "ident": ident, "regions_sel": regions_select(t, lang), "wx": wx, "wx_n": wx_n, "cmp": cmp,
            "season": season_info(lang), "fc": fc, "prog": prog, "clim": clim,
            **_abroad_ctx(abroad_all, g, inr, member, link, lang), "unclear_n": unclear_n,
            "year_list": year_list, "home_n": len({r["message_id"] for r in sel if r.get("geo_src") == "home"}),
            "recent": recent, "taxa": taxa, "legend": legend, "member": member,
            "first_year": (min(r["flight_date"][:4] for r in all_rows) if all_rows else str(today.year)),
            "last_date": max((r["flight_date"] for r in outdoor), default=""),
            "season_year": today.year if today.month >= 3 else today.year - 1,
            "month_names": month_names,
            "osm": bool(SWARM_OSM_GEOCODING and any(r["geo_src"] == "osm" for r in all_rows))}


async def art_box(bot, name: str, lang: str = "de") -> dict | None:
    """Kasten „Schwarmflüge in der Community“ auf der Artenseite."""
    parts = name.split()
    if len(parts) < 2:
        return None
    gen, sp = parts[0], parts[1].lower()
    try:
        rs = [dict(r) for r in await execute_db(bot,
            "SELECT * FROM swarm_reports WHERE genus=? AND indoor=0 AND country IN ('de','at','ch','li')",
            (gen,), fetch=True) or []]
    except Exception:
        return None
    mine = [r for r in rs if r["species"] == sp]
    if not mine:
        return {"n": 0, "genus_n": len(rs), "genus": gen} if rs else None
    s = S.summarize([{**r, "species": sp} for r in mine])
    x = s["taxa"][0] if s["taxa"] else None
    months = [0] * 12
    for r in mine:
        months[int(r["flight_date"][5:7]) - 1] += 1
    mx = max(months) or 1
    return {"n": len(mine), "genus_n": len(rs), "genus": gen, "months": [m / mx for m in months],
            "years": len({r["flight_date"][:4] for r in mine}),
            "last": max(r["flight_date"] for r in mine),
            "peak": S.week_label(x["w_peak"], lang) if x else None,
            "win": (f"{S.week_label(x['w_from'], lang)} {dict(de='bis', en='to', eo='ĝis').get(lang, 'bis')} {S.week_label(x['w_to'], lang)}" if x and x["w_from"] else None),
            "hours": (f"{x['h_from']}–{x['h_to']}" if x and x["h_from"] is not None else None),
            "temps": (f"{x['t_from']}–{x['t_to']}" if x and x["t_from"] is not None else None),
            "regions": [n for n, _ in x["regions"]] if x else []}


