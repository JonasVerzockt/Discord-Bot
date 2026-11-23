# SPDX-License-Identifier: AGPL-3.0-or-later
"""Datenbank-Backup (utils/backup.py): konsistente Kopien, max. 7 Tage, ≥ 2 GB Reserve."""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

TODAY = date(2026, 11, 23)


@pytest.fixture()
def bk(tmp_path, monkeypatch):
    import utils.backup as B
    data = tmp_path / "data"; data.mkdir()
    for name in ("antcheckbot.db", "board.db", "price_history.db"):
        con = sqlite3.connect(data / name)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [(f"{name}-{i}",) for i in range(100)])
        con.commit(); con.close()
    monkeypatch.setattr(B, "DB_FILE", data / "antcheckbot.db")
    monkeypatch.setattr(B, "BOARD_DB_FILE", str(data / "board.db"))
    monkeypatch.setattr(B, "DATA_DIRECTORY", str(data))
    monkeypatch.setattr(B, "BACKUP_ENABLED", True)
    backups = data / "backups"
    return B, data, backups


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        con.close()


def test_backup_all_databases(bk):
    B, data, backups = bk
    res = B.run_backup(TODAY, backups, min_free=0)
    assert res["status"] == "ok"
    day = backups / "2026-11-23"
    assert sorted(p.name for p in day.iterdir()) == ["antcheckbot.db", "board.db", "price_history.db"]
    assert all(_rows(day / n) == 100 for n in res["files"])
    assert not list(backups.glob("*.tmp"))
    assert json.loads((backups / "last.json").read_text())["status"] == "ok"


def test_backup_is_consistent_with_wal_and_open_transaction(bk):
    B, data, backups = bk
    writer = sqlite3.connect(data / "board.db")
    writer.execute("PRAGMA wal_autocheckpoint=0")             # Daten bleiben in der -wal-Datei
    writer.execute("INSERT INTO t VALUES ('nur-im-wal')"); writer.commit()
    writer.execute("BEGIN"); writer.execute("INSERT INTO t VALUES ('nicht-committet')")
    try:
        assert B.run_backup(TODAY, backups, min_free=0)["status"] == "ok"
    finally:
        writer.rollback(); writer.close()
    copy = sqlite3.connect(backups / "2026-11-23" / "board.db")
    vals = {r[0] for r in copy.execute("SELECT v FROM t")}
    copy.close()
    assert "nur-im-wal" in vals and "nicht-committet" not in vals


def test_keeps_at_most_seven_days(bk):
    B, data, backups = bk
    for i in range(1, 11):
        (backups / (TODAY - timedelta(days=i)).isoformat()).mkdir(parents=True)
    (backups / "notizen").mkdir()                              # fremder Ordner bleibt
    (backups / "2026-11-20.tmp").mkdir()                       # liegengebliebener Zwischenordner
    B.run_backup(TODAY, backups, min_free=0)
    days = sorted(d.name for d in backups.iterdir() if d.is_dir() and d.name[:4].isdigit())
    assert len(days) == 7 and days[0] == "2026-11-17" and days[-1] == "2026-11-23"
    assert (backups / "notizen").is_dir() and not (backups / "2026-11-20.tmp").exists()


def test_skipped_when_less_than_reserve_would_remain(bk, monkeypatch):
    B, data, backups = bk
    need = sum(B._db_size(p) for _, p in B.targets())
    reserve = 2 * 1024 ** 3
    monkeypatch.setattr(B, "_free_bytes", lambda p: reserve + need - 1)   # 1 Byte zu wenig
    res = B.run_backup(TODAY, backups, min_free=reserve)
    assert res["status"] == "skipped" and "zu wenig Speicher" in res["reason"]
    assert not (backups / "2026-11-23").exists()
    assert B.health_tile(backup_dir=backups)["state"] in ("warn", "down")
    monkeypatch.setattr(B, "_free_bytes", lambda p: reserve + need + 10)  # knapp genug
    assert B.run_backup(TODAY, backups, min_free=reserve)["status"] == "ok"


def test_same_day_replaces_and_failure_keeps_old(bk):
    B, data, backups = bk
    assert B.run_backup(TODAY, backups, min_free=0)["status"] == "ok"
    assert B.run_backup(TODAY, backups, min_free=0)["status"] == "ok"   # ersetzt, kein Fehler
    (data / "board.db").write_bytes(b"keine datenbank")                 # Quelle kaputt
    for suffix in ("-wal", "-shm"):
        (data / ("board.db" + suffix)).unlink(missing_ok=True)
    res = B.run_backup(TODAY + timedelta(days=1), backups, min_free=0)
    assert res["status"] == "failed"
    assert (backups / "2026-11-23" / "board.db").exists()              # altes Backup unangetastet
    assert not (backups / "2026-11-24").exists() and not list(backups.glob("*.tmp"))
    assert B.health_tile(backup_dir=backups)["state"] == "down"


def test_health_tile_states(bk, monkeypatch):
    B, data, backups = bk
    assert B.health_tile(backup_dir=backups)["state"] == "off"          # noch nie gelaufen
    B.run_backup(TODAY, backups, min_free=0)
    now = datetime.now(timezone.utc)
    assert B.health_tile(now, backups)["state"] == "ok"
    assert B.health_tile(now + timedelta(hours=40), backups)["state"] == "warn"
    assert B.health_tile(now + timedelta(hours=80), backups)["state"] == "down"
    monkeypatch.setattr(B, "BACKUP_ENABLED", False)
    assert B.health_tile(now, backups)["state"] == "off"
    assert B.run_backup(TODAY, backups)["status"] == "disabled"
