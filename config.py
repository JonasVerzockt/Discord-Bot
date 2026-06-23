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
config.py - Zentrale Konfiguration fuer den AAM Discord Bot.
Alle Konstanten und Umgebungsvariablen werden hier geladen.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# Discord
DISCORD_TOKEN     = os.getenv("DISCORD_TOKEN")
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0"))
BOT_OWNER         = int(os.getenv("BOT_OWNER_ID", "0"))
# Server-IDs die Admin-Befehle nutzen duerfen (kommagetrennt in .env)
SERVER_IDS = [
    int(x) for x in os.getenv("SERVER_IDS", "").split(",") if x.strip()
]

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
SCAN_DAYS       = 90
FUZZY_THRESHOLD = 80

# Anthropic (Review-Bot + KI-Chat)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ── KI-Chat-Bot ───────────────────────────────────────────────────────────────
# Modell fuer den Chat (Standard: claude-haiku-4-5-20251001)
AI_CHAT_MODEL = os.getenv("AI_CHAT_MODEL", "claude-haiku-4-5-20251001")

# Kanal-ID in dem der Bot auf ALLE Nachrichten reagiert (eine ID).
# Zusaetzlich reagiert der Bot immer auf @-Erwaehnung in jedem Kanal.
# Leer lassen = nur @-Erwaehnung funktioniert.
AI_CHAT_CHANNEL_IDS: list[int] = [
    int(x) for x in os.getenv("AI_CHAT_CHANNEL_IDS", "").split(",") if x.strip()
]

# Budget in USD (Reset taeglich 00:00 UTC)
AI_CHAT_DAILY_BUDGET_USD      = float(os.getenv("AI_CHAT_DAILY_BUDGET_USD",      "0.50"))
AI_CHAT_USER_DAILY_BUDGET_USD = float(os.getenv("AI_CHAT_USER_DAILY_BUDGET_USD", "0.10"))

# Limits pro Anfrage (Schutz vor teuren Requests)
AI_CHAT_MAX_INPUT_CHARS   = int(os.getenv("AI_CHAT_MAX_INPUT_CHARS",   "1500"))
AI_CHAT_MAX_OUTPUT_TOKENS = int(os.getenv("AI_CHAT_MAX_OUTPUT_TOKENS", "800"))
AI_CHAT_MAX_HISTORY_TURNS = int(os.getenv("AI_CHAT_MAX_HISTORY_TURNS", "10"))

# Wie lange wird eine Konversation gespeichert (Stunden)
AI_CHAT_CONVERSATION_TTL_HOURS = int(os.getenv("AI_CHAT_CONVERSATION_TTL_HOURS", "24"))

# System-Prompt: zuerst Datei, dann Env-Variable, dann eingebauter Standard
_SYSTEM_PROMPT_FILE = BASE_DIR / "ai_chat_system_prompt.txt"
if _SYSTEM_PROMPT_FILE.exists():
    AI_CHAT_SYSTEM_PROMPT: str = _SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
else:
    AI_CHAT_SYSTEM_PROMPT = os.getenv(
        "AI_CHAT_SYSTEM_PROMPT",
        (
            "Du bist ein hilfreicher Assistent der AAM (Ameisen an die Macht) "
            "Discord-Community – einer Gemeinschaft rund um Ameisenhaltung und "
            "Myrmekologie. Beantworte Fragen freundlich, praegnant und auf Deutsch. "
            "Bei Fragen zu Ameisen, Haltung oder Zucht antworte fachkundig. "
            "Halte Antworten kurz und klar."
        ),
    )
