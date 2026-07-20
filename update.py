#!/usr/bin/env python3
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
update.py – Auto-Deploy für den AAM Discord Bot.

Prüft origin/<BRANCH> auf neue Commits, zieht sie per Fast-Forward und startet
den systemd-Dienst neu. Bei geänderter requirements.txt werden die Abhängigkeiten
im venv nachinstalliert. Lokale, uncommittete Änderungen brechen den Deploy ab
(werden nie überschrieben).

Konfiguration über Umgebungsvariablen:
  REPO_DIR   (Default: /opt/discord-bot)
  BRANCH     (Default: main)
  SERVICE    (Default: aam-bot)
  VENV       (Default: <REPO_DIR>/.venv)

Exit-Codes: 0 = nichts zu tun oder Deploy erfolgreich, 1 = Fehler/abgebrochen.

Hinweis: Python liest das komplette Skript beim Start ein – ein Self-Update
(Änderung an update.py) während des Laufs ist daher unkritisch.
"""
import os
import subprocess
import sys

REPO_DIR = os.getenv("REPO_DIR", "/opt/discord-bot")
BRANCH   = os.getenv("BRANCH", "main")
SERVICE  = os.getenv("SERVICE", "aam-bot")
VENV     = os.getenv("VENV", os.path.join(REPO_DIR, ".venv"))


def _fail(msg: str) -> "None":
    """Fehlermeldung nach stderr und mit Code 1 abbrechen (wie 'set -e')."""
    print(f"FEHLER: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list, *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """Führt ein Kommando in REPO_DIR aus. capture=True gibt stdout zurück (getrimmt)."""
    return subprocess.run(
        cmd, cwd=REPO_DIR, check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def git_out(*args: str) -> str:
    """git-Kommando ausführen und stdout (getrimmt) zurückgeben."""
    return run(["git", *args], capture=True).stdout.strip()


def main() -> int:
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        _fail(f"{REPO_DIR} ist kein Git-Repository.")

    try:
        run(["git", "fetch", "origin", BRANCH, "--quiet"])
        local  = git_out("rev-parse", "HEAD")
        remote = git_out("rev-parse", f"origin/{BRANCH}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _fail(f"git fetch/rev-parse fehlgeschlagen: {e}")

    if local == remote:
        return 0   # Nichts zu tun

    print(f"Update gefunden: {local} -> {remote}")

    # Lokale, uncommittete Änderungen niemals überschreiben.
    dirty_worktree = run(["git", "diff", "--quiet"], check=False).returncode != 0
    dirty_index    = run(["git", "diff", "--cached", "--quiet"], check=False).returncode != 0
    if dirty_worktree or dirty_index:
        _fail(f"Lokale, uncommittete Änderungen in {REPO_DIR} – Deploy abgebrochen.")

    # Merken, ob sich requirements.txt ändert (vor dem Merge).
    req_changed = bool(git_out("diff", "--name-only", local, remote, "--", "requirements.txt"))

    try:
        run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _fail(f"git merge --ff-only fehlgeschlagen (kein Fast-Forward möglich?): {e}")

    if req_changed:
        print("requirements.txt geändert – installiere Abhängigkeiten...")
        pip = os.path.join(VENV, "bin", "pip")
        try:
            run([pip, "install", "--quiet", "-r", "requirements.txt"])
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            _fail(f"pip install fehlgeschlagen: {e}")

    print(f"Starte {SERVICE} neu...")
    try:
        run(["sudo", "/usr/bin/systemctl", "restart", SERVICE])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _fail(f"systemctl restart fehlgeschlagen: {e}")

    print(f"Deploy abgeschlossen: {git_out('rev-parse', '--short', 'HEAD')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
