# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/deploy_check.py – Vorabprüfung vor jedem Neustart durch den Auto-Deploy (update.py).

Läuft auf dem SERVER und nutzt deshalb nur Laufzeit-Pakete aus requirements.txt
(kein pytest/pyflakes). Arbeitet komplett offline in einem Temp-Ordner und fasst die
echten Daten nicht an (Alt-Daten-Migration abgeschaltet, eigene Test-DBs).

Geprüft wird:
  1. Alle .py-Dateien lassen sich kompilieren (keine Syntaxfehler).
  2. Alle Cogs aus main.py laden ohne Fehler, Slash-Befehle sind registriert.
  3. Die Web-Oberfläche baut ihre Routen; alle Templates sind gültiges Jinja2.
  4. Sprachdateien (locales/*.json, locales/board/*.json) sind gültiges JSON.

Exit-Code 0 = alles in Ordnung, 1 = Fehler (Details auf stdout).
"""
from __future__ import annotations

import compileall
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent


def _check_compile() -> list[str]:
    errs = []
    for d in ("cogs", "utils", "web", "tools"):
        if (ROOT / d).is_dir() and not compileall.compile_dir(str(ROOT / d), quiet=1, force=False):
            errs.append(f"Syntaxfehler in {d}/")
    for f in ("main.py", "config.py", "grabber.py", "update.py"):
        if (ROOT / f).exists() and not compileall.compile_file(str(ROOT / f), quiet=1):
            errs.append(f"Syntaxfehler in {f}")
    return errs


def _check_json() -> list[str]:
    errs = []
    for f in list((ROOT / "locales").glob("*.json")) + list((ROOT / "locales" / "board").glob("*.json")):
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            errs.append(f"{f.relative_to(ROOT)}: kein gültiges JSON ({e})")
    return errs


def _check_cogs() -> list[str]:
    """Lädt alle Cogs in einem eigenen Prozess (sauberer Zustand, Timeout)."""
    out = Path(tempfile.mkdtemp(prefix="deploy-check-")) / "tree.json"
    r = subprocess.run([sys.executable, str(TESTS / "command_tree_snapshot.py"), str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=240)
    if r.returncode != 0 or not out.exists():
        tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
        return ["Cogs laden fehlgeschlagen:\n    " + "\n    ".join(tail)]
    data = json.loads(out.read_text(encoding="utf-8"))
    if data["loaded"] != data["total"] or not data["commands"]:
        return [f"Cogs: {data['loaded']}/{data['total']} geladen, Fehler: {data['failed'][:3]}"]
    return []


def _check_web() -> list[str]:
    code = (
        "import sys, tempfile; from pathlib import Path\n"
        f"sys.path.insert(0, {str(TESTS)!r}); import board_snapshot as BS\n"
        "BS._setup_env(Path(tempfile.mkdtemp()))\n"
        "import gspread; gspread.service_account = lambda *a, **k: BS._OfflineSheets()\n"
        "from web import build_app\n"
        "from web.core.render import ENV\n"
        "app = build_app(object())\n"
        "n = len(list(app.router.routes()))\n"
        "names = [p.stem for p in (BS.ROOT / 'web/templates').glob('*.html')]\n"
        "for t in names: ENV.get_template(t)\n"
        "print(f'{n} Routen, {len(names)} Templates')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
        return ["Web-Oberfläche fehlerhaft:\n    " + "\n    ".join(tail)]
    m = re.search(r"(\d+) Routen", r.stdout)
    if not m or int(m.group(1)) < 10:
        return [f"Web-Oberfläche: zu wenige Routen ({r.stdout.strip()})"]
    return []


def main() -> int:
    checks = [("Kompilieren", _check_compile), ("Sprachdateien", _check_json),
              ("Cogs laden", _check_cogs), ("Web-Oberfläche", _check_web)]
    failed = []
    for name, fn in checks:
        try:
            errs = fn()
        except Exception as e:                      # Prüfung selbst darf nie „grün“ durchrutschen
            errs = [f"{name}: Prüfung abgebrochen ({type(e).__name__}: {e})"]
        print(("✅ " if not errs else "❌ ") + name)
        for e in errs:
            print("   " + e)
        failed += errs
    print("Vorabprüfung bestanden." if not failed else f"Vorabprüfung FEHLGESCHLAGEN ({len(failed)} Fehler).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
