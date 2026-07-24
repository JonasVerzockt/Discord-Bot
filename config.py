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

# Bot-Version – wird im Discord-Status vor den Sprüchen angezeigt (Schema x.y.z).
VERSION = "1.3.7"

# Discord
DISCORD_TOKEN     = os.getenv("DISCORD_TOKEN")
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0"))

# Google Sheets (Review-Bot)
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SHEET_NAME     = "Rohdaten"

# Datenbank
DB_FILE = BASE_DIR / "antcheckbot.db"

# AntCheck API / Shop-Daten
DATA_DIRECTORY  = os.getenv("DATA_DIRECTORY", str(BASE_DIR))
SHOPS_DATA_FILE = os.getenv("SHOPS_DATA_FILE", str(BASE_DIR / "shops_data.json"))

# Review-Bot
MAPPING_FILE = str(BASE_DIR / "shop_mapping.csv")

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
REVIEW_PARSER_MODEL = os.getenv("REVIEW_PARSER_MODEL", "claude-haiku-4-5-20251001")

# ── Rabattcode-Tracker ────────────────────────────────────────────────────────
# Kanal, in dem Rabattcodes gepostet werden (0 = Feature inaktiv).
DISCOUNT_CHANNEL_ID = int(os.getenv("DISCOUNT_CHANNEL_ID", "0"))
# Modell für die Code-Extraktion (günstiges Haiku für strukturierte Parserei).
DISCOUNT_PARSER_MODEL = os.getenv("DISCOUNT_PARSER_MODEL", "claude-haiku-4-5-20251001")
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

# ── KI-Chat-Bot ───────────────────────────────────────────────────────────────
# Modell für den Chat (Standard: claude-haiku-4-5-20251001)
AI_CHAT_MODEL = os.getenv("AI_CHAT_MODEL", "claude-haiku-4-5-20251001")
# Im Modell-Dropdown mit 👍 als Empfehlung markiertes Modell – unabhängig von der
# Vorauswahl (AI_CHAT_MODEL / zuletzt gewählt). Leer = keine Empfehlung anzeigen.
AI_CHAT_RECOMMENDED_MODEL = os.getenv("AI_CHAT_RECOMMENDED_MODEL", "claude-sonnet-5")
# Modell für die Stufe-2-Shop-Relevanz-Klassifikation (günstiges Haiku).
AI_CHAT_CLASSIFY_MODEL = os.getenv("AI_CHAT_CLASSIFY_MODEL", "claude-haiku-4-5-20251001")

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
# Owner-Login-Token (Pflicht wenn aktiviert) + Discord-User-ID für die Owner-DM
# (darf zunächst leer/0 sein -> dann nur Log-Hinweis statt DM).
BOARD_ADMIN_TOKEN = os.getenv("BOARD_ADMIN_TOKEN", "")
BOARD_OWNER_ID    = int(os.getenv("BOARD_OWNER_ID", "0") or "0")
# Eigene, separate DB-Datei (nicht die Haupt-Bot-DB).
BOARD_DB_FILE     = os.getenv("BOARD_DB_FILE", str(BASE_DIR / "board.db"))
# Salt für IP-Hashing (keine Roh-IP gespeichert). In Produktion setzen!
BOARD_HASH_SALT   = os.getenv("BOARD_HASH_SALT", "change-me-board-salt").encode()

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
