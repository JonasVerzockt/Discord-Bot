# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/conftest.py – gemeinsame Testumgebung für pytest (läuft VOR allen Test-Modulen).

Alle Daten liegen in einem Temp-Ordner, Google Sheets und Wechselkurse sind offline
ersetzt, die Alt-Daten-Migration ist abgeschaltet (siehe board_snapshot._setup_env).
"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import board_snapshot as BS  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="aam-tests-"))
BS._setup_env(TMP)
(TMP / "shops_data.json").write_text(json.dumps(BS._shops_data()), encoding="utf-8")

import gspread  # noqa: E402
gspread.service_account = lambda *a, **k: BS._OfflineSheets()

import utils.currency as _cur  # noqa: E402
_cur._RATES = {"EUR": 1.0, "CHF": 1.05, "USD": 0.92, "GBP": 1.17, "CAD": 0.68}
_cur._LAST_FETCH = datetime.now(timezone.utc)
_cur._fetch_rates_sync = lambda: None


def pytest_configure(config):
    # Veraltete naive UTC-Aufrufe (Python 3.12+) nicht wieder einschleppen: utils.timez.utc_naive nutzen
    for fn in ("utcnow", "utcfromtimestamp"):
        config.addinivalue_line("filterwarnings", rf"error:datetime\.datetime\.{fn}\(\) is deprecated:DeprecationWarning")
