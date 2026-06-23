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
