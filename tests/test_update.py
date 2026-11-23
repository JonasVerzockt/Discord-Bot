# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Auto-Deploy (update.py) mit echten Git-Repositories in einem Temp-Ordner.

Neustart, pip und die Vorabprüfung sind durch kleine Attrappen ersetzt; die DM geht an
einen lokalen Fake-Discord-Server. Geprüft werden alle Pfade: kein Update, gutes Update,
kaputtes Update (Rollback + gemerkter Commit + genau EINE DM), Reparatur, kaputte Pakete,
lokale Änderungen.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

UPDATE_PY = Path(__file__).resolve().parent.parent / "update.py"
GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class _FakeDiscord(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        _FakeDiscord.calls.append((self.path, body, self.headers.get("Authorization")))
        out = json.dumps({"id": "555"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture()
def env(tmp_path):
    origin, work, deploy = tmp_path / "origin.git", tmp_path / "work", tmp_path / "deploy"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "tests").mkdir()
    # Attrappe der Vorabprüfung: schlägt fehl, solange eine Datei BROKEN im Repo liegt.
    (work / "tests" / "deploy_check.py").write_text(
        "import os, sys\nok = not os.path.exists('BROKEN')\n"
        "print('Vorabprüfung bestanden.' if ok else 'Cogs laden fehlgeschlagen: kaputt')\n"
        "sys.exit(0 if ok else 1)\n", encoding="utf-8")
    (work / "requirements.txt").write_text("paket==1.0\n", encoding="utf-8")
    (work / ".gitignore").write_text("data/\n.env\n", encoding="utf-8")
    _git(work, "add", "-A"); _git(work, "commit", "-qm", "start"); _git(work, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(deploy))
    (deploy / ".env").write_text('DISCORD_TOKEN="test-token"\nBOARD_OWNER_ID=4242\n', encoding="utf-8")
    venv = tmp_path / "venv" / "bin"; venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    pip_log = tmp_path / "pip.log"
    (venv / "pip").write_text(
        f"#!/bin/sh\necho \"$(cat requirements.txt)\" >> {pip_log}\n"
        "grep -q kaputt requirements.txt && exit 1\nexit 0\n", encoding="utf-8")
    (venv / "pip").chmod(0o755)
    restarted = tmp_path / "restarted"
    _FakeDiscord.calls = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeDiscord)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    e = {**os.environ, "REPO_DIR": str(deploy), "BRANCH": "main", "VENV": str(venv.parent),
         "RESTART_CMD": f"touch {restarted}", "DISCORD_API_BASE": f"http://127.0.0.1:{srv.server_port}",
         "UPDATE_NOTIFY_DM": "true"}
    e.pop("DISCORD_TOKEN", None); e.pop("BOARD_OWNER_ID", None)

    def update():
        if restarted.exists():
            restarted.unlink()
        r = subprocess.run([sys.executable, str(UPDATE_PY)], env=e, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr, restarted.exists()

    def commit(msg, files=(), remove=()):
        for name, text in dict(files).items():
            (work / name).write_text(text, encoding="utf-8")
        for name in remove:
            (work / name).unlink()
        _git(work, "add", "-A"); _git(work, "commit", "-qm", msg); _git(work, "push", "-q", "origin", "main")
        return _git(work, "rev-parse", "HEAD")

    yield dict(update=update, commit=commit, deploy=deploy, head=lambda: _git(deploy, "rev-parse", "HEAD"),
               dm=_FakeDiscord.calls, pip_log=pip_log, rejected=deploy / "data" / ".update_rejected")
    srv.shutdown()


def test_no_update_does_nothing(env):
    code, out, restarted = env["update"]()
    assert code == 0 and not restarted


def test_good_update_restarts(env):
    sha = env["commit"]("gut", {"a.txt": "1"})
    code, out, restarted = env["update"]()
    assert code == 0 and restarted and env["head"]() == sha
    assert env["dm"] == []


def test_broken_update_rolls_back_and_notifies_once(env):
    old = env["head"]()
    bad = env["commit"]("kaputt", {"BROKEN": "x"})
    code, out, restarted = env["update"]()
    assert code == 1 and not restarted
    assert env["head"]() == old                                  # zurückgesetzt
    assert not (env["deploy"] / "BROKEN").exists()
    assert env["rejected"].read_text().strip() == bad            # gemerkt
    paths = [c[0] for c in env["dm"]]
    assert paths == ["/users/@me/channels", "/channels/555/messages"]
    assert env["dm"][0][1] == {"recipient_id": "4242"} and env["dm"][0][2] == "Bot test-token"
    assert "abgelehnt" in env["dm"][1][1]["content"] and bad[:7] in env["dm"][1][1]["content"]
    # zweiter Lauf: kein neuer Versuch, keine zweite DM
    code, out, restarted = env["update"]()
    assert code == 0 and not restarted and env["head"]() == old and len(env["dm"]) == 2
    # Reparatur-Commit wird wieder deployed, Merker verschwindet
    fixed = env["commit"]("repariert", remove=["BROKEN"])
    code, out, restarted = env["update"]()
    assert code == 0 and restarted and env["head"]() == fixed and not env["rejected"].exists()


def test_failed_pip_restores_old_packages(env):
    old = env["head"]()
    env["commit"]("neue pakete", {"requirements.txt": "paket==kaputt\n"})
    code, out, restarted = env["update"]()
    assert code == 1 and not restarted and env["head"]() == old
    log = env["pip_log"].read_text().splitlines()
    assert log == ["paket==kaputt", "paket==1.0"]               # neu versucht, dann alt wiederhergestellt


def test_dm_can_be_disabled(env):
    env["commit"]("kaputt", {"BROKEN": "x"})
    e = dict(os.environ, REPO_DIR=str(env["deploy"]), BRANCH="main", RESTART_CMD="true",
             UPDATE_NOTIFY_DM="false", VENV=str(env["pip_log"].parent / "venv"))
    r = subprocess.run([sys.executable, str(UPDATE_PY)], env=e, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and env["dm"] == []


def test_local_changes_block_deploy(env):
    old = env["head"]()
    env["commit"]("gut", {"a.txt": "1"})
    (env["deploy"] / "requirements.txt").write_text("lokal geändert\n", encoding="utf-8")
    code, out, restarted = env["update"]()
    assert code == 1 and not restarted and env["head"]() == old
    assert "uncommittete" in out
