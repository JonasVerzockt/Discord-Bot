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
utils/geo.py – Offline-Geocoding + Fuzzing für die Halter-Karte.

Ziel: KEINE Live-Geocoding-Calls. PLZ → (Land, Region, grobe Koordinate) aus einem
lokalen Datensatz (data/plz_dach.csv). Fehlt die Datei, greift ein eingebauter,
grober DACH-Fallback nach PLZ-Leitziffer (nur ungefähre Regions-Zentroide) – damit
das Feature ohne den großen Datensatz lauffähig ist.

Datensatz-Format (data/plz_dach.csv, Header):
    country,plz,place,region_code,region_name,lat,lon
Die Datei baut der Bot automatisch (utils/map_geodata.py) aus den GeoNames-PLZ-Dumps
(CC BY 4.0). Nur gefuzzte Koordinaten verlassen das Backend.

Fuzzing: fester, aus der user_id deterministisch geseedeter Versatz innerhalb eines
Radius (Standard ±3 km). Stabil (springt nicht) → keine Rekonstruktion per Mittelung.
"""
from __future__ import annotations

import csv
import math
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DACH = ("de", "at", "ch", "li")
# Gültige PLZ-Länge je Land (DE 5-stellig, AT/CH/LI 4-stellig).
PLZ_LEN = {"de": 5, "at": 4, "ch": 4, "li": 4}
# "Grobes PLZ-Gebiet": die letzten zwei Ziffern werden weggelassen (DE 3, AT/CH/LI 2 Ziffern).
COARSE_DROP = 2

try:
    from config import DATA_DIR
    _CSV = Path(DATA_DIR) / "plz_dach.csv"
except Exception:                       # pragma: no cover - config evtl. gestubbt
    _CSV = Path("data/plz_dach.csv")

# Geladener Datensatz: (country, plz) -> record-dict. Lazy, mtime-gecacht.
_DATA: dict[tuple[str, str], dict] = {}
_COARSE: dict[tuple[str, str], tuple[float, float]] = {}   # (country, gekürzte PLZ) -> Mitte
_mtime: float | None = None

# ── Regionen vereinheitlichen (ISO 3166-2, wie in den geoBoundaries-Umrissen) ──
# GeoNames liefert für DE gemischte Codes ("11" und "BB" = Brandenburg), für AT "01".."09"
# und teils englische/französische Namen. Für Zählung und Anzeige wird alles auf den
# ISO-Code (ohne Länderpräfix, z. B. "BB", "9", "ZH") und einen deutschen Namen gebracht.
_REGIONS = {
    "de": {"BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg",
           "HB": "Bremen", "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern",
           "NI": "Niedersachsen", "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz",
           "SL": "Saarland", "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein",
           "TH": "Thüringen"},
    "at": {"1": "Burgenland", "2": "Kärnten", "3": "Niederösterreich", "4": "Oberösterreich",
           "5": "Salzburg", "6": "Steiermark", "7": "Tirol", "8": "Vorarlberg", "9": "Wien"},
    "ch": {"AG": "Aargau", "AI": "Appenzell Innerrhoden", "AR": "Appenzell Ausserrhoden",
           "BE": "Bern", "BL": "Basel-Landschaft", "BS": "Basel-Stadt", "FR": "Freiburg",
           "GE": "Genf", "GL": "Glarus", "GR": "Graubünden", "JU": "Jura", "LU": "Luzern",
           "NE": "Neuenburg", "NW": "Nidwalden", "OW": "Obwalden", "SG": "St. Gallen",
           "SH": "Schaffhausen", "SO": "Solothurn", "SZ": "Schwyz", "TG": "Thurgau",
           "TI": "Tessin", "UR": "Uri", "VD": "Waadt", "VS": "Wallis", "ZG": "Zug",
           "ZH": "Zürich"},
}
_DE_NUM = {"01": "BW", "02": "BY", "03": "HB", "04": "HH", "05": "HE", "06": "NI",
           "07": "NW", "08": "RP", "09": "SL", "10": "SH", "11": "BB", "12": "MV",
           "13": "SN", "14": "ST", "15": "TH", "16": "BE"}
# Alte Fallback-Kürzel (vor der Vereinheitlichung gespeichert) -> ISO
_AT_OLD = {"B": "1", "K": "2", "NO": "3", "OO": "4", "S": "5", "ST": "6", "T": "7",
           "V": "8", "W": "9"}


def all_regions() -> dict:
    """Alle DACH-Regionen {land: {code: name}} (für Statistik „Regionen ohne Halter“).
    Liechtenstein zählt als eine Region (Code "LI")."""
    out = {c: dict(t) for c, t in _REGIONS.items()}
    out["li"] = {"LI": "Liechtenstein"}
    return out


def canon_region(country: str, code: str, name: str = "") -> tuple[str, str]:
    """(ISO-Regionscode ohne Länderpräfix, deutscher Name). Unbekanntes bleibt unverändert.
    Liechtenstein wird als Ganzes gezählt (Code "LI"); der Gemeindename bleibt erhalten."""
    c = (country or "").strip().lower()
    k = (code or "").strip().upper()
    if c == "li":
        return "LI", (name or "Liechtenstein")
    if c == "de":
        k = _DE_NUM.get(k, k)
    elif c == "at":
        k = _AT_OLD.get(k, k.lstrip("0") or k)
    table = _REGIONS.get(c, {})
    if k in table:
        return k, table[k]
    return k, name or ""


# ── Grober Fallback: PLZ-Leitziffer → (region_code, region_name, lat, lon) ──────
# NUR ungefähre Regions-Zentroide als Rückfallebene ohne Datensatz. Bewusst grob.
_FALLBACK = {
    "de": {
        "0": ("SN", "Sachsen/Thüringen",        51.05, 13.74),
        "1": ("BE", "Berlin/Brandenburg",       52.52, 13.40),
        "2": ("HH", "Hamburg/Niedersachsen (N)",53.55, 10.00),
        "3": ("NI", "Niedersachsen/Hessen (N)", 52.37,  9.73),
        "4": ("NW", "Nordrhein-Westfalen (N)",  51.51,  7.47),
        "5": ("NW", "Nordrhein-Westfalen/RLP",  50.94,  6.96),
        "6": ("HE", "Hessen/RLP/Saarland",      50.11,  8.68),
        "7": ("BW", "Baden-Württemberg",        48.78,  9.18),
        "8": ("BY", "Bayern (Süd)",             48.14, 11.58),
        "9": ("BY", "Bayern (Nord)/Thüringen",  49.45, 11.08),
    },
    "at": {
        "1": ("9",  "Wien",                     48.21, 16.37),
        "2": ("3",  "Niederösterreich",         48.20, 15.63),
        "3": ("3",  "Niederösterreich/OÖ",      48.30, 15.00),
        "4": ("4",  "Oberösterreich",           48.31, 14.29),
        "5": ("5",  "Salzburg",                 47.81, 13.04),
        "6": ("7",  "Tirol/Vorarlberg",         47.27, 11.39),
        "7": ("1",  "Burgenland",               47.85, 16.52),
        "8": ("6",  "Steiermark",               47.07, 15.44),
        "9": ("2",  "Kärnten",                  46.62, 14.31),
    },
    "ch": {
        "1": ("VD", "Waadt/Genf",               46.52,  6.63),
        "2": ("NE", "Neuenburg/Jura",           47.00,  6.93),
        "3": ("BE", "Bern",                     46.95,  7.44),
        "4": ("BS", "Basel",                    47.56,  7.59),
        "5": ("AG", "Aargau",                   47.39,  8.04),
        "6": ("LU", "Zentralschweiz",           47.05,  8.31),
        "7": ("GR", "Graubünden",               46.85,  9.53),
        "8": ("ZH", "Zürich",                   47.37,  8.54),
        "9": ("SG", "Ostschweiz/St. Gallen",    47.42,  9.37),
    },
    "li": {
        "9": ("LI", "Liechtenstein",            47.14,  9.52),
    },
}


def is_dach(country: str) -> bool:
    return (country or "").strip().lower() in DACH


def load() -> None:
    """Lädt den PLZ-Datensatz vorab (z. B. beim Bot-Start). Idempotent (mtime-Cache);
    danach liegen die Daten im Speicher und es gibt keinen Datei-Zugriff pro Abfrage."""
    _load()


def count() -> int:
    """Anzahl geladener PLZ-Einträge (0 = nur Leitziffer-Fallback aktiv)."""
    _load()
    return len(_DATA)


def plz_prefix_centroids() -> dict:
    """Zentroide je 2-stelligem PLZ-Gebiet (Leitregion) aus dem PLZ-Datensatz.
    Rückgabe: {(country, prefix2): (lat, lon)} – für die PLZ-Ebene der Karte (Blasen).
    Leer, wenn kein Datensatz geladen ist."""
    _load()
    agg: dict = {}
    for (c, plz), rec in _DATA.items():
        pref = plz[:2]
        if len(pref) < 2:
            continue
        a = agg.setdefault((c, pref), [0.0, 0.0, 0])
        a[0] += rec["lat"]; a[1] += rec["lon"]; a[2] += 1
    return {k: (round(v[0] / v[2], 5), round(v[1] / v[2], 5)) for k, v in agg.items() if v[2]}


def _load() -> None:
    """Lädt data/plz_dach.csv, wenn vorhanden und geändert (mtime-Cache)."""
    global _DATA, _COARSE, _mtime
    try:
        m = _CSV.stat().st_mtime
    except FileNotFoundError:
        if _mtime is not None:
            _DATA, _COARSE, _mtime = {}, {}, None
        return
    if m == _mtime:
        return
    data: dict[tuple[str, str], dict] = {}
    try:
        with open(_CSV, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                c = (row.get("country") or "").strip().lower()
                plz = (row.get("plz") or "").strip()
                if not c or not plz:
                    continue
                try:
                    lat = float(row["lat"]); lon = float(row["lon"])
                except (KeyError, ValueError):
                    continue
                rc, rn = canon_region(c, row.get("region_code") or "",
                                      (row.get("region_name") or "").strip())
                data[(c, plz)] = {
                    "country": c, "plz": plz,
                    "place": (row.get("place") or "").strip(),
                    "region_code": rc, "region_name": rn,
                    "lat": lat, "lon": lon,
                }
        # Mittelpunkte der groben PLZ-Gebiete (alle PLZ mit gleichen Anfangsziffern).
        agg: dict = {}
        for (c, plz), rec in data.items():
            pref = plz[:max(1, len(plz) - COARSE_DROP)]
            a = agg.setdefault((c, pref), [0.0, 0.0, 0])
            a[0] += rec["lat"]; a[1] += rec["lon"]; a[2] += 1
        _COARSE = {k: (round(v[0] / v[2], 5), round(v[1] / v[2], 5)) for k, v in agg.items()}
        _DATA, _mtime = data, m
        logger.info("🗺️ PLZ-Datensatz geladen: %d Einträge (%s)", len(data), _CSV)
    except Exception as e:
        logger.error("❌ PLZ-Datensatz nicht lesbar (%s): %s", _CSV, e)


def _digits(plz: str) -> str:
    return "".join(ch for ch in (plz or "") if ch.isdigit())


def plz_prefix(plz: str) -> str:
    """PLZ-Gebiet-Präfix (Leitregion) = erste zwei Ziffern (für PLZ-Choropleth)."""
    d = _digits(plz)
    return d[:2] if len(d) >= 2 else d


def valid_format(country: str, plz: str) -> bool:
    """True, wenn die PLZ nur aus Ziffern besteht und die richtige Länge fürs Land hat."""
    c = (country or "").strip().lower()
    raw = (plz or "").strip()
    return c in PLZ_LEN and raw.isdigit() and len(raw) == PLZ_LEN[c]


def coarse_prefix(plz: str) -> str:
    """Gekürzte PLZ für das grobe Gebiet (letzte COARSE_DROP Ziffern weg)."""
    d = _digits(plz)
    return d[:max(1, len(d) - COARSE_DROP)]


def resolve(country: str, plz: str) -> dict | None:
    """
    Löst (Land, PLZ) zu grober Region + Zentroid-Koordinate auf.

    Rückgabe-dict: {country, plz, plz_prefix, place, region_code, region_name,
                    lat, lon}  (lat/lon = UNGEFUZZTE Zentroid-Koordinate)
    None, wenn Land nicht DACH ist, das PLZ-Format nicht passt oder die PLZ im
    geladenen Datensatz nicht existiert (unbekannte PLZ werden abgelehnt).
    """
    c = (country or "").strip().lower()
    if c not in DACH or not valid_format(c, plz):
        return None
    d = _digits(plz)
    _load()
    rec = _DATA.get((c, d))
    if rec:
        out = dict(rec)
        out["plz_prefix"] = plz_prefix(d)
        return out
    if any(k[0] == c for k in _COARSE):
        return None                     # Datensatz vorhanden, PLZ existiert nicht -> ablehnen
    # Nur ohne Datensatz (z. B. vor dem ersten Download): Fallback nach Leitziffer
    fb = _FALLBACK.get(c, {}).get(d[0])
    if not fb:
        # Land ist DACH, aber Leitziffer unbekannt → Landeszentroid grob
        any_fb = next(iter(_FALLBACK.get(c, {}).values()), None)
        if not any_fb:
            return None
        fb = any_fb
    code, name, lat, lon = fb
    return {
        "country": c, "plz": d, "plz_prefix": plz_prefix(d), "place": "",
        "region_code": code, "region_name": name, "lat": lat, "lon": lon,
    }


def resolve_coarse(country: str, plz: str) -> dict | None:
    """Wie resolve(), aber mit dem Mittelpunkt aller PLZ des groben Gebiets
    (letzte zwei Ziffern weggelassen) statt der Koordinate der genauen PLZ.
    Zusatzfeld "area" = gekürzte PLZ (z. B. "167" bzw. "10")."""
    rec = resolve(country, plz)
    if not rec:
        return None
    pref = coarse_prefix(rec["plz"])
    cen = _COARSE.get((rec["country"], pref))
    out = dict(rec)
    out["area"] = pref
    out["place"] = ""
    if out["country"] == "li":
        # LI-"Regionen" sind Gemeinden -> beim groben Gebiet nur das Land nennen.
        out["region_code"], out["region_name"] = "LI", "Liechtenstein"
    if cen:
        out["lat"], out["lon"] = cen
    return out


def fuzz(lat: float, lon: float, user_id, meters: int = 3000) -> tuple[float, float]:
    """
    Verschiebt (lat, lon) um einen FESTEN, aus der user_id deterministisch
    abgeleiteten Versatz (gleichverteilt in der Kreisscheibe mit Radius *meters*).
    Stabil über Aufrufe hinweg (kein Springen) → keine Mittelungs-Rekonstruktion.
    """
    h = hashlib.sha256(str(user_id).encode()).digest()
    # zwei unabhängige [0,1)-Werte aus dem Hash
    u1 = int.from_bytes(h[0:8], "big") / 2**64
    u2 = int.from_bytes(h[8:16], "big") / 2**64
    angle = 2 * math.pi * u1
    radius = meters * math.sqrt(u2)                 # sqrt → Gleichverteilung in der Fläche
    dnorth = radius * math.cos(angle)
    deast = radius * math.sin(angle)
    dlat = dnorth / 111_320.0
    dlon = deast / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return (round(lat + dlat, 5), round(lon + dlon, 5))
