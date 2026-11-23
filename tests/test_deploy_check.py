# SPDX-License-Identifier: AGPL-3.0-or-later
"""Die echte Vorabprüfung (tests/deploy_check.py) erkennt kaputte Stände – geprüft an einer
Kopie des Projekts, in die gezielt Fehler eingebaut werden."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(".git", ".venv", "data", "__pycache__", ".pytest_cache", "*.geojson")


@pytest.fixture(scope="module")
def clean_copy(tmp_path_factory):
    dst = tmp_path_factory.mktemp("projekt") / "bot"
    shutil.copytree(ROOT, dst, ignore=IGNORE)
    return dst


def _check(root: Path):
    r = subprocess.run([sys.executable, str(root / "tests" / "deploy_check.py")], cwd=root,
                       capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_clean_project_passes(clean_copy):
    code, out = _check(clean_copy)
    assert code == 0, out


@pytest.mark.parametrize("name, path, change, needle", [
    ("Syntaxfehler", "utils/text_chunks.py", lambda s: s + "\ndef kaputt(:\n", "Kompilieren"),
    ("kaputter Import im Cog", "cogs/offers.py",
     lambda s: s.replace("import discord\n", "import discord\nimport gibt_es_nicht\n", 1), "Cogs laden"),
    ("kaputtes Template", "web/templates/shops.html", lambda s: s + "{% if %}", "Web-Oberfläche"),
    ("kaputtes JSON", "locales/board/en.json", lambda s: s[:-5], "Sprachdateien"),
])
def test_broken_project_fails(tmp_path, clean_copy, name, path, change, needle):
    dst = tmp_path / "bot"
    shutil.copytree(clean_copy, dst)
    f = dst / path
    f.write_text(change(f.read_text(encoding="utf-8")), encoding="utf-8")
    code, out = _check(dst)
    assert code == 1, f"{name} nicht erkannt:\n{out}"
    assert f"❌ {needle}" in out, out
