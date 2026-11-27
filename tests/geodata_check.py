# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/geodata_check.py – Regions-Zuordnung der Prognose gegen die ECHTEN Umrisse (static/*.geojson).

Die Umrisse liegen nicht im Repo (der Bot lädt sie selbst, utils/map_geodata.py). Diese Prüfung
läuft daher nur, wo sie vorhanden sind: in der Vorabprüfung auf dem Server (tests/deploy_check.py)
und in pytest (tests/test_geodata_real.py, sonst übersprungen). Nur Standardbibliothek.

Bewusst mit Enklaven (Wien in Niederösterreich, Berlin in Brandenburg, Bremen in Niedersachsen,
Appenzell in St. Gallen): dort hing das Ergebnis früher von der Reihenfolge in der Datei ab.
"""
from __future__ import annotations

from pathlib import Path

FILES = ("de_bundeslaender", "at_bundeslaender", "ch_kantone", "li_land")

# Ort → erwartete Region (Land, Kürzel wie in den Umrissen); None = außerhalb DACH
POINTS = {
    "München":    (48.137, 11.575, ("de", "BY")),
    "Berlin":     (52.520, 13.405, ("de", "BE")),
    "Potsdam":    (52.390, 13.060, ("de", "BB")),
    "Hamburg":    (53.550, 9.990, ("de", "HH")),
    "Bremen":     (53.079, 8.802, ("de", "HB")),
    "Hannover":   (52.375, 9.732, ("de", "NI")),
    "Wien":       (48.208, 16.373, ("at", "9")),
    "St. Pölten": (48.204, 15.626, ("at", "3")),
    "Graz":       (47.070, 15.440, ("at", "6")),
    "Zürich":     (47.377, 8.541, ("ch", "ZH")),
    "Appenzell":  (47.331, 9.409, ("ch", "AI")),
    "Herisau":    (47.386, 9.279, ("ch", "AR")),
    "St. Gallen": (47.424, 9.376, ("ch", "SG")),
    "Genf":       (46.200, 6.140, ("ch", "GE")),
    "Vaduz":      (47.140, 9.520, ("li", "LI")),
    "Madrid":     (40.400, -3.700, None),
    "Paris":      (48.857, 2.352, None),
}


def available(static_dir: Path) -> bool:
    return all((Path(static_dir) / f"{f}.geojson").exists() for f in FILES)


def check(static_dir: Path) -> list[str]:
    """Fehlerliste (leer = alles richtig). Erwartet, dass alle Umriss-Dateien vorhanden sind."""
    from utils import forecast as F
    old = F.STATIC
    F.STATIC = Path(static_dir)
    try:
        polys = F._polys()
    finally:
        F.STATIC = old
    if not polys:
        return ["keine Umrisse lesbar"]
    errs = []
    for name, (lat, lon, want) in POINTS.items():
        got = F.region_of(lat, lon, polys)
        if got != want:
            errs.append(f"{name}: {got} statt {want}")
    return errs
