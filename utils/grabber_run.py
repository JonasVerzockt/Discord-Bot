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
utils/grabber_run.py – Grabber aus dem Bot heraus starten + Datenstand lesen.

Der Grabber (grabber.py) läuft normalerweise stündlich per Cron als User ``aam``.
Der Bot liest nur die fertige shops_data.json. Damit Admins nach einer Korrektur
bei AntCheck nicht bis zum nächsten Cron-Lauf warten müssen, startet
``run_grabber()`` denselben Grabber einmalig als Subprozess:

  • gleicher Interpreter wie der Bot (``sys.executable`` → /opt/discord-bot/.venv/bin/python
    unter systemd) und damit dieselbe venv,
  • gleicher User wie der Bot (systemd ``User=aam``) → Dateien gehören weiter ``aam``,
  • Arbeitsverzeichnis = Projektordner (wie ``cd /opt/discord-bot`` im Cron, .env wird gefunden),
  • harter Timeout (``GRABBER_TIMEOUT``, Standard 600 s),
  • Parallel-Schutz doppelt: asyncio-Lock im Bot + flock im Grabber selbst (Exit 75).

``data_status()`` liefert den Zeitstempel ``_meta.fetched_at`` und das Alter der
Daten, damit /wartung-Ausgaben zeigen, wie frisch die Shop-Daten wirklich sind.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config import BASE_DIR, SHOPS_DATA_FILE
from utils.timez import berlin_from_iso

logger = logging.getLogger(__name__)

GRABBER_SCRIPT = BASE_DIR / "grabber.py"
GRABBER_TIMEOUT = int(os.getenv("GRABBER_TIMEOUT", "600"))
EXIT_LOCKED = 75            # muss zu grabber.EXIT_LOCKED passen
STALE_AFTER_HOURS = 2       # Cron läuft stündlich → >2 h alt = Cron-Lauf fehlt

_run_lock = asyncio.Lock()


@dataclass
class GrabberResult:
    status: str             # "ok" | "locked" | "timeout" | "failed"
    duration: float
    returncode: int | None = None
    summary: str = ""       # "✅ Fertig: …"-Zeile des Grabbers (bei Erfolg)
    tail: str = ""          # letzte Ausgabezeilen (bei Fehler)


def _last_lines(text: str, n: int = 6, limit: int = 900) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    out = "\n".join(lines[-n:])
    return out[-limit:]


async def run_grabber(timeout: int = GRABBER_TIMEOUT) -> GrabberResult:
    """Startet grabber.py einmalig und wartet auf das Ende (max. ``timeout`` s)."""
    if _run_lock.locked():
        return GrabberResult(status="locked", duration=0.0)
    async with _run_lock:
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(GRABBER_SCRIPT),
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error("❌ Grabber nach %ss abgebrochen (Timeout)", timeout)
            return GrabberResult(status="timeout", duration=time.monotonic() - t0,
                                 returncode=proc.returncode)
        duration = time.monotonic() - t0
        out = out_b.decode("utf-8", errors="replace")
        rc = proc.returncode

        if rc == EXIT_LOCKED:
            logger.info("⏳ Grabber übersprungen – anderer Lauf (Cron) aktiv")
            return GrabberResult(status="locked", duration=duration, returncode=rc)
        if rc != 0:
            logger.error("❌ Grabber fehlgeschlagen (Exit %s):\n%s", rc, _last_lines(out, 20, 4000))
            return GrabberResult(status="failed", duration=duration, returncode=rc,
                                 tail=_last_lines(out))

        summary = ""
        for line in reversed(out.splitlines()):
            if "Fertig:" in line:
                summary = line.split("Fertig:", 1)[1].split("->", 1)[0].strip()
                break
        logger.info("✅ Grabber manuell ausgeführt (%.1fs): %s", duration, summary)
        return GrabberResult(status="ok", duration=duration, returncode=rc, summary=summary)


def data_status() -> tuple[str | None, float | None]:
    """(fetched_at als Berliner Zeit, Alter in Sekunden) aus shops_data.json.
    (None, None), wenn Datei/Zeitstempel fehlt oder unlesbar ist."""
    try:
        with open(SHOPS_DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        ts = raw.get("_meta", {}).get("fetched_at") if isinstance(raw, dict) else None
        if not ts:
            return None, None
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return berlin_from_iso(ts) or str(ts), max(age, 0.0)
    except Exception as e:
        logger.debug("data_status: %s", e)
        return None, None


def format_data_status(l10n, lang: str) -> str:
    """Eine Zeile „Datenstand AntCheck: … (vor X Min.)“ + ggf. Warnung bei alten Daten."""
    ts, age = data_status()
    if ts is None or age is None:
        return l10n.get("data_status_unknown", lang)
    if age < 3600:
        age_txt = l10n.get("age_minutes", lang, n=int(age // 60))
    else:
        age_txt = l10n.get("age_hours", lang, n=f"{age / 3600:.1f}")
    line = l10n.get("data_status_line", lang, ts=ts, age=age_txt)
    if age > STALE_AFTER_HOURS * 3600:
        line += "\n" + l10n.get("data_status_stale", lang, hours=STALE_AFTER_HOURS)
    return line
