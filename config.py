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
