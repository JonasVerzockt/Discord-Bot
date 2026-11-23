# SPDX-License-Identifier: AGPL-3.0-or-later
"""tools/env_sync.py: Aufbau aus .env.example, Werte aus .env, wirksame Konfiguration unverändert."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("env_sync", ROOT / "tools" / "env_sync.py")
ES = importlib.util.module_from_spec(spec); spec.loader.exec_module(ES)

EXAMPLE = """# ── Discord ──
# Bot-Token (Pflicht)
DISCORD_TOKEN=dein-token
# ── Board ──
# BOARD_ENABLED=false
# Schlüssel
# BOARD_HASH_SALT=ein-langes-zufaelliges-salt
PYTHONUNBUFFERED=1
"""
ENV = """BOARD_HASH_SALT="geheim mit # raute"
DISCORD_TOKEN=abc.def
ALTE_VARIABLE=x
export BOARD_ENABLED=true
AI_CHAT_PUBLIC=true   # Kommentar
"""


def test_build_keeps_values_and_structure():
    text, rep = ES.build(EXAMPLE, ENV)
    lines = text.splitlines()
    assert lines[0] == "# ── Discord ──" and "# Bot-Token (Pflicht)" in lines
    assert "DISCORD_TOKEN=abc.def" in lines
    # In der Vorlage auskommentiert („# BOARD_ENABLED=false“), bei mir aktiv → aktiv mit meinem Wert
    assert "BOARD_ENABLED=true" in lines and "# BOARD_ENABLED=false" not in lines
    assert 'BOARD_HASH_SALT="geheim mit # raute"' in lines                # Wert zeichengenau
    assert "# PYTHONUNBUFFERED=1" in lines                                   # nicht gesetzt → bleibt aus
    assert lines.index("DISCORD_TOKEN=abc.def") < lines.index("BOARD_ENABLED=true")
    assert "ALTE_VARIABLE=x" in lines[-3:] and rep["unbekannt"] == ["ALTE_VARIABLE", "AI_CHAT_PUBLIC"]
    assert rep["inline_kommentar_verdacht"] == ["AI_CHAT_PUBLIC"]            # gequoteter Salt ist ok
    assert ES.parse_active(text)[0] == ES.parse_active(ENV)[0]               # wirksam identisch


def test_cli_writes_new_file_without_printing_values(tmp_path, capsys):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    assert ES.main(["--env", str(tmp_path / ".env"), "--example", str(tmp_path / ".env.example")]) == 0
    out = capsys.readouterr().out
    assert "abc.def" not in out and "geheim" not in out                     # keine Werte auf dem Bildschirm
    assert (tmp_path / ".env.new").exists() and (tmp_path / ".env").read_text() == ENV
    assert oct((tmp_path / ".env.new").stat().st_mode)[-3:] == "600"


def test_cli_write_makes_backup(tmp_path):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    (tmp_path / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    assert ES.main(["--env", str(tmp_path / ".env"), "--example", str(tmp_path / ".env.example"), "--write"]) == 0
    backups = list(tmp_path.glob(".env.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == ENV
    assert ES.parse_active((tmp_path / ".env").read_text())[0] == ES.parse_active(ENV)[0]


def test_real_example_roundtrip():
    """Mit der echten .env.example: eine .env aus allen Standardwerten bleibt wirksam gleich."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    env = "\n".join(f"{k}=wert-{i}" for i, k in enumerate(
        sorted({m.group(2) for m in map(ES._TEMPLATE.match, example.splitlines()) if m})))
    text, rep = ES.build(example, env)
    assert ES.parse_active(text)[0] == ES.parse_active(env)[0] and not rep["unbekannt"]
