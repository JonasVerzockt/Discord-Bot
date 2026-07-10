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
utils/db.py - SQLite-Datenbankhelfer für den AAM Discord Bot.

Enthält:
  * execute_db()  - async Wrapper mit ThreadPoolExecutor
  * init_db()     - legt alle Tabellen an (idempotent)

Schema:
  server_settings, user_settings, shops, notifications,
  user_shop_blacklist, shop_name_mappings, server_user_mappings,
  user_seen_products, global_stats, server_info, eu_countries,
  review_tracking, review_pending, ch_delivery_shops, user_price_tracking,
  user_species_watch, user_species_watch_seen, ai_chat_budget, ai_chat_history,
  discount_scanned, discount_codes
"""
import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor
from config import DB_FILE

_executor = ThreadPoolExecutor(max_workers=5)
logger    = logging.getLogger(__name__)


async def execute_db(bot, query: str, params: tuple = (), *, commit: bool = False, fetch: bool = False):
    """
    Fuehrt eine SQLite-Query in einem Thread-Pool aus (non-blocking).

    Args:
        bot:    discord.Bot-Instanz (für bot.loop)
        query:  SQL-Query
        params: Query-Parameter
        commit: True -> schreibende Operation
        fetch:  True -> gibt fetchall() zurück

    Returns:
        list[sqlite3.Row] wenn fetch=True, sonst rowcount (int)
    """
    def _sync():
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Wartet bis zu 5s wenn die DB gerade gesperrt ist (verhindert
        # "database is locked" bei parallelen Schreibzugriffen / WAL).
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            if commit:
                conn.commit()
            if fetch:
                return cur.fetchall()
            return cur.rowcount
        except Exception as e:
            logger.error(f"❌ DB error | query={query!r} params={params} | {e}")
            raise
        finally:
            conn.close()

    return await bot.loop.run_in_executor(_executor, _sync)


# ── Schema ────────────────────────────────────────────────────────────────────
_SCHEMA = """
-- AntCheckBot-Tabellen
CREATE TABLE IF NOT EXISTS server_settings (
    server_id  INTEGER PRIMARY KEY,
    channel_id INTEGER,
    language   TEXT    DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id  INTEGER PRIMARY KEY,
    language TEXT    DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS shops (
    id             INTEGER PRIMARY KEY,
    name           TEXT,
    country        TEXT,
    url            TEXT,
    average_rating REAL,
    url_override   TEXT   DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                TEXT    NOT NULL,
    species                TEXT    NOT NULL,
    regions                TEXT    NOT NULL,
    status                 TEXT    DEFAULT 'active',
    excluded_species       TEXT,
    server_id              INTEGER,
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notified_at            TIMESTAMP,
    pending_feedback_until TIMESTAMP,
    UNIQUE(user_id, species, regions)
);

CREATE TABLE IF NOT EXISTS user_shop_blacklist (
    user_id TEXT NOT NULL,
    shop_id TEXT NOT NULL,
    PRIMARY KEY (user_id, shop_id)
);

CREATE TABLE IF NOT EXISTS shop_name_mappings (
    external_name TEXT    PRIMARY KEY,
    shop_id       INTEGER,
    FOREIGN KEY (shop_id) REFERENCES shops(id)
);

CREATE TABLE IF NOT EXISTS server_user_mappings (
    user_id   TEXT    NOT NULL,
    server_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, server_id)
);

CREATE TABLE IF NOT EXISTS user_seen_products (
    user_id    TEXT NOT NULL,
    product_id TEXT NOT NULL,
    PRIMARY KEY (user_id, product_id)
);

CREATE TABLE IF NOT EXISTS global_stats (
    key   TEXT    PRIMARY KEY,
    value INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS server_info (
    server_id    INTEGER PRIMARY KEY,
    server_name  TEXT,
    member_count INTEGER,
    created_at   TEXT,
    icon_url     TEXT,
    splash_url   TEXT,
    banner_url   TEXT,
    description  TEXT
);

CREATE TABLE IF NOT EXISTS eu_countries (
    code TEXT PRIMARY KEY
);

-- Review-Bot-Tabellen
CREATE TABLE IF NOT EXISTS review_tracking (
    message_id TEXT    PRIMARY KEY,
    sheet_row  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS review_pending (
    message_id TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    identifier TEXT DEFAULT ''
);

-- CH-Lieferliste (manuell hinzugefügte Shops)
CREATE TABLE IF NOT EXISTS ch_delivery_shops (
    shop_id   TEXT PRIMARY KEY,
    added_by  TEXT,
    added_at  TEXT DEFAULT (datetime('now'))
);

-- Preis-Tracking: User -> beobachtete Produkte
CREATE TABLE IF NOT EXISTS user_price_tracking (
    user_id           TEXT    NOT NULL,
    product_id        INTEGER NOT NULL,
    species           TEXT    NOT NULL DEFAULT '',
    product_title     TEXT    NOT NULL DEFAULT '',
    product_url       TEXT    NOT NULL DEFAULT '',
    shop_name         TEXT    NOT NULL DEFAULT '',
    shop_id           TEXT    NOT NULL DEFAULT '',
    currency_iso      TEXT    NOT NULL DEFAULT 'EUR',
    last_notified_min REAL,
    last_notified_max REAL,
    target_price      REAL,
    target_mode       TEXT,
    added_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, product_id)
);

-- Arten-Beobachtung (alle Shops) – vormals in price_tracking.py erzeugt
CREATE TABLE IF NOT EXISTS user_species_watch (
    user_id    TEXT    NOT NULL,
    species    TEXT    NOT NULL,
    is_genus   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, species)
);

CREATE TABLE IF NOT EXISTS user_species_watch_seen (
    user_id         TEXT    NOT NULL,
    watched_species TEXT    NOT NULL,
    product_id      INTEGER NOT NULL,
    last_min        REAL,
    last_max        REAL,
    currency        TEXT,
    PRIMARY KEY (user_id, watched_species, product_id)
);

-- KI-Chat: Tagesbudget (user_id = 0 -> global) – vormals in utils/ai_chat.py
CREATE TABLE IF NOT EXISTS ai_chat_budget (
    date     TEXT    NOT NULL,
    user_id  INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL    NOT NULL DEFAULT 0.0,
    PRIMARY KEY (date, user_id)
);

-- KI-Chat: Konversations-Historie (Key: Discord-Message-ID der Bot-Antwort)
CREATE TABLE IF NOT EXISTS ai_chat_history (
    message_id   INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    history_json TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_history_expires
    ON ai_chat_history (expires_at);

-- Rabattcode-Tracker: bereits an Haiku geschickte Nachrichten (nur einmal parsen)
CREATE TABLE IF NOT EXISTS discount_scanned (
    message_id TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Rabattcode-Tracker: extrahierte Codes (mehrere pro Nachricht möglich)
-- status_override: NULL = automatisch (Datumslogik), 'valid' = manuell gültig,
--                  'invalid' = manuell deaktiviert
CREATE TABLE IF NOT EXISTS discount_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT    NOT NULL,
    shop            TEXT    NOT NULL DEFAULT '',
    shop_url        TEXT    NOT NULL DEFAULT '',
    code            TEXT    NOT NULL,
    discount        TEXT    NOT NULL DEFAULT '',
    valid_from      TEXT,
    valid_until     TEXT,
    is_permanent    INTEGER NOT NULL DEFAULT 0,
    min_order       TEXT,
    message_date    TEXT,
    author          TEXT,
    status_override TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(message_id, code, shop)
);

CREATE INDEX IF NOT EXISTS idx_discount_until
    ON discount_codes (valid_until);

-- Wochen-Digest: Opt-in-Abonnenten (nur diese bekommen die DM)
CREATE TABLE IF NOT EXISTS digest_subscribers (
    user_id       TEXT    PRIMARY KEY,
    subscribed_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Baseline für "neue Arten" im Wochen-Digest (Diff gegen aktuelles shops_data)
CREATE TABLE IF NOT EXISTS known_species (
    species    TEXT    PRIMARY KEY,
    first_seen TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Baseline für "neue Shops" im Wochen-Digest
CREATE TABLE IF NOT EXISTS known_shops (
    shop_id    TEXT    PRIMARY KEY,
    name       TEXT,
    first_seen TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Erfolge (Achievements): freigeschaltete Erfolge pro User
CREATE TABLE IF NOT EXISTS achievements (
    user_id        TEXT    NOT NULL,
    achievement_id TEXT    NOT NULL,
    unlocked_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, achievement_id)
);

-- Leichtes Event-Log für Aktions-/Versteckt-Erfolge (z.B. Befehlsnutzung, Zielpreis-Treffer)
CREATE TABLE IF NOT EXISTS user_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT    NOT NULL,
    event   TEXT    NOT NULL,
    ts      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_events_user ON user_events (user_id);
"""

# Standard EU-Länder (falls DB noch leer)
_EU_COUNTRIES = [
    "at","be","bg","cy","cz","de","dk","ee","es","fi",
    "fr","gr","hr","hu","ie","it","lt","lu","lv","mt",
    "nl","pl","pt","ro","se","si","sk",
]


# Spalten-Migrationen für bestehende DBs: (Tabelle, Spalte, DDL)
_MIGRATIONS = [
    ("shops",          "url_override",    "ALTER TABLE shops ADD COLUMN url_override TEXT DEFAULT NULL"),
    ("discount_codes", "status_override", "ALTER TABLE discount_codes ADD COLUMN status_override TEXT"),
    ("user_price_tracking", "target_price", "ALTER TABLE user_price_tracking ADD COLUMN target_price REAL"),
    ("user_price_tracking", "target_mode",  "ALTER TABLE user_price_tracking ADD COLUMN target_mode TEXT"),
]


async def init_db(bot) -> None:
    """Legt alle Tabellen an (idempotent). Befüllt EU-Länderliste wenn leer."""
    def _sync():
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        try:
            # WAL: bessere Nebenläufigkeit (Leser blockieren Schreiber nicht).
            # Persistent – muss nur einmal gesetzt werden.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            # Spalten-Migrationen für bestehende DBs (idempotent)
            for _table, _col, _ddl in _MIGRATIONS:
                try:
                    conn.execute(_ddl)
                    conn.commit()
                    logger.info(f"🔄 DB-Migration: {_table}.{_col} Spalte hinzugefügt")
                except Exception:
                    pass  # Spalte bereits vorhanden
            # EU-Länder nur einmalig befüllen
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM eu_countries")
            if cur.fetchone()[0] == 0:
                conn.executemany(
                    "INSERT OR IGNORE INTO eu_countries (code) VALUES (?)",
                    [(c,) for c in _EU_COUNTRIES],
                )
                conn.commit()
        finally:
            conn.close()

    await bot.loop.run_in_executor(_executor, _sync)
    logger.info(f"✅ DB initialisiert: {DB_FILE}")
