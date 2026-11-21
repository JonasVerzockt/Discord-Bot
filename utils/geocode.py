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
utils/geocode.py – Ortsnamen → Koordinate + Region (für das Schwarmflug-Radar).

Reihenfolge:
  1. Cache (Tabelle geocode_cache, auch „nicht gefunden“ wird gemerkt)
  2. lokal: PLZ im Text (data/plz_dach.csv) oder exakter Ortsname aus derselben Datei
  3. OpenStreetMap Nominatim (nur wenn SWARM_OSM_GEOCODING an ist)

Nominatim-Nutzungsrichtlinie (https://operations.osmfoundation.org/policies/nominatim/):
höchstens 1 Anfrage pro Sekunde, eindeutiger User-Agent, Ergebnisse cachen, Quellenangabe
„© OpenStreetMap-Mitwirkende“. Deshalb: globale Sperre mit ≥ 1,1 s Abstand, jeder Suchtext
nur einmal, keine Autovervollständigung. Übertragen wird nur der Ortstext der Meldung,
niemals Name oder Discord-ID.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp

from utils import geo
from utils.db import execute_db

logger = logging.getLogger(__name__)

_URL = "https://nominatim.openstreetmap.org/search"
_LOCK = asyncio.Lock()
_LAST = 0.0
_GAP = 1.1
_PLACES: dict[tuple[str, str], dict] | None = None
_PLACES_MTIME = None

# Bundesland-/Kantonsname aus Nominatim (address.state) → (Land, ISO-Code)
_STATE: dict[str, tuple[str, str]] = {}
for _c, _t in geo.all_regions().items():
    for _code, _name in _t.items():
        _STATE[_name.lower()] = (_c, _code)
_STATE.update({"bavaria": ("de", "BY"), "north rhine-westphalia": ("de", "NW"), "lower saxony": ("de", "NI"),
               "hesse": ("de", "HE"), "saxony": ("de", "SN"), "saxony-anhalt": ("de", "ST"),
               "thuringia": ("de", "TH"), "rhineland-palatinate": ("de", "RP"),
               "mecklenburg-western pomerania": ("de", "MV"), "upper austria": ("at", "4"),
               "lower austria": ("at", "3"), "carinthia": ("at", "2"), "styria": ("at", "6"),
               "tyrol": ("at", "7"), "vienna": ("at", "9"), "kanton zürich": ("ch", "ZH")})
for _n, _v in list(_STATE.items()):
    _STATE.setdefault("kanton " + _n, _v)


def _norm(q: str) -> str:
    q = re.sub(r"\s+", " ", (q or "").strip().lower())
    return q.strip(" ,.;:-")[:120]


def _local_places() -> dict:
    """Ortsname (klein) + Land → Mittelpunkt aller PLZ dieses Orts (aus plz_dach.csv)."""
    global _PLACES, _PLACES_MTIME
    geo._load()
    if _PLACES is not None and _PLACES_MTIME == geo._mtime:
        return _PLACES
    agg: dict = {}
    for (c, _plz), rec in geo._DATA.items():
        name = (rec.get("place") or "").strip().lower()
        if not name:
            continue
        a = agg.setdefault((c, name), [0.0, 0.0, 0, rec["region_code"]])
        a[0] += rec["lat"]; a[1] += rec["lon"]; a[2] += 1
    _PLACES = {k: {"lat": round(v[0] / v[2], 5), "lon": round(v[1] / v[2], 5), "country": k[0],
                   "region": v[3], "src": "plz"} for k, v in agg.items()}
    _PLACES_MTIME = geo._mtime
    return _PLACES


_PRE = re.compile(r"^(?:(?:in\s+der\s+)?n(?:ä|ae)he(?:\s+von)?|nahe|naeher|näher|raum|region|bei|in|im|lk\.?|landkreis|"
                  r"kreis|gemeinde|stadt|umgebung(?:\s+von)?|nördlich(?:\s+von)?|südlich(?:\s+von)?|"
                  r"östlich(?:\s+von)?|westlich(?:\s+von)?|irgendwo\s+(?:bei|in|zwischen))\s+", re.I)
_POST = re.compile(r"\s+(?:umgebung|umland|innenstadt|außerhalb|ausserhalb|zentrum|uni|nord|süd|sued|ost|west|"
                   r"und\s+umgebung|landkreis|lkr\.?|land)\s*$", re.I)


def clean_place(place: str) -> str:
    """„Nähe Schweinfurt“, „LK Onsabrück“, „Weinheim (Nähe Mannheim)“ → Kernort für die Suche."""
    p = re.sub(r"\([^)]*\)", " ", place or "")
    p = re.split(r"[,;/]| - ", p)[0]
    p = re.sub(r"\d{4,5}", " ", p)
    p = re.sub(r"\s+", " ", p).strip(" .`'\"-")
    for _ in range(2):
        p = _PRE.sub("", p).strip()
        p = _POST.sub("", p).strip()
    return p


def _fuzzy_local(name: str, country: str = "") -> dict | None:
    """Tippfehler im Ortsnamen („Onsabrück“) gegen die Ortsliste, nur bei hoher Ähnlichkeit."""
    if len(name) < 7:
        return None
    try:
        from rapidfuzz import process, fuzz
    except Exception:
        return None
    places = _local_places()
    keys = [k for k in places if not country or k[0] == country]
    hit = process.extractOne(name.lower(), [k[1] for k in keys], scorer=fuzz.ratio, score_cutoff=88)
    return dict(places[keys[hit[2]]]) if hit else None


def local(place: str, country: str = "") -> dict | None:
    """PLZ im Text oder exakter Ortsname. Kein Netz."""
    m = re.search(r"(?<!\d)(\d{4,5})(?!\d)", place or "")          # auch „1210Wien“
    for c in ([country] if country else []) + [c for c in geo.DACH if c != country]:
        if m and geo.valid_format(c, m.group(1)):
            r = geo.resolve(c, m.group(1))
            if r and r.get("place"):
                return {"lat": r["lat"], "lon": r["lon"], "country": c, "region": r["region_code"], "src": "plz"}
    places = _local_places()
    if not places:
        return None
    # „Köln Agnesviertel“, „Jena (Thüringen) Innenstadt“ → erster Teil zählt
    head = re.split(r"[,(/]| bei | nahe | in der | im ", (place or ""), maxsplit=1)[0]
    cands = [_norm(head)]
    words = cands[0].split()
    if len(words) > 1:
        cands += [" ".join(words[:2]), words[0]]
    core = _norm(clean_place(place))
    if core and core not in cands:
        cands.append(core)
    for cand in cands:
        for c in ([country] if country else list(geo.DACH)):
            hit = places.get((c, cand))
            if hit:
                return dict(hit)
    return _fuzzy_local(core, country) if core else None


async def _cache_get(bot, key):
    r = await execute_db(bot, "SELECT lat, lon, country, region, src FROM geocode_cache WHERE query=?", (key,), fetch=True)
    return dict(r[0]) if r else None


async def _cache_put(bot, key, res):
    res = res or {}
    await execute_db(bot, "INSERT OR REPLACE INTO geocode_cache (query, lat, lon, country, region, src) "
                          "VALUES (?,?,?,?,?,?)",
                     (key, res.get("lat"), res.get("lon"), res.get("country") or "", res.get("region") or "",
                      res.get("src") or "none"), commit=True)


async def _nominatim(q: str, world: bool = False) -> dict | None:
    global _LAST
    from config import BOARD_PUBLIC_URL
    try:
        from config import VERSION as _V
    except Exception:
        _V = "1"
    headers = {"User-Agent": f"AAM-Discord-Bot/{_V} (+{BOARD_PUBLIC_URL or 'https://github.com'})",
               "Accept-Language": "de"}
    params = {"q": q, "format": "jsonv2", "addressdetails": "1", "limit": "1"}
    if not world:
        params["countrycodes"] = "de,at,ch,li"
    async with _LOCK:
        wait = _GAP - (time.monotonic() - _LAST)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(_URL, params=params, headers=headers) as r:
                    _LAST = time.monotonic()
                    if r.status != 200:
                        logger.info("Nominatim HTTP %s für %r", r.status, q)
                        return {"error": True}
                    data = await r.json()
        except Exception as e:
            _LAST = time.monotonic()
            logger.info("Nominatim nicht erreichbar: %s", e)
            return {"error": True}
    if not data:
        return None
    hit = data[0]
    addr = hit.get("address") or {}
    cc = (addr.get("country_code") or "").lower()
    st = (addr.get("state") or addr.get("county") or "").lower()
    c, code = _STATE.get(st, (cc, ""))
    if cc == "li":
        c, code = "li", "LI"
    try:
        return {"lat": round(float(hit["lat"]), 5), "lon": round(float(hit["lon"]), 5),
                "country": c or cc, "region": code, "src": "osm"}
    except (KeyError, ValueError):
        return None


async def locate(bot, place: str, country: str = "", region: str = "") -> dict | None:
    """Koordinate + Region zu einem Ortstext (siehe locate_status)."""
    return (await locate_status(bot, place, country, region))[0]


async def locate_status(bot, place: str, country: str = "", region: str = "") -> tuple[dict | None, str]:
    """(Ergebnis, Status). Status: ok | none (endgültig nicht gefunden, gecacht) | skip (kein
    sinnvoller Ort, z. B. „?“) | error (Netz/Sperre, später erneut). Passt die Region nicht zur
    genannten Regionsrolle, wird das Ergebnis verworfen (Namensgleichheit anderswo)."""
    from config import SWARM_OSM_GEOCODING
    if not place or len(re.findall(r"[A-Za-zÄÖÜäöüß]", place)) < 3 and not re.search(r"\d{4,5}", place):
        return None, "skip"
    key = _norm(f"{place}|{country}")
    cached = await _cache_get(bot, key)
    if cached is not None:
        if cached.get("lat") is not None:
            return cached, "ok"
        if cached.get("country") and cached["country"] not in geo.DACH:
            return cached, "abroad"
        return None, "none"
    res = local(place, country)
    core = clean_place(place)
    if res is None and SWARM_OSM_GEOCODING:
        rname = geo.all_regions().get(country, {}).get(region, "") if region else ""
        tries = [f"{place}, {rname}".strip(", ")]
        if core and core.lower() != place.lower():
            tries.append(f"{core}, {rname}".strip(", "))
        for q in tries:
            res = await _nominatim(q)
            if res and res.get("error"):
                return None, "error"          # Netzfehler: nicht cachen, später erneut
            if res:
                break
        if res is None and not region:
            # Weltweit: liegt der Ort im Ausland, wird es eine Auslandsmeldung (ohne Koordinate)
            w = await _nominatim(core or place, world=True)
            if w and w.get("error"):
                return None, "error"
            if w and w.get("country") and w["country"] not in geo.DACH:
                res = {"lat": None, "lon": None, "country": w["country"], "region": "", "src": "abroad"}
            elif w:
                res = w
    if res and region and res.get("region") and (res["country"], res["region"]) != (country, region):
        res = None
    await _cache_put(bot, key, res)
    if res and res.get("src") == "abroad":
        return res, "abroad"
    return (res, "ok") if res else (None, "none")
