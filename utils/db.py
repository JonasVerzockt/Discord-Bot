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
  user_species_watch, user_species_watch_seen, user_species_watch_variant_seen,
  pending_variant_removed, ai_chat_budget, ai_chat_history,
  discount_scanned, discount_codes
"""
import sqlite3
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from config import DB_FILE

_executor = ThreadPoolExecutor(max_workers=5)
logger    = logging.getLogger(__name__)

# Persistente SQLite-Verbindung PRO Worker-Thread (spart connect/close pro Query).
# WAL erlaubt parallele Leser ueber die bis zu 5 Executor-Threads; Schreiber werden
# von SQLite serialisiert (busy_timeout wartet bei Contention).
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


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
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            if commit:
                conn.commit()
            if fetch:
                return cur.fetchall()
            return cur.rowcount
        except Exception as e:
            try:
                conn.rollback()   # offene Transaktion verwerfen, Verbindung bleibt nutzbar
            except Exception:
                pass
            logger.error(f"❌ DB error | query={query!r} params={params} | {e}")
            raise

    return await bot.loop.run_in_executor(_executor, _sync)


async def execute_many(bot, query: str, seq_params, *, commit: bool = True) -> int:
    """Fuehrt eine Query gebuendelt fuer viele Parametersaetze aus – EINE Verbindung,
    executemany, EINE Transaktion (statt einer Query pro Element). Leere Sequenz -> 0."""
    rows = list(seq_params)
    if not rows:
        return 0

    def _sync():
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.executemany(query, rows)
            if commit:
                conn.commit()
            return cur.rowcount
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"❌ DB error (executemany) | query={query!r} n={len(rows)} | {e}")
            raise

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
    variant_id        INTEGER NOT NULL DEFAULT 0,
    variant_title     TEXT    NOT NULL DEFAULT '',
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
    PRIMARY KEY (user_id, product_id, variant_id)
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

-- Pro-Variante-Baseline für Arten-Beobachtungen: erlaubt dem Alert, ALLE
-- geänderten/neuen/entfallenen Varianten eines Produkts einzeln aufzulisten
-- (statt nur der aggregierten Min/Max-Spanne).
CREATE TABLE IF NOT EXISTS user_species_watch_variant_seen (
    user_id         TEXT    NOT NULL,
    watched_species TEXT    NOT NULL,
    product_id      INTEGER NOT NULL,
    variant_id      INTEGER NOT NULL,
    variant_title   TEXT,
    last_price      REAL,
    currency        TEXT,
    PRIMARY KEY (user_id, watched_species, product_id, variant_id)
);

-- Entfallene Varianten (Arten-Beobachtung) werden hier gesammelt und EINMAL
-- täglich zu fester Zeit als Übersicht verschickt (statt bei jedem Stundencheck).
CREATE TABLE IF NOT EXISTS pending_variant_removed (
    user_id         TEXT    NOT NULL,
    watched_species TEXT    NOT NULL,
    product_id      INTEGER NOT NULL,
    variant_id      INTEGER NOT NULL,
    variant_title   TEXT,
    last_price      REAL,
    currency        TEXT,
    product_title   TEXT,
    shop_name       TEXT,
    antcheck_url    TEXT,
    detected_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, watched_species, product_id, variant_id)
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
    expires_at   TEXT    NOT NULL,
    model        TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ai_history_expires
    ON ai_chat_history (expires_at);

-- KI-Chat: zuletzt gewaehltes Modell pro User (fuer Vorauswahl im Dropdown)
CREATE TABLE IF NOT EXISTS ai_chat_user_model (
    user_id    INTEGER PRIMARY KEY,
    model      TEXT    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

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

-- Befehls-Nutzungsprotokoll (Moderation); Kanal-Posts separat, DB-Retention per Cleanup
CREATE TABLE IF NOT EXISTS command_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    user_name   TEXT,
    command     TEXT,
    params      TEXT,
    channel_id  TEXT,
    server_id   TEXT,
    status      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_command_log_created ON command_log (created_at);
CREATE INDEX IF NOT EXISTS idx_command_log_user ON command_log (user_id);

-- Perf: heiße Abfragen ohne PK/UNIQUE-Abdeckung
CREATE INDEX IF NOT EXISTS idx_notifications_status   ON notifications (status);
CREATE INDEX IF NOT EXISTS idx_discount_codes_author  ON discount_codes (author);
CREATE INDEX IF NOT EXISTS idx_ai_chat_budget_user    ON ai_chat_budget (user_id);
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
    ("ai_chat_history",     "model",         "ALTER TABLE ai_chat_history ADD COLUMN model TEXT DEFAULT ''"),
]


def _migrate_upt_variant_id(conn) -> None:
    """
    Rebuild von user_price_tracking: fuegt variant_id/variant_title hinzu und
    setzt PK auf (user_id, product_id, variant_id). Idempotent: laeuft nur, wenn
    die Spalte variant_id noch fehlt. Bestehende Zeilen -> variant_id=0.
    Laeuft NACH den Spalten-Migrationen, damit target_price/target_mode existieren.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(user_price_tracking)").fetchall()]
    if not cols or "variant_id" in cols:
        return
    logger.info("🔄 DB-Migration: user_price_tracking -> variant_id (PK-Rebuild)")
    conn.execute("ALTER TABLE user_price_tracking RENAME TO _upt_old")
    conn.execute("""
        CREATE TABLE user_price_tracking (
            user_id           TEXT    NOT NULL,
            product_id        INTEGER NOT NULL,
            variant_id        INTEGER NOT NULL DEFAULT 0,
            variant_title     TEXT    NOT NULL DEFAULT '',
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
            PRIMARY KEY (user_id, product_id, variant_id)
        )
    """)
    conn.execute("""
        INSERT INTO user_price_tracking
            (user_id, product_id, variant_id, variant_title, species, product_title,
             product_url, shop_name, shop_id, currency_iso, last_notified_min,
             last_notified_max, target_price, target_mode, added_at)
        SELECT user_id, product_id, 0, '', species, product_title,
               product_url, shop_name, shop_id, currency_iso, last_notified_min,
               last_notified_max, target_price, target_mode, added_at
        FROM _upt_old
    """)
    conn.execute("DROP TABLE _upt_old")
    conn.commit()


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
            # PK-Rebuild-Migration: user_price_tracking um variant_id erweitern
            # (bestehende Zeilen -> variant_id=0 = "ganzes Produkt", Verhalten unveraendert)
            try:
                _migrate_upt_variant_id(conn)
            except Exception as e:
                logger.error(f"❌ variant_id-Migration fehlgeschlagen: {e}")
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
