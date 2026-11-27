# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Regions-Zuordnung gegen die echten Umrisse (static/*.geojson, siehe tests/geodata_check.py).
Übersprungen, wenn die Dateien fehlen (z. B. in GitHub Actions). Andere Quelle per Umgebungsvariable
AAM_GEODATA_DIR (Ordner mit de_bundeslaender.geojson usw.).
"""
import os
from pathlib import Path

import pytest

import geodata_check as G
from utils import forecast as F

DIR = Path(os.environ.get("AAM_GEODATA_DIR") or F.STATIC)


@pytest.mark.skipif(not G.available(DIR), reason=f"Umrisse fehlen in {DIR} (lädt der Bot selbst)")
def test_regions_with_real_outlines():
    assert G.check(DIR) == []
