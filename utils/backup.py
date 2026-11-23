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
utils/backup.py – tägliches Backup aller SQLite-Datenbanken.

  • Gesichert werden: Haupt-DB (antcheckbot.db), Board-DB (board.db) und die
    Preis-Historie des Grabbers (price_history.db) – jeweils nur, wenn vorhanden.
  • Methode: SQLite-Backup-API (sqlite3.Connection.backup) – liefert eine konsistente
    Kopie auch während der Bot/Grabber schreibt (WAL-Modus). Jede Kopie wird danach mit
    PRAGMA quick_check geprüft.
  • Ablage: <DATA_DIRECTORY>/backups/JJJJ-MM-TT/ (je Tag ein Ordner). Erst in einen
    Zwischenordner geschrieben und nach erfolgreicher Prüfung umbenannt – ein halbes
    Backup gibt es nie.
  • Aufbewahrung: höchstens BACKUP_KEEP_DAYS Tage (Standard und Maximum: 7).
  • Speicherplatz: Das Backup läuft nur, wenn danach noch mindestens BACKUP_MIN_FREE_GB
    (Standard 2 GB) frei bleiben – vorher geschätzt, nachher noch einmal geprüft.

Ergebnis des letzten Laufs: <backups>/last.json (für die Statusseite / den Health-Check).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import BOARD_DB_FILE, DATA_DIRECTORY, DB_FILE

logger = logging.getLogger(__name__)

BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "true").lower() not in ("0", "false", "no")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(Path(DATA_DIRECTORY) / "backups")))
MAX_KEEP_DAYS = 7
KEEP_DAYS = max(1, min(MAX_KEEP_DAYS, int(os.getenv("BACKUP_KEEP_DAYS", str(MAX_KEEP_DAYS)) or MAX_KEEP_DAYS)))
MIN_FREE_BYTES = int(float(os.getenv("BACKUP_MIN_FREE_GB", "2")) * 1024 ** 3)
LAST_FILE = "last.json"


def targets() -> list[tuple[str, Path]]:
    """(Dateiname im Backup, Quellpfad) aller vorhandenen Datenbanken."""
    cands = [("antcheckbot.db", Path(DB_FILE)), ("board.db", Path(BOARD_DB_FILE)),
             ("price_history.db", Path(DATA_DIRECTORY) / "price_history.db")]
    return [(n, p) for n, p in cands if p.is_file()]


def _db_size(p: Path) -> int:
    """Größe inkl. WAL-Datei – obere Schätzung der Backup-Größe."""
    total = p.stat().st_size
    wal = p.with_name(p.name + "-wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def prune(today: date, backup_dir: Path = BACKUP_DIR, keep_days: int = KEEP_DAYS) -> list[str]:
    """Löscht Tagesordner, die älter als keep_days sind (heute zählt mit), und
    liegengebliebene Zwischenordner. Rückgabe: gelöschte Ordnernamen."""
    removed = []
    if not backup_dir.is_dir():
        return removed
    oldest_kept = today - timedelta(days=keep_days - 1)
    for d in backup_dir.iterdir():
        if not d.is_dir():
            continue
        if d.name.endswith(".tmp"):
            shutil.rmtree(d, ignore_errors=True); removed.append(d.name); continue
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue                                  # fremde Ordner nie anfassen
        if day < oldest_kept:
            shutil.rmtree(d, ignore_errors=True); removed.append(d.name)
    return sorted(removed)


def _write_last(backup_dir: Path, result: dict) -> None:
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        prev = read_last(backup_dir) or {}
        if result.get("status") == "ok":
            result["last_ok"] = result["at"]
        elif prev.get("last_ok"):
            result["last_ok"] = prev["last_ok"]
        (backup_dir / LAST_FILE).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning("💾 Backup-Status konnte nicht gespeichert werden: %s", e)


def read_last(backup_dir: Path = BACKUP_DIR) -> dict | None:
    try:
        return json.loads((backup_dir / LAST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_backup(today: date | None = None, backup_dir: Path = BACKUP_DIR,
               min_free: int = MIN_FREE_BYTES, keep_days: int = KEEP_DAYS) -> dict:
    """Ein Backup-Lauf (blockierend – im Bot per run_in_executor aufrufen).
    status: ok | skipped (zu wenig Platz) | failed | disabled | empty."""
    today = today or datetime.now().date()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    if not BACKUP_ENABLED:
        return {"status": "disabled", "at": at}
    removed = prune(today, backup_dir, keep_days)
    src = targets()
    if not src:
        res = {"status": "empty", "at": at, "reason": "keine Datenbank gefunden", "removed": removed}
        _write_last(backup_dir, res)
        return res
    final = backup_dir / today.isoformat()
    need = sum(_db_size(p) for _, p in src)
    replacing = _dir_size(final) if final.is_dir() else 0      # heutiges Backup wird ersetzt
    free = _free_bytes(backup_dir)
    if free + replacing - need < min_free:
        res = {"status": "skipped", "at": at, "removed": removed,
               "reason": f"zu wenig Speicher: frei {free / 1024**3:.2f} GB, Backup ~{need / 1024**2:.0f} MB, "
                         f"Mindestreserve {min_free / 1024**3:.1f} GB"}
        logger.warning("💾 Backup übersprungen – %s", res["reason"])
        _write_last(backup_dir, res)
        return res
    tmp = backup_dir / (today.isoformat() + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    files = {}
    try:
        for name, path in src:
            source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
            dest = sqlite3.connect(tmp / name)
            try:
                source.backup(dest)
                check = dest.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                dest.close(); source.close()
            if check != "ok":
                raise RuntimeError(f"{name}: Prüfung fehlgeschlagen ({check})")
            files[name] = (tmp / name).stat().st_size
        if _free_bytes(backup_dir) + replacing < min_free:
            raise RuntimeError("nach dem Schreiben wären weniger als die Mindestreserve frei")
        if final.is_dir():
            shutil.rmtree(final)
        tmp.rename(final)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        res = {"status": "failed", "at": at, "reason": str(e)[:300], "removed": removed}
        logger.error("💾 Backup fehlgeschlagen: %s", e)
        _write_last(backup_dir, res)
        return res
    res = {"status": "ok", "at": at, "dir": final.name, "files": files,
           "bytes": sum(files.values()), "free_after": _free_bytes(backup_dir),
           "removed": removed, "seconds": round(time.monotonic() - t0, 1)}
    logger.info("💾 Backup %s: %d Datenbanken, %.1f MB, %.1f GB frei, gelöscht: %s",
                final.name, len(files), res["bytes"] / 1024 ** 2, res["free_after"] / 1024 ** 3,
                ", ".join(removed) or "–")
    _write_last(backup_dir, res)
    return res


def health_tile(now: datetime | None = None, backup_dir: Path = BACKUP_DIR) -> dict:
    """Kachel für die Statusseite („Datenbank-Backup“)."""
    name = "Datenbank-Backup"
    if not BACKUP_ENABLED:
        return dict(name=name, state="off", detail="deaktiviert (BACKUP_ENABLED=false)")
    last = read_last(backup_dir)
    if not last:
        return dict(name=name, state="off", detail="noch kein Backup (läuft täglich 03:30)")
    now = now or datetime.now(timezone.utc)
    ok_at = last.get("last_ok")
    age_h = None
    if ok_at:
        try:
            age_h = (now - datetime.fromisoformat(ok_at)).total_seconds() / 3600
        except ValueError:
            age_h = None
    if last.get("status") == "failed":
        return dict(name=name, state="down", detail=f"fehlgeschlagen: {last.get('reason', '')[:80]}")
    if age_h is None or age_h > 72:
        return dict(name=name, state="down" if age_h is not None else "warn",
                    detail=last.get("reason", "kein erfolgreiches Backup")[:100])
    if last.get("status") == "skipped":
        return dict(name=name, state="warn", detail=last.get("reason", "übersprungen")[:100])
    if age_h > 36:
        return dict(name=name, state="warn", detail=f"letztes Backup vor {age_h:.0f} h")
    mb = (last.get("bytes") or 0) / 1024 ** 2
    return dict(name=name, state="ok",
                detail=f"{last.get('dir', '')} · {len(last.get('files') or {})} DBs · {mb:.1f} MB · "
                       f"max. {KEEP_DAYS} Tage")
