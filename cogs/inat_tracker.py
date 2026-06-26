# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jonas Beier

"""
cogs/inat_tracker.py – iNaturalist-Links aus Discord → Google Sheets

Schreibt pro erkanntem Link eine Zeile:
  Spalte A  – Discord User ID
  Spalte B  – Anzeigename auf dem Server (display_name)
  Spalte C  – (leer – wird von der Tabelle selbst befüllt)
  Spalte D  – iNaturalist-Link (immer https)
  Spalte E  – Datum (Berliner Zeit, DD.MM.YYYY)

Reagiert mit ✅ wenn mindestens ein Link eingetragen wurde.
Links werden nur im konfigurierten Zeitfenster erkannt.
"""

# ══════════════════════════════════════════════════════════════════════════════
#  KONFIGURATION – nur hier anpassen
# ══════════════════════════════════════════════════════════════════════════════

INAT_CHANNEL_ID    = 123456789012345678      # Kanal-ID der zu überwachenden iNat-Channel
INAT_SHEET_ID      = "DEINE_GOOGLE_SHEET_ID" # aus der Sheet-URL: /d/XXXXXXX/edit
INAT_WORKSHEET     = "Tabelle1"              # Name des Tabellenblatts / Tabs

# Zeitfenster (Berliner Zeit) – Format: "YYYY-MM-DD HH:MM"
INAT_START = "2026-06-26 18:00"
INAT_END   = "2026-06-28 22:00"

# ══════════════════════════════════════════════════════════════════════════════

import re
import logging
from datetime import datetime

import discord
import gspread
import pytz
from discord.ext import commands
from google.oauth2.service_account import Credentials

from config import BASE_DIR

GOOGLE_CREDS_FILE = str(BASE_DIR / "service_account.json")

logger = logging.getLogger(__name__)

BERLIN   = pytz.timezone("Europe/Berlin")
INAT_RE  = re.compile(
    r"https?://www\.inaturalist\.org/observations/(\d+)",
    re.IGNORECASE,
)
_SCOPES  = ["https://www.googleapis.com/auth/spreadsheets"]


class InatTrackerCog(commands.Cog, name="InatTracker"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._ws: gspread.Worksheet | None = None
        self._start = BERLIN.localize(datetime.strptime(INAT_START, "%Y-%m-%d %H:%M"))
        self._end   = BERLIN.localize(datetime.strptime(INAT_END,   "%Y-%m-%d %H:%M"))

    # ── Sheet-Verbindung (lazy, damit Bot-Start nicht blockiert) ──────────────

    def _sheet(self) -> gspread.Worksheet:
        if self._ws is None:
            creds     = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=_SCOPES)
            gc        = gspread.authorize(creds)
            self._ws  = gc.open_by_key(INAT_SHEET_ID).worksheet(INAT_WORKSHEET)
            logger.info(f"📊 iNat-Sheet verbunden: '{INAT_WORKSHEET}'")
        return self._ws

    # ── Hilfsmethoden ─────────────────────────────────────────────────────────

    def _in_window(self) -> bool:
        return self._start <= datetime.now(BERLIN) <= self._end

    def _next_free_row(self, ws: gspread.Worksheet) -> int:
        """Erste freie Zeile anhand von Spalte A."""
        return len(ws.col_values(1)) + 1

    def _write_entry(
        self,
        ws: gspread.Worksheet,
        discord_id: int,
        display_name: str,
        inat_url: str,
    ) -> int:
        """Schreibt A, B, D, E – C bleibt unberührt."""
        row      = self._next_free_row(ws)
        date_str = datetime.now(BERLIN).strftime("%d.%m.%Y")
        ws.batch_update([
            {"range": f"A{row}", "values": [[str(discord_id)]]},
            {"range": f"B{row}", "values": [[display_name]]},
            {"range": f"D{row}", "values": [[inat_url]]},
            {"range": f"E{row}", "values": [[date_str]]},
        ])
        return row

    # ── Event ─────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.channel.id != INAT_CHANNEL_ID:
            return
        if not self._in_window():
            return

        obs_ids = INAT_RE.findall(message.content)
        if not obs_ids:
            return

        try:
            ws = self._sheet()
        except Exception as e:
            logger.error(f"iNat-Sheet Verbindungsfehler: {e}")
            return

        discord_id   = message.author.id
        display_name = message.author.display_name
        written      = 0

        for obs_id in obs_ids:
            url = f"https://www.inaturalist.org/observations/{obs_id}"
            try:
                row = self._write_entry(ws, discord_id, display_name, url)
                logger.info(
                    f"📋 iNat Zeile {row}: user={discord_id} ({display_name})  {url}"
                )
                written += 1
            except gspread.exceptions.APIError as e:
                logger.error(f"Sheets-API Fehler (obs {obs_id}): {e}")
                self._ws = None   # Verbindung beim nächsten Aufruf neu aufbauen
            except Exception as e:
                logger.error(f"iNat Schreibfehler (obs {obs_id}): {e}")

        if written:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException as e:
                logger.error(f"Reaktion fehlgeschlagen: {e}")


def setup(bot: discord.Bot):
    bot.add_cog(InatTrackerCog(bot))
