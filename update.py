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

Sicherheitsnetz (seit 5.2.0):
  • Vor dem Neustart läuft eine Vorabprüfung (tests/deploy_check.py: kompilieren,
    alle Cogs laden, Web-Oberfläche + Templates, Sprachdateien) – offline, ohne die
    echten Daten anzufassen.
  • Schlägt die Paket-Installation oder die Vorabprüfung fehl, wird auf den vorherigen
    Commit zurückgesetzt (inkl. der vorherigen Pakete). Der laufende Bot wird NICHT
    neu gestartet und läuft mit dem alten Stand weiter.
  • Der abgelehnte Commit wird gemerkt (data/.update_rejected) und nicht alle 5 Minuten
    erneut versucht – erst ein neuer Commit auf dem Branch löst den nächsten Versuch aus.
  • Optional: DM an den Owner (BOARD_OWNER_ID) über die Discord-API mit dem Bot-Token
    (DISCORD_TOKEN) aus der .env – bei Ablehnung und bei fehlgeschlagenem Neustart.

Konfiguration über Umgebungsvariablen (oder .env im REPO_DIR):
  REPO_DIR          (Default: /opt/discord-bot)
  BRANCH            (Default: main)
  SERVICE           (Default: aam-bot)
  VENV              (Default: <REPO_DIR>/.venv)
  UPDATE_PREFLIGHT  (Default: true)  – Vorabprüfung vor dem Neustart
  UPDATE_NOTIFY_DM  (Default: true)  – DM an den Owner bei Problemen
  RESTART_CMD       (Default: sudo /usr/bin/systemctl restart <SERVICE>)
  DISCORD_API_BASE  (nur für Tests: Ziel der DM statt https://discord.com/api/v10)

Exit-Codes: 0 = nichts zu tun oder Deploy erfolgreich, 1 = Fehler/abgebrochen.

Hinweis: Python liest das komplette Skript beim Start ein – ein Self-Update
(Änderung an update.py) während des Laufs ist daher unkritisch.
"""
import json
import os
import shlex
import subprocess
import sys
import urllib.request

REPO_DIR = os.getenv("REPO_DIR", "/opt/discord-bot")
BRANCH   = os.getenv("BRANCH", "main")
SERVICE  = os.getenv("SERVICE", "aam-bot")
VENV     = os.getenv("VENV", os.path.join(REPO_DIR, ".venv"))
PREFLIGHT = os.getenv("UPDATE_PREFLIGHT", "true").lower() not in ("0", "false", "no")
RESTART_CMD = shlex.split(os.getenv("RESTART_CMD", f"sudo /usr/bin/systemctl restart {SERVICE}"))
REJECTED_FILE = os.path.join(REPO_DIR, "data", ".update_rejected")
DISCORD_API = os.getenv("DISCORD_API_BASE", "https://discord.com/api/v10")


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


# ── Konfiguration aus der .env (der Update-Dienst hat keine EnvironmentFile) ──
def _env_value(key: str) -> str:
    if os.getenv(key):
        return os.environ[key]
    path = os.path.join(REPO_DIR, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip().removeprefix("export ").strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def notify_owner(text: str) -> bool:
    """Optionale DM an den Owner über die Discord-REST-API. Fehler werden nur geloggt –
    eine Benachrichtigung darf den Deploy nie beeinflussen."""
    if os.getenv("UPDATE_NOTIFY_DM", "true").lower() in ("0", "false", "no"):
        return False
    token, owner = _env_value("DISCORD_TOKEN"), _env_value("BOARD_OWNER_ID")
    if not token or not owner or owner == "0":
        print("Hinweis: keine DM (DISCORD_TOKEN oder BOARD_OWNER_ID fehlt).")
        return False
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
               "User-Agent": "DiscordBot (aam-bot-update, 1.0)"}

    def _post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(DISCORD_API + path, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")
    try:
        channel = _post("/users/@me/channels", {"recipient_id": str(owner)})
        _post(f"/channels/{channel['id']}/messages", {"content": text[:1900]})
        return True
    except Exception as e:
        print(f"Hinweis: DM an den Owner fehlgeschlagen: {e}", file=sys.stderr)
        return False


# ── Abgelehnte Commits merken (kein Dauer-Neuversuch alle 5 Minuten) ─────────
def _read_rejected() -> str:
    try:
        with open(REJECTED_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_rejected(sha: str) -> None:
    try:
        os.makedirs(os.path.dirname(REJECTED_FILE), exist_ok=True)
        with open(REJECTED_FILE, "w", encoding="utf-8") as f:
            f.write(sha + "\n")
    except OSError as e:
        print(f"Hinweis: abgelehnter Commit konnte nicht gemerkt werden: {e}", file=sys.stderr)


def _clear_rejected() -> None:
    try:
        os.remove(REJECTED_FILE)
    except OSError:
        pass


def _pip_install() -> None:
    pip = os.path.join(VENV, "bin", "pip")
    run([pip, "install", "--quiet", "-r", "requirements.txt"])


def _preflight() -> tuple[bool, str]:
    """Vorabprüfung mit dem venv-Python des Bots. (ok, Ausgabe)"""
    py = os.path.join(VENV, "bin", "python")
    script = os.path.join(REPO_DIR, "tests", "deploy_check.py")
    if not os.path.exists(script):
        return True, "tests/deploy_check.py fehlt – Vorabprüfung übersprungen."
    try:
        r = subprocess.run([py, script], cwd=REPO_DIR, text=True, capture_output=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"Vorabprüfung nicht ausführbar: {e}"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _rollback(local: str, remote: str, req_changed: bool, reason: str) -> int:
    """Zurück auf den vorherigen Commit (inkl. Pakete), Commit merken, Owner informieren."""
    print(f"ROLLBACK auf {local[:7]}: {reason}", file=sys.stderr)
    run(["git", "reset", "--hard", local], check=False)
    if req_changed:
        try:
            _pip_install()                       # vorherige Paketversionen wiederherstellen
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"WARNUNG: alte Pakete konnten nicht wiederhergestellt werden: {e}", file=sys.stderr)
    _write_rejected(remote)
    subject = ""
    try:
        subject = git_out("log", "-1", "--format=%s", remote)
    except subprocess.CalledProcessError:
        pass
    notify_owner(f"⚠️ **Auto-Deploy abgelehnt** – `{remote[:7]}` {subject}\n"
                 f"Der Bot läuft unverändert weiter mit `{local[:7]}`.\n"
                 f"Grund:\n```\n{reason[-1500:]}\n```\n"
                 f"Ein neuer Commit auf `{BRANCH}` löst den nächsten Versuch aus.")
    return 1


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

    if remote == _read_rejected():
        return 0   # bereits abgelehnt und gemeldet – warten auf einen neuen Commit

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
        try:
            _pip_install()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return _rollback(local, remote, req_changed, f"pip install fehlgeschlagen: {e}")

    if PREFLIGHT:
        print("Vorabprüfung...")
        ok, output = _preflight()
        print(output)
        if not ok:
            return _rollback(local, remote, req_changed, output)

    print(f"Starte {SERVICE} neu...")
    try:
        run(RESTART_CMD)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        notify_owner(f"❌ **Auto-Deploy:** Neustart von `{SERVICE}` mit `{remote[:7]}` fehlgeschlagen: {e}")
        _fail(f"systemctl restart fehlgeschlagen: {e}")

    _clear_rejected()
    print(f"Deploy abgeschlossen: {git_out('rev-parse', '--short', 'HEAD')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
