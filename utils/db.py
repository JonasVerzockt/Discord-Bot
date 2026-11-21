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

-- KI-Chat: kumulierte Ausgaben pro User je Zeitraum (Tag/Woche/Monat/Jahr).
-- Reines Ledger fuer den DSGVO-Datenexport – KEINE Abfrage-Befehle darauf.
-- period_type: 'day'|'week'|'month'|'year'; period_key z.B. 2026-07-23 / 2026-W30 / 2026-07 / 2026
CREATE TABLE IF NOT EXISTS ai_chat_user_spend (
    user_id     INTEGER NOT NULL,
    period_type TEXT    NOT NULL,
    period_key  TEXT    NOT NULL,
    cost_usd    REAL    NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, period_type, period_key)
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
-- Lieferzeit je Bewertung (Kalendertage Bestellung → Ankunft), aus dem Review-Text.
-- shop = Domain wie in der Bewertungstabelle (clean_shop_name).
CREATE TABLE IF NOT EXISTS review_delivery (
    message_id   TEXT PRIMARY KEY,
    shop         TEXT NOT NULL,
    days         INTEGER,             -- NULL = keine Angabe (bereits ausgewertet)
    review_date  TEXT,                -- YYYY-MM-DD
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_review_delivery_shop ON review_delivery (shop);

-- Wer hat welche Bewertung geschrieben – NUR für den persönlichen Rückblick (/rueckblick,
-- nur für die Person selbst sichtbar). Die Bewertungstabelle (Sheet) bleibt anonym.
-- rating NULL = Bewertung keiner Tabellenzeile zugeordnet.
CREATE TABLE IF NOT EXISTS review_authors (
    message_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    review_date  TEXT NOT NULL,       -- YYYY-MM-DD
    shop         TEXT NOT NULL DEFAULT '',
    rating       REAL
);
CREATE INDEX IF NOT EXISTS idx_review_authors_user ON review_authors (user_id, review_date);

-- iNaturalist-Statistik (ersetzt den früheren Wettbewerb mit Google-Tabelle).
-- Bestätigte Verknüpfung Discord-Konto ↔ iNat-Konto (/inat_verknuepfen, Code im iNat-Profil).
CREATE TABLE IF NOT EXISTS inat_accounts (
    discord_id    TEXT PRIMARY KEY,
    inat_user_id  INTEGER NOT NULL UNIQUE,
    inat_login    TEXT NOT NULL,
    verified_at   TEXT NOT NULL
);
-- Offene Verknüpfungs-Codes (1 Stunde gültig).
CREATE TABLE IF NOT EXISTS inat_link_codes (
    discord_id  TEXT PRIMARY KEY,
    inat_login  TEXT NOT NULL,
    code        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
-- Gepostete Beobachtungen. Gezählt wird nur, wenn inat_user_id zum verknüpften Konto passt.
CREATE TABLE IF NOT EXISTS inat_obs (
    obs_id        INTEGER PRIMARY KEY,
    discord_id    TEXT NOT NULL,
    message_id    TEXT,
    posted_at     TEXT NOT NULL,          -- UTC
    inat_user_id  INTEGER,
    observed_on   TEXT,                   -- YYYY-MM-DD laut iNat
    taxon_id      INTEGER,
    taxon_name    TEXT,
    taxon_rank    TEXT,
    ancestors     TEXT,                   -- JSON-Liste der Vorfahren-IDs
    is_ant        INTEGER NOT NULL DEFAULT 0,
    missing       INTEGER NOT NULL DEFAULT 0,   -- 1 = bei iNat gelöscht/nicht gefunden
    checked_at    TEXT                    -- NULL = noch nicht von der API geprüft
);
CREATE INDEX IF NOT EXISTS idx_inat_obs_user ON inat_obs (discord_id);
CREATE TABLE IF NOT EXISTS inat_posted_months (
    month      TEXT PRIMARY KEY,          -- YYYY-MM, verhindert Doppel-Posts
    posted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Saison-Verfügbarkeit: je Tag und Art die Zahl der Shops mit lagerndem Angebot
-- (Maximum über die stündlichen Läufe des Tages). species_key '*' markiert, dass der
-- Tag erfasst wurde (fehlende Art an so einem Tag = 0 Shops).
CREATE TABLE IF NOT EXISTS species_stock_daily (
    day          TEXT NOT NULL,       -- YYYY-MM-DD (Berlin)
    species_key  TEXT NOT NULL,       -- kanonischer Artname, klein
    shops        INTEGER NOT NULL,
    PRIMARY KEY (day, species_key)
);
CREATE INDEX IF NOT EXISTS idx_species_stock_key ON species_stock_daily (species_key, day);

-- Jahresrückblick: bereits gepostete Jahre (verhindert Doppel-Posts nach Neustart).
CREATE TABLE IF NOT EXISTS year_review_posted (
    year       INTEGER PRIMARY KEY,
    posted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Öffentliche Nutzungsstatistik: Monatssummen (überdauern die Command-Log-Retention).
-- metric: 'calls' | 'users' | 'cmd:<befehl>'; nur Befehle für alle (keine Admin-Befehle).
CREATE TABLE IF NOT EXISTS usage_monthly (
    month   TEXT    NOT NULL,      -- YYYY-MM
    metric  TEXT    NOT NULL,
    value   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, metric)
);

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

-- Benutzerdefinierte Text-Befehle ("Tags"): von Admins angelegt, per /tag abrufbar.
-- content = Rohtext inkl. Markdown; admin_only = nur Admins dürfen abrufen;
-- as_embed = als Embed statt Klartext posten.
CREATE TABLE IF NOT EXISTS custom_commands (
    name        TEXT    PRIMARY KEY,   -- kleingeschrieben, eindeutig
    content     TEXT    NOT NULL,
    description TEXT    DEFAULT '',     -- Kurzbeschreibung (in der /info-Autocomplete)
    admin_only  INTEGER NOT NULL DEFAULT 0,
    as_embed    INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT,
    uses        INTEGER NOT NULL DEFAULT 0
);

-- Schlagwort-Alerts für den Angebote-Kanal (/offer_alert)
CREATE TABLE IF NOT EXISTS offer_keywords (
    user_id     TEXT NOT NULL,
    keyword     TEXT NOT NULL,          -- normalisiert (klein, getrimmt)
    keyword_raw TEXT,                    -- Originaleingabe für die Anzeige
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, keyword)
);
-- Bereits gemeldete Treffer (Dedup: kein Doppel-Alert je User+Nachricht+Schlagwort)
CREATE TABLE IF NOT EXISTS offer_alert_seen (
    user_id    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    keyword    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, message_id, keyword)
);
-- Fortschritt des Vorwärts-Scanners (zuletzt verarbeitete Nachricht)
CREATE TABLE IF NOT EXISTS offer_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_message_id TEXT
);
-- Zuordnung Alert-PN → (User, Schlagwort) für den Reaktions-Lebenszyklus (✅/🔄)
CREATE TABLE IF NOT EXISTS offer_alert_msgs (
    message_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    keyword    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Zurückgestellte Angebote (Nachtruhe 23–9 bzw. Admin-⏸️): werden nicht sofort
-- gemeldet, sondern jeden Scan neu geprüft und nach Wegfall des Grundes nachgeliefert.
CREATE TABLE IF NOT EXISTS offer_deferred (
    message_id TEXT PRIMARY KEY,
    reason     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Halter-Karte (Map) ──────────────────────────────────────────────────────
-- Opt-in-Einträge. Der Discord-Name wird NICHT gespeichert (live über user_id
-- aufgelöst); nur ein optionaler Vorname. Koordinaten ausschließlich gefuzzt
-- (PLZ-Zentroid + fester, user-ID-geseedeter Jitter); für Nicht-DACH NULL
-- (dann nur Listeneintrag). show_entry=0 => nur anonyme Zählung (z.B. U18),
-- kein Pin / keine Listenzeile.
CREATE TABLE IF NOT EXISTS map_entries (
    user_id           TEXT PRIMARY KEY,
    country           TEXT NOT NULL DEFAULT '',   -- ISO-2 (de/at/ch/li/…)
    region_code       TEXT,                       -- Bundesland/Kanton-Code (DACH)
    region_name       TEXT,                       -- Anzeigename der Region
    plz_prefix        TEXT,                       -- für PLZ-Gebiet-Choropleth
    lat_fuzzed        REAL,                       -- NULL für Nicht-DACH
    lon_fuzzed        REAL,
    first_name        TEXT,                       -- optional, nur Vorname
    show_entry        INTEGER NOT NULL DEFAULT 0, -- 1 = Pin/Listenzeile sichtbar (18+)
    show_name         INTEGER NOT NULL DEFAULT 1, -- 0 = Name/Vorname ausblenden (anonym)
    coarse            INTEGER NOT NULL DEFAULT 0, -- 1 = Pin auf Mitte des groben PLZ-Gebiets (letzte 2 Ziffern weg)
    age_ok            INTEGER NOT NULL DEFAULT 0,
    contact_ok        INTEGER NOT NULL DEFAULT 0, -- Opt-in: 1 nur wenn aktiv zugestimmt ("Kontakt über Discord")
    consent_at        TEXT,
    last_confirmed_at TEXT,
    reminder_sent_at  TEXT,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Selbst zugewiesene Tags (Mehrfachauswahl, fester Katalog in utils/map_tags.py).
CREATE TABLE IF NOT EXISTS map_entry_tags (
    user_id  TEXT NOT NULL,
    tag_code TEXT NOT NULL,
    PRIMARY KEY (user_id, tag_code)
);

-- „Auch aktiv in“: bis zu 2 weitere Regionen (nur Bundesland/Kanton, kein Ort), z. B. für Pendler.
-- Nur für 18+-Einträge; wird mit dem Karteneintrag gelöscht.
CREATE TABLE IF NOT EXISTS map_entry_regions (
    user_id     TEXT NOT NULL,
    country     TEXT NOT NULL,
    region_code TEXT NOT NULL,
    PRIMARY KEY (user_id, country, region_code)
);

-- Events / Messen / Treffpunkte: ÖFFENTLICH, nicht personenbezogen.
-- status: pending | approved | rejected. rrule = iCal-RRULE (optional, für Serien).
CREATE TABLE IF NOT EXISTS map_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    type         TEXT NOT NULL DEFAULT 'other',
    country      TEXT,
    lat          REAL,
    lon          REAL,
    venue        TEXT,
    plz          TEXT,
    start_at     TEXT NOT NULL,                  -- ISO (Berliner Zeit)
    end_at       TEXT,
    all_day      INTEGER NOT NULL DEFAULT 0,
    rrule        TEXT,                           -- iCal RRULE (optional)
    exdates      TEXT,                           -- ausgefallene Serien-Termine (ISO, kommagetrennt)
    url          TEXT,
    description  TEXT,
    submitted_by TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Teilnahme an Events ("Ich gehe hin", über Discord-Login auf der Karte). Pro Vorkommen
-- (occ_date = Datum des Termins, bei Serien das jeweilige Vorkommen). Wird 30 Tage nach dem
-- Termin, beim Server-Austritt und beim Löschen des Events entfernt.
CREATE TABLE IF NOT EXISTS map_event_rsvp (
    event_id   INTEGER NOT NULL,
    occ_date   TEXT    NOT NULL,              -- JJJJ-MM-TT (Berliner Zeit)
    user_id    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, occ_date, user_id)
);

-- Termin-Erinnerung: bereits verschickte Erinnerungen (je Vorkommen und Person nur einmal).
CREATE TABLE IF NOT EXISTS map_rsvp_reminded (
    event_id  INTEGER NOT NULL,
    occ_date  TEXT    NOT NULL,
    user_id   TEXT    NOT NULL,
    sent_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, occ_date, user_id)
);

-- ── Mein Konto (Board /konto) ────────────────────────────────────────────────
-- Persönliche Einstellungen, die nur das Konto betreffen. Fehlende Zeile = Standard.
-- quiet_start/quiet_end: Ruhezeit in vollen Stunden (Berlin), NULL = aus.
-- logout_before: Board-Sitzungen, die vor diesem Zeitpunkt (Unix) begonnen haben, sind ungültig.
-- ics_salt: Teil des geheimen Links zum persönlichen Kalender (neu erzeugen = alter Link ungültig).
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id        TEXT PRIMARY KEY,
    rsvp_reminder  INTEGER NOT NULL DEFAULT 1,
    quiet_start    INTEGER,
    quiet_end      INTEGER,
    logout_before  INTEGER NOT NULL DEFAULT 0,
    ics_salt       TEXT,
    intros         INTEGER NOT NULL DEFAULT 1,   -- Intros beim Öffnen des Boards (1 = an)
    ach_seen_at    TEXT,                         -- Erfolge bis hier schon per Intro gefeiert (UTC)
    milestones     TEXT,                         -- schon gefeierte Meilensteine, z. B. "inat10,rev100"
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Verlauf: was der Bot dir zuletzt per PN geschickt hat (nur Vorschau, 30 Tage, max. 50 je Person).
CREATE TABLE IF NOT EXISTS dm_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    sent_at  TEXT NOT NULL DEFAULT (datetime('now')),
    preview  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dm_log_user ON dm_log (user_id, sent_at);
-- Angebots-Alerts, die während der persönlichen Ruhezeit anfielen (danach zugestellt).
CREATE TABLE IF NOT EXISTS quiet_offer_queue (
    user_id     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    keyword_raw TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, message_id, keyword)
);

-- ── Schwarmflug-Radar (Meldungen aus dem Schwarmflug-Kanal, utils/swarm.py) ──
-- Je Meldung und Taxon eine Zeile. Öffentlich nur Taxon, Datum und Region; place/lat/lon
-- und author_id nur für eingeloggte Mitglieder bzw. Datenauskunft.
CREATE TABLE IF NOT EXISTS swarm_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL,
    author_id    TEXT,
    posted_at    TEXT NOT NULL,          -- UTC
    flight_date  TEXT NOT NULL,          -- YYYY-MM-DD (Berlin)
    flight_time  TEXT,                   -- HH:MM oder NULL
    genus        TEXT NOT NULL,
    species      TEXT,
    qualifier    TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL,
    country      TEXT NOT NULL DEFAULT '',
    region       TEXT NOT NULL DEFAULT '',
    place        TEXT NOT NULL DEFAULT '',
    lat          REAL,
    lon          REAL,
    geo_src      TEXT,                   -- plz | osm | NULL
    temp_c       REAL,
    wind         TEXT,
    weather      TEXT,
    indoor       INTEGER NOT NULL DEFAULT 0,
    protected    TEXT,
    source       TEXT NOT NULL DEFAULT 'rule',   -- rule | ai | follow
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    -- Wetter zur Flugzeit (Open-Meteo, utils/weather.py); w_state: NULL offen, 1 ok, -1 nicht möglich
    w_temp       REAL,
    w_hum        REAL,                   -- relative Luftfeuchte %
    w_press      REAL,                   -- Luftdruck auf Meereshöhe hPa
    w_wind       REAL,                   -- Wind 10 m km/h
    w_cloud      REAL,                   -- Bewölkung %
    w_rain24     REAL,                   -- Niederschlag in den 24 h davor, mm
    w_state      INTEGER,
    w_src        TEXT,                   -- ifs | hf | fc (Quelle des nachgetragenen Wetters)
    UNIQUE (message_id, label)
);
CREATE INDEX IF NOT EXISTS idx_swarm_date ON swarm_reports (flight_date);
CREATE INDEX IF NOT EXISTS idx_swarm_genus ON swarm_reports (genus, species);
CREATE TABLE IF NOT EXISTS swarm_scanned (
    message_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL,             -- ok | none
    scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Schwarmflug-Alarm (Mein Konto): Gattung oder Art, optional nur eine Region.
CREATE TABLE IF NOT EXISTS swarm_alerts (
    user_id    TEXT NOT NULL,
    taxon      TEXT NOT NULL,             -- "Lasius" oder "Lasius niger"
    country    TEXT NOT NULL DEFAULT '',
    region     TEXT NOT NULL DEFAULT '',  -- '' = überall
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, taxon, country, region)
);
CREATE TABLE IF NOT EXISTS swarm_alert_sent (
    user_id    TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, message_id)
);
-- Einstellungen des Schwarmflug-Radars (z. B. ach_since: ab wann Meldungen für Erfolge zählen).
CREATE TABLE IF NOT EXISTS swarm_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Saisonstart je Jahr (erste Meldung im Freien), für Intro, Banner, PN und Erfolg.
CREATE TABLE IF NOT EXISTS swarm_season (
    year        INTEGER PRIMARY KEY,
    message_id  TEXT NOT NULL,
    author_id   TEXT,
    label       TEXT NOT NULL,
    country     TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT '',
    flight_date TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- ── Schwarmflug-Prognose (utils/forecast.py, Wetter von Open-Meteo) ──────────
-- Tagesbudget je Zweck (forecast | weather | train), Einheit = Open-Meteo-Aufrufe (gewichtet).
CREATE TABLE IF NOT EXISTS om_budget (
    day     TEXT NOT NULL,
    purpose TEXT NOT NULL,
    used    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, purpose)
);
-- Prognosepunkte: 35-km-Gitter über DACH + Fundort-Hotspots (0,1°-Zellen mit ≥ 3 Meldungen).
CREATE TABLE IF NOT EXISTS fc_points (
    key      TEXT PRIMARY KEY,           -- "lat,lon" auf 2 Nachkommastellen
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    country  TEXT NOT NULL DEFAULT '',
    region   TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,              -- grid | hot
    n        INTEGER NOT NULL DEFAULT 0, -- Meldungen in der Zelle (nur hot)
    train    INTEGER NOT NULL DEFAULT 0, -- 1 = Lernpunkt (Wetter ganzer Saisons)
    active   INTEGER NOT NULL DEFAULT 1
);
-- Lerndaten: Tageswerte je Lernpunkt (Wetter um 17 Uhr + Tageshöchstwert), alle Saisontage.
CREATE TABLE IF NOT EXISTS fc_train (
    key    TEXT NOT NULL,
    date   TEXT NOT NULL,
    temp REAL, hum REAL, dew REAL, rain24 REAL, rain48 REAL, rain72 REAL, dp12 REAL, wind REAL,
    cloud REAL, rad REAL, tmax REAL, soil_t REAL, soil_m REAL,
    src    TEXT NOT NULL DEFAULT 'ifs',   -- ifs (Historical Weather) | hf (Historical Forecast, feiner)
    PRIMARY KEY (key, date)
);
CREATE TABLE IF NOT EXISTS fc_train_done (
    key  TEXT NOT NULL,
    year INTEGER NOT NULL,
    src  TEXT NOT NULL DEFAULT 'ifs',
    PRIMARY KEY (key, year)
);
-- Verlauf der Trefferquote je Lernlauf (Vergleich mit dem Vorjahr auf dem Board)
CREATE TABLE IF NOT EXISTS fc_model_hist (
    taxon      TEXT NOT NULL,
    trained_at TEXT NOT NULL DEFAULT (datetime('now')),
    auc REAL, hit_hi REAL, hit_base REAL, n_pos INTEGER
);
-- Gelernte Modelle je Gattung/Art (oder Regel, solange zu wenig Daten).
CREATE TABLE IF NOT EXISTS fc_models (
    taxon      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,            -- model | rule
    params     TEXT NOT NULL,            -- JSON
    auc        REAL,
    hit_hi     REAL,                     -- Anteil Tage mit Meldung bei hoher Chance (Test-Jahre)
    hit_base   REAL,                     -- Anteil Tage mit Meldung insgesamt (Test-Jahre)
    n_pos      INTEGER,
    n_obs      INTEGER,
    trained_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Vorhersage je Punkt, Tag und Taxon (nur Stufe ≥ 1). level: 1 möglich, 2 gut, 3 sehr gut.
CREATE TABLE IF NOT EXISTS fc_pred (
    key    TEXT NOT NULL,
    day    TEXT NOT NULL,
    taxon  TEXT NOT NULL,
    p      REAL NOT NULL,
    level  INTEGER NOT NULL,
    h_from INTEGER, h_to INTEGER,
    temp   REAL,
    run_at TEXT NOT NULL,
    PRIMARY KEY (key, day, taxon)
);
CREATE INDEX IF NOT EXISTS idx_fc_pred_day ON fc_pred (day, taxon);
CREATE TABLE IF NOT EXISTS fc_runs (
    day  TEXT NOT NULL,
    slot INTEGER NOT NULL,
    at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (day, slot)
);
-- Prognose-PN (Mein Konto): off | rare | normal | often (ohne Eintrag: rare)
CREATE TABLE IF NOT EXISTS fc_pref (
    user_id TEXT PRIMARY KEY,
    level   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fc_sent (
    user_id TEXT NOT NULL,
    day     TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS fc_posts (
    day        TEXT PRIMARY KEY,
    message_id TEXT
);
-- Ortsauflösung (eigene PLZ-Daten bzw. OpenStreetMap Nominatim), auch Fehlschläge.
CREATE TABLE IF NOT EXISTS geocode_cache (
    query      TEXT PRIMARY KEY,
    lat        REAL,
    lon        REAL,
    country    TEXT,
    region     TEXT,
    src        TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Perf: heiße Abfragen ohne PK/UNIQUE-Abdeckung
CREATE INDEX IF NOT EXISTS idx_notifications_status   ON notifications (status);
CREATE INDEX IF NOT EXISTS idx_map_entries_country    ON map_entries (country);
CREATE INDEX IF NOT EXISTS idx_map_entry_tags_tag     ON map_entry_tags (tag_code);
CREATE INDEX IF NOT EXISTS idx_map_events_status      ON map_events (status);
CREATE INDEX IF NOT EXISTS idx_map_events_start       ON map_events (start_at);
CREATE INDEX IF NOT EXISTS idx_map_event_rsvp_user    ON map_event_rsvp (user_id);
CREATE INDEX IF NOT EXISTS idx_offer_keywords_user    ON offer_keywords (user_id);
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
