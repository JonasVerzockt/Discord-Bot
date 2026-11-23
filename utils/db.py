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
from pathlib import Path
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
# Tabellen-Definitionen: utils/schema.sql (eigene Datei, damit SQL lesbar/editierbar bleibt).
_SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
_SCHEMA = _SCHEMA_FILE.read_text(encoding="utf-8")

# Standard EU-Länder (falls DB noch leer)
_EU_COUNTRIES = [
    "at","be","bg","cy","cz","de","dk","ee","es","fi",
    "fr","gr","hr","hu","ie","it","lt","lu","lv","mt",
    "nl","pl","pt","ro","se","si","sk",
]


# Spalten-Migrationen für bestehende DBs: (Tabelle, Spalte, DDL)
_MIGRATIONS = [
    ("swarm_reports",  "raw_label",       "ALTER TABLE swarm_reports ADD COLUMN raw_label TEXT"),
    ("swarm_reports",  "taxon_check",     "ALTER TABLE swarm_reports ADD COLUMN taxon_check TEXT"),
    ("swarm_reports",  "w_src",           "ALTER TABLE swarm_reports ADD COLUMN w_src TEXT"),
    ("shops",          "url_override",    "ALTER TABLE shops ADD COLUMN url_override TEXT DEFAULT NULL"),
    ("discount_codes", "status_override", "ALTER TABLE discount_codes ADD COLUMN status_override TEXT"),
    ("user_price_tracking", "target_price", "ALTER TABLE user_price_tracking ADD COLUMN target_price REAL"),
    ("user_price_tracking", "target_mode",  "ALTER TABLE user_price_tracking ADD COLUMN target_mode TEXT"),
    ("ai_chat_history",     "model",         "ALTER TABLE ai_chat_history ADD COLUMN model TEXT DEFAULT ''"),
    ("custom_commands",     "description",   "ALTER TABLE custom_commands ADD COLUMN description TEXT DEFAULT ''"),
    ("map_entries",         "show_name",     "ALTER TABLE map_entries ADD COLUMN show_name INTEGER NOT NULL DEFAULT 1"),
    ("map_events",          "exdates",       "ALTER TABLE map_events ADD COLUMN exdates TEXT"),
    ("map_entries",         "coarse",        "ALTER TABLE map_entries ADD COLUMN coarse INTEGER NOT NULL DEFAULT 0"),
    ("user_prefs",          "intros",        "ALTER TABLE user_prefs ADD COLUMN intros INTEGER NOT NULL DEFAULT 1"),
    ("user_prefs",          "ach_seen_at",   "ALTER TABLE user_prefs ADD COLUMN ach_seen_at TEXT"),
    ("user_prefs",          "milestones",    "ALTER TABLE user_prefs ADD COLUMN milestones TEXT"),
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
