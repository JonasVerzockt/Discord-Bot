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
config.py - Zentrale Konfiguration für den AAM Discord Bot.
Alle Konstanten und Umgebungsvariablen werden hier geladen.
"""
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# Alle Laufzeit-/Datendateien liegen zentral in DATA_DIR (Standard: data/).
# Über die Env-Variable DATA_DIR verlegbar; DATA_DIR=. behält das alte Root-Layout.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Bot-Logdateien (bot_log_YYYYMMDD.log) liegen in LOG_DIR (Standard: logs/).
# Über LOG_DIR verlegbar (z.B. /var/log/antcheck, sofern für den Bot-User beschreibbar).
LOG_DIR = Path(os.getenv("LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Bot-Version – wird im Discord-Status vor den Sprüchen angezeigt (Schema x.y.z).
VERSION = "5.2.0"

# Discord
DISCORD_TOKEN     = os.getenv("DISCORD_TOKEN")
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0"))

# Google Sheets (Review-Bot)
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SHEET_NAME     = "Rohdaten"

# Datenbank
DB_FILE = Path(os.getenv("DB_FILE", str(DATA_DIR / "antcheckbot.db")))

# AntCheck API / Shop-Daten
DATA_DIRECTORY  = os.getenv("DATA_DIRECTORY", str(DATA_DIR))
SHOPS_DATA_FILE = os.getenv("SHOPS_DATA_FILE", str(Path(DATA_DIRECTORY) / "shops_data.json"))

# Ameisen-Artenliste (GBIF) – siehe tools/build_ant_species.py
SPECIES_CATALOG_FILE = Path(os.getenv("SPECIES_CATALOG_FILE", str(DATA_DIR / "ant_species.json")))

# Review-Bot
MAPPING_FILE = str(DATA_DIR / "shop_mapping.csv")

# Lokalisierung
LOCALES_DIR = BASE_DIR / "locales"

# Verhalten
SCAN_DAYS          = 90
FUZZY_THRESHOLD    = 81
# Sekunden die der Bot wartet, bevor er eine Review verarbeitet –
# damit geteilte Nachrichten desselben Users zusammengeführt werden können.
ACCUMULATION_DELAY = int(os.getenv("ACCUMULATION_DELAY", "8"))

# Anthropic (Review-Bot + KI-Chat)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# Modell für die KI-Extraktion der Reviews (günstiges Haiku für strukturierte Parserei).
REVIEW_PARSER_MODEL = os.getenv("REVIEW_PARSER_MODEL", "claude-haiku-5-5")

# ── Rabattcode-Tracker ────────────────────────────────────────────────────────
# Kanäle, in denen Rabattcodes gepostet werden (leer = Feature inaktiv).
# Primär: DISCOUNT_CHANNEL_ID (rückwärtskompatibel). Optional ein zweiter Kanal
# über DISCOUNT_CHANNEL_ID_2, oder beliebig viele kommagetrennt über
# DISCOUNT_CHANNEL_IDS. Alle Quellen werden zu DISCOUNT_CHANNEL_IDS zusammengeführt
# (dedupliziert, Reihenfolge erhalten).
DISCOUNT_CHANNEL_ID = int(os.getenv("DISCOUNT_CHANNEL_ID", "0"))
DISCOUNT_CHANNEL_ID_2 = int(os.getenv("DISCOUNT_CHANNEL_ID_2", "0"))
DISCOUNT_CHANNEL_IDS: list[int] = []
for _cid in [
    DISCOUNT_CHANNEL_ID,
    DISCOUNT_CHANNEL_ID_2,
    *[int(x) for x in os.getenv("DISCOUNT_CHANNEL_IDS", "").split(",") if x.strip()],
]:
    if _cid and _cid not in DISCOUNT_CHANNEL_IDS:
        DISCOUNT_CHANNEL_IDS.append(_cid)
# Modell für die Code-Extraktion (günstiges Haiku für strukturierte Parserei).
DISCOUNT_PARSER_MODEL = os.getenv("DISCOUNT_PARSER_MODEL", "claude-haiku-5-5")
# Bild-Analyse: gepostete Screenshots/Flyer/Werbung ebenfalls per Vision auf
# Rabattcodes prüfen (Standard an). Nur Datei-Anhänge, keine verlinkten Bilder.
DISCOUNT_VISION_ENABLED = os.getenv("DISCOUNT_VISION_ENABLED", "true").lower() == "true"
# Max. Anzahl Bilder pro Nachricht, die an die Vision-API gehen.
DISCOUNT_VISION_MAX_IMAGES = int(os.getenv("DISCOUNT_VISION_MAX_IMAGES", "4"))
# Max. Bildgröße in Bytes (wie beim KI-Chat: 4 MB). Größere werden übersprungen.
DISCOUNT_VISION_MAX_BYTES = int(os.getenv("DISCOUNT_VISION_MAX_BYTES", "4000000"))

# ── Command-Log (Moderation) ──────────────────────────────────────────────────
# Kanal-ID für das gebündelte Befehls-Log (0/leer = kein Kanal-Post; DB-Log läuft
# trotzdem). Wird von Jonas selbst gesetzt.
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0"))
# Aufbewahrung der Log-Zeilen in der DB (Tage). 365 = zweckgebunden für Moderation
# (siehe NUTZUNGSBEDINGUNGEN). Kanal-Nachrichten werden NICHT automatisch gelöscht.
COMMAND_LOG_RETENTION_DAYS = int(os.getenv("COMMAND_LOG_RETENTION_DAYS", "365"))

# ── Schlagwort-Alerts für den Angebote-Kanal (/offer_alert) ───────────────────
# Kanal, der auf Wunsch-Schlagworte überwacht wird (0/leer = Feature inaktiv).
OFFER_CHANNEL_ID = int(os.getenv("OFFER_CHANNEL_ID", "0"))
# Prüf-Puffer: Angebote, die jünger als so viele Minuten sind, werden NICHT gemeldet
# (Team-Review-Fenster).
OFFER_ALERT_DELAY_MIN = int(os.getenv("OFFER_ALERT_DELAY_MIN", "60"))
# Rückblick beim einmaligen Backfill nach Einrichten eines Schlagworts (Tage).
OFFER_BACKFILL_DAYS = int(os.getenv("OFFER_BACKFILL_DAYS", "90"))
# Custom-Emote-ID, mit der „verkauft" markiert wird (im Text oder als Reaktion).
OFFER_SOLD_EMOTE_ID = int(os.getenv("OFFER_SOLD_EMOTE_ID", "577904916006174741"))
# Nachtruhe (Berliner Zeit): im Fenster [START, END) werden KEINE automatischen
# Alert-PNs verschickt; nachts aufgelaufene Treffer gehen gesammelt ab END raus.
# Standard 23–9 Uhr. START==END schaltet die Nachtruhe aus. Der Live-Scanner hält
# nachts gepostete Angebote zurueck; der Einricht-Backfill kappt – wenn er WAEHREND
# der Nachtruhe laeuft – die Angebote der laufenden Nacht (frueherer Naechte bleiben
# drin). Der Admin-/offer_alert check ist unberuehrt.
OFFER_QUIET_START_HOUR = int(os.getenv("OFFER_QUIET_START_HOUR", "23"))
OFFER_QUIET_END_HOUR   = int(os.getenv("OFFER_QUIET_END_HOUR", "9"))

# ── KI-Chat-Bot ───────────────────────────────────────────────────────────────
# Modell für den Chat (Standard: claude-haiku-5-5)
AI_CHAT_MODEL = os.getenv("AI_CHAT_MODEL", "claude-haiku-5-5")
# Im Modell-Dropdown mit 👍 als Empfehlung markiertes Modell – unabhängig von der
# Vorauswahl (AI_CHAT_MODEL / zuletzt gewählt). Leer = keine Empfehlung anzeigen.
AI_CHAT_RECOMMENDED_MODEL = os.getenv("AI_CHAT_RECOMMENDED_MODEL", "claude-sonnet-5-5")
# Modell für die Stufe-2-Shop-Relevanz-Klassifikation (günstiges Haiku).
AI_CHAT_CLASSIFY_MODEL = os.getenv("AI_CHAT_CLASSIFY_MODEL", "claude-haiku-5-5")

# Kanal-ID in dem der Bot auf ALLE Nachrichten reagiert (eine ID).
# Zusaetzlich reagiert der Bot immer auf @-Erwaehnung in jedem Kanal.
# Leer lassen = nur @-Erwaehnung funktioniert.
AI_CHAT_CHANNEL_IDS: list[int] = [
    int(x) for x in os.getenv("AI_CHAT_CHANNEL_IDS", "").split(",") if x.strip()
]

# Budget in USD (Reset täglich 00:00 UTC)
AI_CHAT_DAILY_BUDGET_USD      = float(os.getenv("AI_CHAT_DAILY_BUDGET_USD",      "0.50"))
AI_CHAT_USER_DAILY_BUDGET_USD = float(os.getenv("AI_CHAT_USER_DAILY_BUDGET_USD", "0.10"))

# Limits pro Anfrage (Schutz vor teuren Requests)
AI_CHAT_MAX_INPUT_CHARS   = int(os.getenv("AI_CHAT_MAX_INPUT_CHARS",   "1500"))
AI_CHAT_MAX_OUTPUT_TOKENS = int(os.getenv("AI_CHAT_MAX_OUTPUT_TOKENS", "800"))
AI_CHAT_MAX_HISTORY_TURNS = int(os.getenv("AI_CHAT_MAX_HISTORY_TURNS", "10"))
# Anteil der max. Output-Tokens, mit dem die Budget-VORAB-Schätzung rechnet.
# Antworten liegen real meist deutlich unter dem Maximum -> 1.0 (immer Maximum)
# überschätzt stark und blockt Anfragen unnötig. 0.5 = realistischer Mittelwert
# mit Sicherheitsreserve. Die TATSÄCHLICHEN Kosten werden danach exakt abgerechnet.
AI_CHAT_BUDGET_OUTPUT_RATIO = float(os.getenv("AI_CHAT_BUDGET_OUTPUT_RATIO", "0.5"))
# Prompt-Caching (5-Min) fuer den stabilen System+Shop-Praefix – NUR bei
# Reply-Fortsetzungen (aktive Gespraeche), wo Folge-Turns den Cache lesen.
# Kalte Einzelfragen bleiben ungecacht. false = Caching komplett aus.
AI_CHAT_PROMPT_CACHE = os.getenv("AI_CHAT_PROMPT_CACHE", "true").lower() == "true"
# Werkzeuge (Tool Use): der Chat fragt Angebote, Preise, Shops und Codes selbst ab.
AI_CHAT_TOOLS = os.getenv("AI_CHAT_TOOLS", "true").lower() == "true"
# Höchstens so viele Werkzeug-Runden pro Frage (jede Runde = ein weiterer API-Aufruf).
AI_CHAT_TOOL_ROUNDS = int(os.getenv("AI_CHAT_TOOL_ROUNDS", "3"))

# Jahresrückblick: Kanal für den Post am 31.12. um 23 Uhr (Berliner Zeit). 0 = kein Post.
YEAR_REVIEW_CHANNEL_ID = int(os.getenv("YEAR_REVIEW_CHANNEL_ID", "0") or "0")

# Schwarmflug-Radar: Kanal mit den Schwarmflug-Meldungen (0 = Funktion aus).
SWARM_CHANNEL_ID = int(os.getenv("SWARM_CHANNEL_ID", "0") or "0")
# KI-Modell für Freitext-Meldungen ohne Vorlage (Standard wie der Review-Parser).
SWARM_PARSER_MODEL = os.getenv("SWARM_PARSER_MODEL", REVIEW_PARSER_MODEL)
# Ortsauflösung über OpenStreetMap Nominatim (nur der Ortsname wird übertragen).
SWARM_OSM_GEOCODING = os.getenv("SWARM_OSM_GEOCODING", "true").lower() in ("1", "true", "yes")
# Wetter zur Flugzeit nachtragen (Open-Meteo, nur gerundete Koordinate und Datum werden übertragen).
SWARM_WEATHER = os.getenv("SWARM_WEATHER", "true").lower() in ("1", "true", "yes")
# Schwarmflug-Prognose (Open-Meteo-Vorhersage + gelerntes Modell), Post im Schwarmflug-Kanal
SWARM_FORECAST = os.getenv("SWARM_FORECAST", "true").lower() in ("1", "true", "yes")
SWARM_FORECAST_POST = os.getenv("SWARM_FORECAST_POST", "true").lower() in ("1", "true", "yes")
# Tagesbudget Open-Meteo gesamt (freie API: < 10.000/Tag); Rest bleibt als Puffer frei.
SWARM_OM_DAILY = int(os.getenv("SWARM_OM_DAILY", "8500") or "8500")

# Wie lange wird eine Konversation gespeichert (Stunden)
AI_CHAT_CONVERSATION_TTL_HOURS = int(os.getenv("AI_CHAT_CONVERSATION_TTL_HOURS", "24"))
AI_CHAT_PUBLIC = os.getenv("AI_CHAT_PUBLIC", "false").lower() == "true"

# System-Prompts: eine Datei pro Sprache (de/en/eo).
# Dateiname: ai_chat_system_prompt_{lang}.txt
# ai_chat_system_prompt_en.txt ist Pflicht und dient als Fallback für alle Sprachen.
# ── Feedback-Board (öffentliches Ideen-/Bug-Board, eigener Webdienst) ──────────
# Standardmäßig AUS: Ist BOARD_ENABLED nicht 'true', startet der Board-Cog nichts
# und der Bot bleibt unberührt. Läuft im Bot-Prozess (aiohttp), eigene DB-Datei.
BOARD_ENABLED     = os.getenv("BOARD_ENABLED", "false").lower() == "true"
BOARD_BIND        = os.getenv("BOARD_BIND", "127.0.0.1")   # nur lokal binden -> Caddy davor
BOARD_PORT        = int(os.getenv("BOARD_PORT", "8080"))
# Öffentliche URL (für Links/Anzeige); darf zunächst leer sein.
BOARD_PUBLIC_URL  = os.getenv("BOARD_PUBLIC_URL", "").strip().rstrip("/")
# Optionaler Discord-Einladungslink (https://discord.gg/…) für den Kasten „Mitmachen“
# auf der Karte. Leer = Button wird nicht angezeigt.
DISCORD_INVITE_URL = os.getenv("DISCORD_INVITE_URL", "").strip()
# Discord-User-ID des Owners: Owner-DMs (Einreichungen, Alarme) UND Admin-Login im
# Board (über „Login mit Discord“, kein Token mehr). Optional weitere Admin-IDs,
# kommagetrennt. BOARD_ADMIN_TOKEN wird nicht mehr verwendet.
BOARD_OWNER_ID    = int(os.getenv("BOARD_OWNER_ID", "0") or "0")
BOARD_ADMIN_IDS: list[int] = [int(x) for x in os.getenv("BOARD_ADMIN_IDS", "").replace(" ", "").split(",")
                              if x.isdigit()]
# Eigene, separate DB-Datei (nicht die Haupt-Bot-DB).
BOARD_DB_FILE     = os.getenv("BOARD_DB_FILE", str(DATA_DIR / "board.db"))
# KI-Chat als schwebendes Fenster auf dem Board (nur eingeloggte AAM-Mitglieder,
# gleiches Tagesbudget wie im Discord). Grenzen je Mitglied: Nachrichten pro Minute/Stunde.
BOARD_AI_CHAT          = os.getenv("BOARD_AI_CHAT", "true").lower() == "true"
BOARD_AI_CHAT_PER_MIN  = int(os.getenv("BOARD_AI_CHAT_PER_MIN", "5"))
BOARD_AI_CHAT_PER_HOUR = int(os.getenv("BOARD_AI_CHAT_PER_HOUR", "40"))
# Geheimer Schlüssel für alle HMAC-Signaturen des Boards: IP-Hashes (keine Roh-IP gespeichert),
# Admin-/Mitglieder-Cookies, CSRF-Tokens, Captcha. PFLICHT bei BOARD_ENABLED=true: mit dem
# Platzhalter oder einem zu kurzen Wert startet das Board nicht (sonst wären Cookies fälschbar).
# Erzeugen z. B. mit:  python3 -c "import secrets; print(secrets.token_hex(32))"
BOARD_HASH_SALT_PLACEHOLDER = "change-me-board-salt"
BOARD_HASH_SALT_MIN_LEN     = 16
BOARD_HASH_SALT   = os.getenv("BOARD_HASH_SALT", BOARD_HASH_SALT_PLACEHOLDER).encode()

# ── Halter-Karte (Map-Feature) ────────────────────────────────────────────────
# Standardmäßig AUS. Zeigt Ameisenhalter grob (DACH-Karte) + International als Liste.
MAP_ENABLED        = os.getenv("MAP_ENABLED", "false").lower() == "true"
# Guild, gegen die die Mitgliedschaft geprüft wird (für Login + Auto-Löschung).
MAP_GUILD_ID       = int(os.getenv("MAP_GUILD_ID", "0") or "0")
# Fuzzing: fester, aus der user_id geseedeter Versatz (Meter). Standard ±3 km.
MAP_JITTER_METERS  = int(os.getenv("MAP_JITTER_METERS", "3000"))
# Max. Zoomstufe der Karte (Stadt/Umland, keine Straßenebene).
MAP_MAX_ZOOM       = int(os.getenv("MAP_MAX_ZOOM", "10"))
# Jahres-Bestätigung: nach so vielen Monaten erinnern bzw. löschen (voller Zweit-Zyklus).
MAP_REMIND_MONTHS  = int(os.getenv("MAP_REMIND_MONTHS", "12"))
MAP_DELETE_MONTHS  = int(os.getenv("MAP_DELETE_MONTHS", "24"))
# Geodaten (Leaflet, GeoJSON-Umrisse, PLZ-Koordinaten) automatisch vom Bot bereitstellen
# & aktuell halten (utils/map_geodata.py: geoBoundaries + GeoNames, kein externes Tool).
# Standard AUS. Intervall in Tagen.
MAP_GEODATA_AUTO         = os.getenv("MAP_GEODATA_AUTO", "false").lower() == "true"
MAP_GEODATA_REFRESH_DAYS = int(os.getenv("MAP_GEODATA_REFRESH_DAYS", "30"))

# ── Discord-OAuth2 ("Login mit Discord" für die Mitglieder-Kartenebene) ────────
# Secrets gehören in die .env (NICHT committen). Scope: nur "identify".
BOARD_OAUTH_CLIENT_ID     = os.getenv("BOARD_OAUTH_CLIENT_ID", "").strip()
BOARD_OAUTH_CLIENT_SECRET = os.getenv("BOARD_OAUTH_CLIENT_SECRET", "").strip()
# Muss exakt der im Discord Developer Portal hinterlegten Redirect-URI entsprechen.
BOARD_OAUTH_REDIRECT_URI  = os.getenv("BOARD_OAUTH_REDIRECT_URI", "").strip()

AI_CHAT_SYSTEM_PROMPTS: dict[str, str] = {}
for _lang in ("de", "en", "eo"):
    _f = BASE_DIR / f"ai_chat_system_prompt_{_lang}.txt"
    if _f.exists():
        AI_CHAT_SYSTEM_PROMPTS[_lang] = (
            _f.read_text(encoding="utf-8").strip().replace("{model}", AI_CHAT_MODEL)
        )

# Die englische Datei ist Pflicht (Fallback für alle Sprachen). Fehlt sie, wird ein
# klarer Fehler geloggt; der KI-Chat lehnt Anfragen dann mit Fehlermeldung ab.
if "en" not in AI_CHAT_SYSTEM_PROMPTS:
    logging.getLogger(__name__).error(
        "❌ ai_chat_system_prompt_en.txt fehlt – KI-Chat hat keinen "
        "Fallback-System-Prompt und wird Anfragen mit Fehlermeldung ablehnen."
    )

# ── Einmalige Daten-Migration (altes Root-Layout → data/) ──────────────────────
# Verschiebt bestehende Laufzeitdaten beim Start automatisch nach DATA_DIR, damit
# vorhandene Installationen ohne manuelles Verschieben weiterlaufen (idempotent,
# nur wenn Quelle da und Ziel frei; SQLite-Begleitdateien werden mitgezogen).
try:
    from utils.paths import migrate_legacy_files as _migrate
    _migrate({
        BASE_DIR / "antcheckbot.db":  DB_FILE,
        BASE_DIR / "board.db":        Path(BOARD_DB_FILE),
        BASE_DIR / "shop_mapping.csv": Path(MAPPING_FILE),
        BASE_DIR / "shops_data.json":  Path(SHOPS_DATA_FILE),
        BASE_DIR / "price_history.db": Path(DATA_DIRECTORY) / "price_history.db",
        BASE_DIR / "ant_species.json": SPECIES_CATALOG_FILE,
    })
except Exception as _e:  # Migration darf den Start nie verhindern
    logging.getLogger(__name__).warning("⚠️ Daten-Migration übersprungen: %s", _e)
