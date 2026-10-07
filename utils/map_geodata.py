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
utils/map_geodata.py – Besorgt ALLE für die Halter-Karte nötigen Geodaten,
vollautomatisch und ohne externe Tools (kein ogr2ogr, keine amtlichen ZIPs).

Quellen (alle offen lizenziert, stabile Direkt-URLs):
  • Leaflet (BSD-2)                     -> static/leaflet.js|css  (Kartenbibliothek)
  • Leaflet.markercluster (MIT)         -> static/leaflet.markercluster.js + MarkerCluster*.css
  • geoBoundaries ADM1 (CC BY 4.0)      -> static/*_bundeslaender/kantone.geojson
  • GeoNames PLZ-Dumps (CC BY 4.0)      -> data/plz_dach.csv (PLZ+Ort+Region+Koordinaten)
  • GeoNames Länder-Dumps (CC BY 4.0)   -> static/map_cities.json (Orte ab 50.000 Einw. + Hauptstädte)

Aufruf aus dem Bot: `refresh()` (blockierendes I/O -> im Thread ausführen, s. cogs/map_tasks).
Attribution steht im Footer der Kartenseite. Ohne diese Dateien läuft der Bot mit dem
groben PLZ-Leitziffer-Fallback aus utils/geo.py weiter.
"""
import csv
import io
import json
import logging
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

from config import BASE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

STATIC_DIR = Path(BASE_DIR) / "static"
_UA = {"User-Agent": "AAM-Bot map-data fetch (+https://board.jonasants.de)"}

# Direkt-Downloads (url -> Zielpfad).
# WICHTIG: geoBoundaries liegt als Git-LFS -> echten Inhalt über media.githubusercontent.com/media
# laden (raw.githubusercontent.com liefert nur den LFS-Zeiger). Alternativ die geoBoundaries-API.
_GEO = "https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/main/releaseData/gbOpen"
SOURCES: list[tuple[str, Path]] = [
    ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",  STATIC_DIR / "leaflet.js"),
    ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", STATIC_DIR / "leaflet.css"),
    # Pin-Clustering: Leaflet.markercluster (MIT), Version fest gepinnt.
    ("https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js", STATIC_DIR / "leaflet.markercluster.js"),
    ("https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css",         STATIC_DIR / "MarkerCluster.css"),
    ("https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css", STATIC_DIR / "MarkerCluster.Default.css"),
    (f"{_GEO}/DEU/ADM1/geoBoundaries-DEU-ADM1_simplified.geojson", STATIC_DIR / "de_bundeslaender.geojson"),
    (f"{_GEO}/AUT/ADM1/geoBoundaries-AUT-ADM1_simplified.geojson", STATIC_DIR / "at_bundeslaender.geojson"),
    (f"{_GEO}/CHE/ADM1/geoBoundaries-CHE-ADM1_simplified.geojson", STATIC_DIR / "ch_kantone.geojson"),
    # Liechtenstein: ADM1 = Gemeinden; ADM0 als Landesumriss als Fallback.
    (f"{_GEO}/LIE/ADM1/geoBoundaries-LIE-ADM1_simplified.geojson", STATIC_DIR / "li_gemeinden.geojson"),
    (f"{_GEO}/LIE/ADM0/geoBoundaries-LIE-ADM0_simplified.geojson", STATIC_DIR / "li_land.geojson"),
]

# GeoNames-PLZ-Dumps: pro Land ein ZIP mit PLZ + Ort + Region + Koordinaten.
GEONAMES = {"de": "DE", "at": "AT", "ch": "CH", "li": "LI"}
GEONAMES_URL = "https://download.geonames.org/export/zip/{cc}.zip"
PLZ_MAX_AGE_DAYS = 25

# Städte als Orientierungspunkte: GeoNames-Länderdumps (gleiche Lizenz CC BY wie die PLZ-Daten).
# Spalten (tab-getrennt): geonameid, name, asciiname, alternatenames, lat, lon, feature class,
# feature code, country code, cc2, admin1..4, population, elevation, dem, timezone, mod. date.
CITIES_URL = "https://download.geonames.org/export/dump/{cc}.zip"
CITIES_MIN_POP = 100_000
CITY_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC"}   # ohne PPLX (Stadtteile)
CITIES_FILE = STATIC_DIR / "map_cities.json"


def _download(url: str, dest: Path, force: bool) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(Request(url, headers=_UA), timeout=60) as r:
            data = r.read()
    except Exception as e:
        return f"✗ {dest.name} ({e})" if not dest.exists() else f"✗ {dest.name} ({e}, alte Datei bleibt)"
    if not force and dest.exists() and dest.stat().st_size == len(data):
        return f"= {dest.name} unverändert"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return f"✓ {dest.name} ({len(data)} B)"


def _build_plz(force: bool) -> str:
    """Baut data/plz_dach.csv aus den GeoNames-PLZ-Dumps (idempotent, mtime-geprüft)."""
    dest = Path(DATA_DIR) / "plz_dach.csv"
    if not force and dest.exists():
        age = (time.time() - dest.stat().st_mtime) / 86400
        if age < PLZ_MAX_AGE_DAYS:
            return f"= plz_dach.csv aktuell ({age:.0f} Tage)"
    rows, seen, errs = [], set(), []
    for cc, CC in GEONAMES.items():
        try:
            with urlopen(Request(GEONAMES_URL.format(cc=CC), headers=_UA), timeout=90) as r:
                raw = r.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                text = z.read(f"{CC}.txt").decode("utf-8")
        except Exception as e:
            errs.append(f"{CC}: {e}"); continue
        for line in text.splitlines():
            f = line.split("\t")
            if len(f) < 11 or not f[1].strip():
                continue
            key = (cc, f[1].strip())
            if key in seen:
                continue
            try:
                lat, lon = float(f[9]), float(f[10])
            except ValueError:
                continue
            seen.add(key)
            rows.append({"country": cc, "plz": f[1].strip(), "place": f[2].strip(),
                         "region_code": f[4].strip(), "region_name": f[3].strip(),
                         "lat": round(lat, 5), "lon": round(lon, 5)})
    if not rows:
        return f"✗ plz_dach.csv: keine GeoNames-Daten ({'; '.join(errs)})"
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["country", "plz", "place",
                           "region_code", "region_name", "lat", "lon"])
        w.writeheader(); w.writerows(rows)
    tmp.replace(dest)
    suffix = f" (⚠ {'; '.join(errs)})" if errs else ""
    return f"✓ plz_dach.csv: {len(rows)} PLZ (GeoNames){suffix}"


def _city_tier(code: str, pop: int) -> int:
    """Zoom-Stufe: 0 = immer sichtbar … 3 = erst weit hineingezoomt."""
    if code == "PPLC" or pop >= 500_000:
        return 0
    if code == "PPLA" or pop >= 200_000:
        return 1
    if pop >= 100_000:
        return 2
    return 3


def _local_names() -> dict:
    """Ortsnamen aus der PLZ-CSV je Land (deutsche/lokale Schreibweise, z. B. „München“)."""
    out: dict = {}
    try:
        with open(Path(DATA_DIR) / "plz_dach.csv", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("place"):
                    out.setdefault(row["country"], set()).add(row["place"].strip())
    except FileNotFoundError:
        pass
    return out


def parse_cities(lines, cc: str, local: set | None = None) -> list[dict]:
    """Filtert Zeilen eines GeoNames-Dumps auf Orte ab CITIES_MIN_POP plus Haupt-/Landeshauptstädte."""
    out = []
    for line in lines:
        f = line.rstrip("\n").split("\t")
        if len(f) < 15 or f[6] != "P" or f[7] not in CITY_CODES:
            continue
        try:
            pop = int(f[14] or 0)
            lat, lon = float(f[4]), float(f[5])
        except ValueError:
            continue
        if pop < CITIES_MIN_POP and f[7] not in ("PPLC", "PPLA"):
            continue
        name = f[1].strip()
        if local:   # GeoNames-„name“ ist oft englisch (Munich, Vienna) -> lokale Schreibweise bevorzugen
            for cand in [name, f[2].strip()] + [a.strip() for a in f[3].split(",")]:
                if cand in local:
                    name = cand
                    break
        out.append({"n": name, "lat": round(lat, 4), "lon": round(lon, 4), "p": pop,
                    "t": _city_tier(f[7], pop), "c": cc})
    return out


def _build_cities(force: bool) -> str:
    """Baut static/map_cities.json aus den GeoNames-Länderdumps (DE/AT/CH/LI)."""
    if not force and CITIES_FILE.exists():
        age = (time.time() - CITIES_FILE.stat().st_mtime) / 86400
        if age < PLZ_MAX_AGE_DAYS:
            return f"= {CITIES_FILE.name} aktuell ({age:.0f} Tage)"
    local = _local_names()
    cities, errs = [], []
    for cc, CC in GEONAMES.items():
        try:
            with urlopen(Request(CITIES_URL.format(cc=CC), headers=_UA), timeout=180) as r:
                raw = r.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as z, z.open(f"{CC}.txt") as fh:
                cities += parse_cities(io.TextIOWrapper(fh, encoding="utf-8"), cc, local.get(cc))
        except Exception as e:
            errs.append(f"{CC}: {e}")
    if not cities:
        return f"✗ {CITIES_FILE.name}: keine GeoNames-Daten ({'; '.join(errs)})"
    # Doppelte (gleicher Name, ~gleicher Ort) entfernen, größte zuerst (für Überlappungen).
    seen, uniq = set(), []
    for c in sorted(cities, key=lambda c: -c["p"]):
        k = (c["n"], round(c["lat"], 1), round(c["lon"], 1))
        if k not in seen:
            seen.add(k); uniq.append(c)
    tmp = CITIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"cities": uniq}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CITIES_FILE)
    suffix = f" (⚠ {'; '.join(errs)})" if errs else ""
    return f"✓ {CITIES_FILE.name}: {len(uniq)} Orte (GeoNames){suffix}"


def refresh(force: bool = False) -> list[str]:
    """Lädt Leaflet + geoBoundaries-GeoJSON und baut die PLZ-CSV aus GeoNames.
    Blockierendes I/O – aus dem Bot via asyncio.to_thread aufrufen. Wirft nicht."""
    out = []
    for url, dest in SOURCES:
        try:
            out.append(_download(url, dest, force))
        except Exception as e:
            out.append(f"✗ {dest.name} ({e})")
    try:
        out.append(_build_plz(force))
    except Exception as e:
        out.append(f"✗ plz_dach.csv ({e})")
    try:
        out.append(_build_cities(force))      # nach der PLZ-CSV (liefert die lokalen Ortsnamen)
    except Exception as e:
        out.append(f"✗ {CITIES_FILE.name} ({e})")
    return out
