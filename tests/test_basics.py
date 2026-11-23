# SPDX-License-Identifier: AGPL-3.0-or-later
"""
tests/test_basics.py – schnelle Grundprüfungen ohne Discord-Login und ohne Internet.

  python -m pytest tests/

Die ausführlichen Vorher/Nachher-Vergleiche liegen in board_snapshot.py und
command_tree_snapshot.py (siehe README, Abschnitt „Tests“).
"""
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent


def test_board_translations_complete():
    """Alle Board-Texte gibt es in jeder Sprache (gleiche Schlüssel, nicht leer)."""
    cats = {lang: json.loads((ROOT / "locales/board" / f"{lang}.json").read_text(encoding="utf-8"))
            for lang in ("de", "en", "eo")}
    keys = set(cats["de"])
    for lang, cat in cats.items():
        assert set(cat) == keys, f"{lang}: abweichende Schlüssel {sorted(set(cat) ^ keys)[:10]}"
        empty = [k for k, v in cat.items() if not str(v).strip()]
        assert not empty, f"{lang}: leere Texte {empty[:10]}"


def test_templates_parse():
    """Jedes Template ist syntaktisch gültiges Jinja2 (inkl. extends/import-Ziele)."""
    sys.path.insert(0, str(ROOT))
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(ROOT / "web/templates")))
    names = sorted(p.name for p in (ROOT / "web/templates").glob("*.html"))
    assert names, "keine Templates gefunden"
    for name in names:
        env.parse(env.loader.get_source(env, name)[0])


def test_command_tree(tmp_path):
    """Alle Cogs laden offline; jeder Slash-Befehl ist registriert."""
    out = tmp_path / "tree.json"
    r = subprocess.run([sys.executable, str(TESTS / "command_tree_snapshot.py"), str(out)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["loaded"] == data["total"]
    assert len(data["commands"]) > 30


def test_board_pages(tmp_path):
    """Jede Board-Seite und jede Formular-Aktion antwortet ohne Serverfehler (5xx)."""
    snap = tmp_path / "snap"
    r = subprocess.run([sys.executable, str(TESTS / "board_snapshot.py"), str(snap)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    index = json.loads((snap / "index.json").read_text(encoding="utf-8"))
    errors = [k for k, v in index.items() if v["status"] >= 500]
    assert not errors, f"Serverfehler: {errors[:10]}"
    assert len(index) > 400
