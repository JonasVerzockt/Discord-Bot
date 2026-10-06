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

Aufruf aus dem Bot: `refresh()` (blockierendes I/O -> im Thread ausführen, s. cogs/map_tasks).
Attribution steht im Footer der Kartenseite. Ohne diese Dateien läuft der Bot mit dem
groben PLZ-Leitziffer-Fallback aus utils/geo.py weiter.
"""
import csv
import io
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
    return out
