# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jonas Beier

"""
cogs/inat_tracker.py – iNaturalist-Links aus Discord -> Google Sheets

Schreibt pro erkanntem Link eine Zeile:
  Spalte A  -- Discord Username (z.B. jonasverzockt)
  Spalte B  -- Anzeigename auf dem Server (display_name)
  Spalte C  -- (leer -- wird von der Tabelle selbst befuellt)
  Spalte D  -- iNaturalist-Link (immer https)
  Spalte E  -- Datum (Berliner Zeit, DD.MM.YYYY)

Vor dem Eintragen wird geprueft:
  1. Ist der Link bereits in Spalte D vorhanden? -> ignorieren
  2. Gehoert die Beobachtung zur Ueberfamilie Formicoidea (taxon_id=1269340)? -> sonst ignorieren
  Beides wird im Log angezeigt.

Reagiert mit Haekchen wenn mindestens ein Link eingetragen wurde.
Bei API-Fehler: Sanduhr-Reaktion + alle 5 Minuten erneut versuchen.
Links werden nur im konfigurierten Zeitfenster erkannt.
"""

# ==============================================================================
#  KONFIGURATION -- nur hier anpassen
# ==============================================================================

INAT_CHANNEL_ID = 1166491005814579300
INAT_SHEET_ID   = "1aPQKNiVBCscjM6VbBGaBtG8MOvLKVoTw_PYKMp17dtQ"
INAT_WORKSHEET  = "Rohdaten"

# Zeitfenster (Berliner Zeit) -- Format: "YYYY-MM-DD HH:MM"
INAT_START = "2026-06-05 00:00"
INAT_END   = "2026-10-30 20:00"

# Nur Beobachtungen die zu dieser Ueberfamilie gehoeren werden akzeptiert
INAT_TAXON_ID = 1269340  # Formicoidea

# ==============================================================================

import asyncio
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import gspread
import requests
from discord.ext import commands
from google.oauth2.service_account import Credentials

from config import BASE_DIR

GOOGLE_CREDS_FILE = str(BASE_DIR / "service_account.json")

logger = logging.getLogger(__name__)

BERLIN  = ZoneInfo("Europe/Berlin")
INAT_RE = re.compile(
    r"https?://www\.inaturalist\.org/observations/(\d+)",
    re.IGNORECASE,
)
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class InatTrackerCog(commands.Cog, name="InatTracker"):

    def __init__(self, bot: discord.Bot):
        self.bot   = bot
        self._ws: gspread.Worksheet | None = None
        self._start = datetime.strptime(INAT_START, "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
        self._end   = datetime.strptime(INAT_END,   "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)

    # ---- Sheet-Verbindung (lazy, damit Bot-Start nicht blockiert) ------------

    def _sheet(self) -> gspread.Worksheet:
        if self._ws is None:
            creds    = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=_SCOPES)
            gc       = gspread.authorize(creds)
            self._ws = gc.open_by_key(INAT_SHEET_ID).worksheet(INAT_WORKSHEET)
            logger.info(f"iNat-Sheet verbunden: '{INAT_WORKSHEET}'")
        return self._ws

    # ---- Hilfsmethoden -------------------------------------------------------

    def _in_window(self) -> bool:
        return self._start <= datetime.now(tz=BERLIN) <= self._end

    def _link_exists(self, ws: gspread.Worksheet, url: str) -> bool:
        """Prueft ob die URL bereits in Spalte D vorhanden ist."""
        return url in ws.col_values(4)

    def _next_free_row(self, ws: gspread.Worksheet) -> int:
        """Erste freie Zeile anhand von Spalte A."""
        return len(ws.col_values(1)) + 1

    async def _is_formicoidea(self, obs_id: str) -> bool | None:
        """
        Fragt die iNaturalist API ab und prueft ob die Beobachtung
        zur Ueberfamilie Formicoidea (taxon_id=1269340) gehoert.
        Rueckgabe:
          True  -- gehoert zu Formicoidea
          False -- gehoert nicht dazu (API hat geantwortet)
          None  -- API nicht erreichbar (Retry ausloesen)
        """
        url = f"https://api.inaturalist.org/v1/observations/{obs_id}"
        try:
            resp = await asyncio.to_thread(
                requests.get, url,
                headers={"Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            data    = resp.json()
            results = data.get("results", [])
            if not results:
                logger.warning(f"iNat API: keine Ergebnisse fuer obs {obs_id}")
                return False
            taxon = results[0].get("taxon")
            if not taxon:
                logger.warning(f"iNat obs {obs_id}: kein Taxon gesetzt (unidentifiziert)")
                return False
            ancestor_ids = taxon.get("ancestor_ids", [])
            taxon_id     = taxon.get("id")
            return INAT_TAXON_ID in ancestor_ids or taxon_id == INAT_TAXON_ID
        except Exception as e:
            logger.error(f"iNat API nicht erreichbar (obs {obs_id}): {e}")
            return None

    def _write_entry(
        self,
        ws: gspread.Worksheet,
        username: str,
        display_name: str,
        inat_url: str,
    ) -> int:
        """Schreibt A, B, D, E -- C bleibt unberuehrt."""
        row      = self._next_free_row(ws)
        date_str = datetime.now(tz=BERLIN).strftime("%d.%m.%Y")
        ws.batch_update(
            [
                {"range": f"A{row}", "values": [[username]]},
                {"range": f"B{row}", "values": [[display_name]]},
                {"range": f"D{row}", "values": [[inat_url]]},
                {"range": f"E{row}", "values": [[date_str]]},
            ],
            value_input_option="USER_ENTERED",
        )
        return row

    # ---- Event ---------------------------------------------------------------

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

        username     = message.author.name
        display_name = message.author.display_name
        written      = 0

        for obs_id in obs_ids:
            url = f"https://www.inaturalist.org/observations/{obs_id}"

            # ---- Check 1: bereits im Sheet? ---------------------------------
            if self._link_exists(ws, url):
                logger.info(
                    f"iNat obs {obs_id} bereits im Sheet -- uebersprungen "
                    f"(user={username}, {display_name})"
                )
                continue

            # ---- Check 2: gehoert zu Formicoidea? ---------------------------
            is_ant = await self._is_formicoidea(obs_id)
            if is_ant is None:
                # API nicht erreichbar -- Retry-Logik startet im Hintergrund
                await message.add_reaction("⏳")
                asyncio.create_task(
                    self._retry_pending(message, obs_id, ws, username, display_name)
                )
                continue
            if not is_ant:
                logger.info(
                    f"iNat obs {obs_id} gehoert nicht zu Formicoidea "
                    f"(taxon_id={INAT_TAXON_ID}) -- uebersprungen "
                    f"(user={username}, {display_name})"
                )
                continue

            # ---- Eintragen --------------------------------------------------
            try:
                row = self._write_entry(ws, username, display_name, url)
                logger.info(
                    f"iNat Zeile {row}: user={username} ({display_name})  {url}"
                )
                written += 1
            except Exception as e:
                logger.error(f"iNat Schreibfehler (obs {obs_id}): {e}")
                self._ws = None

        if written:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException as e:
                logger.error(f"Reaktion fehlgeschlagen: {e}")

    async def _retry_pending(
        self,
        message: discord.Message,
        obs_id: str,
        ws: gspread.Worksheet,
        username: str,
        display_name: str,
    ) -> None:
        """Wiederholt den API-Check alle 5 Minuten bis die API antwortet."""
        url     = f"https://www.inaturalist.org/observations/{obs_id}"
        attempt = 0
        while True:
            attempt += 1
            await asyncio.sleep(300)  # 5 Minuten warten
            logger.info(f"iNat Retry #{attempt} fuer obs {obs_id}")
            is_ant = await self._is_formicoidea(obs_id)
            if is_ant is None:
                logger.warning(
                    f"iNat API noch nicht erreichbar (obs {obs_id}, Versuch {attempt})"
                )
                continue  # nochmal warten
            # API hat geantwortet -- Sanduhr entfernen
            try:
                await message.remove_reaction("⏳", self.bot.user)
            except Exception:
                pass
            if not is_ant:
                logger.info(
                    f"iNat obs {obs_id} gehoert nicht zu Formicoidea -- uebersprungen"
                )
                return
            # Nochmals pruefen ob der Link zwischenzeitlich eingetragen wurde
            if self._link_exists(ws, url):
                logger.info(f"iNat obs {obs_id} inzwischen bereits im Sheet")
                return
            try:
                row = self._write_entry(ws, username, display_name, url)
                logger.info(f"iNat Retry OK: Zeile {row}  {url}")
                await message.add_reaction("✅")
            except Exception as e:
                logger.error(f"iNat Retry Schreibfehler (obs {obs_id}): {e}")
            return


def setup(bot: discord.Bot):
    bot.add_cog(InatTrackerCog(bot))
