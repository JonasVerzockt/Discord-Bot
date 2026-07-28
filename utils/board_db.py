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
utils/board_db.py – EIGENE, separate SQLite-DB für das Feedback-Board.

Bewusst getrennt von der Haupt-Bot-DB (`config.DB_FILE`): eigene Datei
`config.BOARD_DB_FILE`, eigene Verbindungen. Alle Zugriffe laufen über
`run_in_executor`, weil der Webserver denselben asyncio-Loop wie der Bot nutzt –
blockierendes SQLite im Handler würde sonst den Bot einfrieren.
"""
import asyncio
import logging
import sqlite3
import threading

from config import BOARD_DB_FILE

logger = logging.getLogger(__name__)

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_submissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT    NOT NULL DEFAULT 'idea',
    title          TEXT    NOT NULL,
    body           TEXT    DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'pending',
    component      TEXT    DEFAULT '',
    priority       TEXT    DEFAULT '',
    submitter_hash TEXT,
    submitter_name TEXT    DEFAULT '',
    version        TEXT    DEFAULT '',
    source         TEXT    DEFAULT 'public',
    created_at     TEXT    DEFAULT (datetime('now')),
    approved_at    TEXT,
    updated_at     TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS board_votes (
    submission_id  INTEGER NOT NULL,
    voter_hash     TEXT    NOT NULL,
    created_at     TEXT    DEFAULT (datetime('now')),
    PRIMARY KEY (submission_id, voter_hash)
);
CREATE TABLE IF NOT EXISTS board_comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id  INTEGER NOT NULL,
    author         TEXT    DEFAULT 'Owner',
    body           TEXT    NOT NULL,
    created_at     TEXT    DEFAULT (datetime('now'))
);
-- Vorfall-Historie der Status-Kacheln: pro Check eine Zeile je 'nicht OK'-Phase.
-- ended_at NULL = noch offen; wird bei Rückkehr auf OK automatisch gesetzt.
CREATE TABLE IF NOT EXISTS board_incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    check_key   TEXT    NOT NULL,
    state       TEXT    NOT NULL,        -- 'warn' | 'down'
    detail      TEXT    DEFAULT '',
    admin_note  TEXT    DEFAULT '',
    started_at  TEXT    DEFAULT (datetime('now')),
    ended_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_board_status ON board_submissions(status);
CREATE INDEX IF NOT EXISTS idx_board_comments_sub ON board_comments(submission_id);
CREATE INDEX IF NOT EXISTS idx_board_incidents_key ON board_incidents(check_key, id DESC);
"""


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(BOARD_DB_FILE, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        _local.conn = c
    return c


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def board_init() -> None:
    def _s():
        c = _conn()
        c.executescript(_SCHEMA)
        c.commit()
    await _run(_s)
    logger.info("🗂️ Board-DB initialisiert: %s", BOARD_DB_FILE)


async def board_query(query: str, params: tuple = ()) -> list:
    def _s():
        return _conn().execute(query, params).fetchall()
    return await _run(_s)


async def board_one(query: str, params: tuple = ()):
    def _s():
        return _conn().execute(query, params).fetchone()
    return await _run(_s)


async def board_exec(query: str, params: tuple = ()) -> int:
    def _s():
        c = _conn()
        cur = c.execute(query, params)
        c.commit()
        return cur.lastrowid if query.strip().lower().startswith("insert") else cur.rowcount
    return await _run(_s)


async def board_execmany(query: str, seq) -> int:
    rows = list(seq)
    if not rows:
        return 0
    def _s():
        c = _conn()
        cur = c.executemany(query, rows)
        c.commit()
        return cur.rowcount
    return await _run(_s)
