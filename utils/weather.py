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
utils/weather.py – Wetter zur Flugzeit für Schwarmflug-Meldungen (Open-Meteo).

Quelle: Open-Meteo (https://open-meteo.com), Code AGPLv3, Daten CC BY 4.0, kostenlos für
nicht-kommerzielle Nutzung ohne API-Schlüssel. Grenzen der freien API laut Open-Meteo:
10.000 Aufrufe/Tag, 5.000/Stunde, 600/Minute. Wir bleiben weit darunter (höchstens
WEATHER_PER_RUN Abfragen je Stundenlauf, 1,1 s Abstand).

Übertragen werden nur eine auf 0,1° gerundete Koordinate (≈ 10 km) und das Datum, keine
Angaben zur Person. Je (Koordinate, Tag) wird nur einmal gefragt.

Wann: stündlich für jede Meldung im Freien mit Koordinate (auflösbarer Ort), auch wenn eine
Temperatur angegeben ist; die Angabe aus der Meldung wird nie überschrieben, fehlt sie, zählt
der Wetterwert als „geschätzt“. Meldungen nur mit Regionsrolle bekommen kein Wetter (zu grob).
Ohne Uhrzeit gilt die typische Flugzeit der Art (Median der Meldungen), sonst 17 Uhr.

Ohne Uhrzeit wird der Mittelwert von zwei Stunden vor bis zwei Stunden nach der typischen
Flugzeit genommen (±2 h), mit Uhrzeit der Wert dieser Stunde.

Quellen je Datum: letzte 7 Tage Forecast API; ab 2023 Historical Forecast API (archivierte
hochauflösende Vorhersagemodelle, dieselben wie die Prognose); davor Historical Weather API
(Standard ECMWF IFS 9 km ab 2017). Außerhalb der Saison werden Meldungen ab 2023, die noch mit
einer gröberen Quelle nachgetragen wurden, mit der feineren erneut abgefragt.

Ältere Tage: Historical Weather API (archive-api, ERA5 u. a., ~5 Tage Verzug),
die letzten Wochen: Forecast API mit start_date/end_date.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import aiohttp

from utils.db import execute_db
from utils.timez import BERLIN

logger = logging.getLogger(__name__)

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"
HIST_FC_FROM = date(2023, 1, 1)
FORECAST = "https://api.open-meteo.com/v1/forecast"
HOURLY = "temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,cloud_cover,precipitation"
WEATHER_PER_RUN = 300
_GAP = 1.1
DEFAULT_HOUR = 17          # ohne Uhrzeit und ohne bekannte Flugzeit der Art


def src_of(day: date, today: date) -> str:
    if (today - day).days <= 7:
        return "fc"
    return "hf" if day >= HIST_FC_FROM else "ifs"


def src_url(day: date, today: date) -> str:
    return {"fc": FORECAST, "hf": HIST_FC, "ifs": ARCHIVE}[src_of(day, today)]


async def _fetch(session, lat: float, lon: float, day: date) -> dict | None:
    today = datetime.now(BERLIN).date()
    url = src_url(day, today)
    params = {"latitude": f"{lat:.1f}", "longitude": f"{lon:.1f}", "hourly": HOURLY,
              "start_date": (day - timedelta(days=1)).isoformat(), "end_date": day.isoformat(),
              "timezone": "Europe/Berlin"}
    try:
        async with session.get(url, params=params) as r:
            if r.status == 429:
                return {"limit": True}
            if r.status != 200:
                logger.info("🌦️ Open-Meteo HTTP %s (%s)", r.status, day)
                return None
            return await r.json()
    except Exception as e:
        logger.info("🌦️ Open-Meteo nicht erreichbar: %s", e)
        return {"error": True}


def _pick(js: dict, day: date, hour: int, spread: int = 0) -> dict | None:
    """Werte zur Stunde (spread=0) bzw. Mittel über hour±spread. Regen = Summe der 24 h davor."""
    h = js.get("hourly") or {}
    times = h.get("time") or []
    key = f"{day.isoformat()}T{hour:02d}:00"
    if key not in times:
        return None
    i = times.index(key)
    idx = [j for j in range(i - spread, i + spread + 1) if 0 <= j < len(times)]

    def v(name):
        arr = h.get(name) or []
        xs = [arr[j] for j in idx if j < len(arr) and arr[j] is not None]
        return round(sum(xs) / len(xs), 1) if xs else None
    rain = [x for x in (h.get("precipitation") or [])[max(0, i - 24):i] if x is not None]
    out = {"w_temp": v("temperature_2m"), "w_hum": v("relative_humidity_2m"), "w_press": v("pressure_msl"),
           "w_wind": v("wind_speed_10m"), "w_cloud": v("cloud_cover"),
           "w_rain24": round(sum(rain), 1) if rain else None}
    return out if out["w_temp"] is not None else None


async def typical_hours(bot) -> dict:
    """Median-Flugstunde je Art und je Gattung (ab 3 Meldungen mit Uhrzeit)."""
    from statistics import median
    rows = await execute_db(bot, "SELECT genus, species, flight_time FROM swarm_reports "
                                 "WHERE flight_time IS NOT NULL", fetch=True) or []
    by: dict = {}
    for r in rows:
        h = int(r["flight_time"][:2])
        by.setdefault(r["genus"], []).append(h)
        if r["species"]:
            by.setdefault(f"{r['genus']} {r['species']}", []).append(h)
    return {k: int(median(v)) for k, v in by.items() if len(v) >= 3}


async def backfill(bot, limit: int = WEATHER_PER_RUN) -> int:
    """Trägt für offene Meldungen mit Koordinate das Wetter nach. Gibt die Zahl
    aktualisierter Zeilen zurück."""
    today = datetime.now(BERLIN).date()
    off_season = today.month not in range(3, 11)
    rows = [dict(r) for r in await execute_db(bot,
        "SELECT id, lat, lon, flight_date, flight_time, genus, species, w_src FROM swarm_reports "
        "WHERE lat IS NOT NULL AND indoor=0 AND flight_date<=? AND (w_state IS NULL OR "
        "(? AND w_state=1 AND COALESCE(w_src,'')<>'hf' AND flight_date>=? AND flight_date<=?)) "
        "ORDER BY w_state IS NOT NULL, flight_date DESC",
        (today.isoformat(), 1 if off_season else 0, HIST_FC_FROM.isoformat(),
         (today - timedelta(days=8)).isoformat()), fetch=True) or []]
    if not rows:
        return 0
    typ = await typical_hours(bot)
    groups: dict = {}
    for r in rows:
        k = (round(r["lat"], 1), round(r["lon"], 1), r["flight_date"])
        groups.setdefault(k, []).append(r)
    done = calls = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                     headers={"User-Agent": "AAM-Discord-Bot (Schwarmflug-Radar)"}) as s:
        for (lat, lon, d), lst in groups.items():
            if calls >= limit:
                break
            from utils import forecast as FC
            if await FC.budget_left(bot, "weather") < 1:
                break
            try:
                day = date.fromisoformat(d)
            except ValueError:
                continue
            js = await _fetch(s, lat, lon, day)
            calls += 1
            await FC.budget_use(bot, "weather", 1)
            await asyncio.sleep(_GAP)
            if js and js.get("limit"):
                await FC.budget_block(bot)
                break
            if js and js.get("error"):
                break                                   # später erneut
            src = src_of(day, today)
            for r in lst:
                if r["flight_time"]:
                    hour, spread = int(r["flight_time"][:2]), 0
                else:
                    hour = typ.get(f"{r['genus']} {r['species']}", typ.get(r["genus"], DEFAULT_HOUR))
                    spread = 2
                w = _pick(js, day, hour, spread) if js else None
                if w:
                    await execute_db(bot, "UPDATE swarm_reports SET w_temp=?, w_hum=?, w_press=?, w_wind=?, "
                                          "w_cloud=?, w_rain24=?, w_state=1, w_src=? WHERE id=?",
                                     (w["w_temp"], w["w_hum"], w["w_press"], w["w_wind"], w["w_cloud"],
                                      w["w_rain24"], src, r["id"]), commit=True)
                    done += 1
                elif r.get("w_src"):
                    continue                            # Neuabfrage fehlgeschlagen: alter Wert bleibt
                elif js is not None or (today - day).days > 14:
                    await execute_db(bot, "UPDATE swarm_reports SET w_state=-1 WHERE id=?", (r["id"],), commit=True)
    if done:
        logger.info("🌦️ Wetter für %d Schwarmflug-Meldungen nachgetragen (%d Abfragen)", done, calls)
    return done
