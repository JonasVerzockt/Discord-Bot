# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jonas Beier

"""
cogs/inat_tracker.py – iNaturalist-Links aus Discord -> Google Sheets

Schreibt pro erkanntem Link eine Zeile:
  Spalte A  -- Discord Username (z.B. jonasverzockt)
  Spalte B  -- Anzeigename auf dem Server (display_name)
  Spalte C  -- (leer -- wird von der Tabelle selbst befüllt)
  Spalte D  -- iNaturalist-Link (immer https)
  Spalte E  -- Datum (Berliner Zeit, DD.MM.YYYY)

Vor dem Eintragen wird geprüft:
  1. Ist der Link bereits in Spalte D vorhanden? -> ignorieren
  2. Gehört die Beobachtung zur Überfamilie Formicoidea (taxon_id=1269340)? -> sonst ignorieren

Alle 5 eingetragenen Beobachtungen wird ein Ranking-Snapshot aus dem
"Übersicht"-Tab (A1:E{letzte Zeile}) als PNG in den Channel gepostet.
Dabei wird gewartet bis Spalte Z2 im Übersicht-Tab leer ist
(Apps Script setzt Z2 als Sperr-Flag während es rechnet).
"""

# ──────────────────────────────────────────────────────────────────────────────
#  KONFIGURATION -- nur hier anpassen
# ──────────────────────────────────────────────────────────────────────────────

INAT_CHANNEL_ID   = 1166491005814579300
INAT_SHEET_ID     = "1aPQKNiVBCscjM6VbBGaBtG8MOvLKVoTw_PYKMp17dtQ"
INAT_WORKSHEET    = "Rohdaten"
INAT_UEBERSICHT   = "Übersicht"   # Tab mit dem Ranking (Snapshot-Quelle)

# Apps Script Web App (optional) – wenn gesetzt, wird das Script nach jedem
# 5. Eintrag manuell getriggert bevor der Snapshot gemacht wird.
# URL aus Apps Script: Bereitstellen → Als Web App → URL kopieren
# Secret muss mit BOT_TRIGGER_SECRET im Apps Script übereinstimmen.
import os as _os
INAT_WEBAPP_URL    = _os.getenv("INAT_WEBAPP_URL", "")    # Web App URL
INAT_WEBAPP_SECRET = _os.getenv("INAT_WEBAPP_SECRET", "") # Secret Token

# Zeitfenster (Berliner Zeit) -- Format: "YYYY-MM-DD HH:MM"
INAT_START = "2026-06-05 00:00"
INAT_END   = "2026-10-30 20:00"

# Nur Beobachtungen die zu dieser Überfamilie gehören werden akzeptiert
INAT_TAXON_ID = 1269340  # Formicoidea

# Ranking-Snapshot: nach wie vielen neuen Einträgen posten?
INAT_SNAPSHOT_EVERY = 5

# Wie lange maximal auf Z2-Freigabe warten (Sekunden)?
INAT_Z2_TIMEOUT = 600  # 10 Minuten

# ──────────────────────────────────────────────────────────────────────────────

import asyncio
import io
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
import gspread
import requests
from discord.ext import commands
from google.oauth2.service_account import Credentials
import google.auth.transport.requests as _ga_req

from config import BASE_DIR
from utils.localization import l10n, get_guild_lang

GOOGLE_CREDS_FILE = str(BASE_DIR / "service_account.json")

logger = logging.getLogger(__name__)

BERLIN  = ZoneInfo("Europe/Berlin")
INAT_RE = re.compile(
    r"https?://(?:www\.)?inaturalist\.org/observations/(\d+)",
    re.IGNORECASE,
)
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class InatTrackerCog(commands.Cog, name="InatTracker"):

    def __init__(self, bot: discord.Bot):
        self.bot              = bot
        self._ws: gspread.Worksheet | None = None
        self._ws_ue: gspread.Worksheet | None = None  # Übersicht-Tab
        self._start = datetime.strptime(INAT_START, "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
        self._end   = datetime.strptime(INAT_END,   "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
        self._last_manual_snapshot: datetime | None = None  # Cooldown für "Rangliste"-Trigger

    # ── Sheet-Verbindungen (lazy) ─────────────────────────────────────────────

    def _sheet(self) -> gspread.Worksheet:
        if self._ws is None:
            creds    = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=_SCOPES)
            gc       = gspread.authorize(creds)
            self._ws = gc.open_by_key(INAT_SHEET_ID).worksheet(INAT_WORKSHEET)
            logger.info(f"🌿 iNat-Sheet verbunden: '{INAT_WORKSHEET}'")
        return self._ws

    def _sheet_uebersicht(self) -> gspread.Worksheet:
        if self._ws_ue is None:
            creds       = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=_SCOPES)
            gc          = gspread.authorize(creds)
            self._ws_ue = gc.open_by_key(INAT_SHEET_ID).worksheet(INAT_UEBERSICHT)
            logger.info(f"📊 iNat Übersicht-Tab verbunden: '{INAT_UEBERSICHT}'")
        return self._ws_ue

    # ── Hilfsmethoden ─────────────────────────────────────────────────────────

    def _in_window(self) -> bool:
        return self._start <= datetime.now(tz=BERLIN) <= self._end

    def _link_exists(self, ws: gspread.Worksheet, url: str) -> bool:
        """Prüft ob die URL bereits in Spalte D vorhanden ist."""
        return url in ws.col_values(4)

    def _next_free_row(self, ws: gspread.Worksheet) -> int:
        """Erste freie Zeile anhand von Spalte A."""
        return len(ws.col_values(1)) + 1

    async def _is_formicoidea(self, obs_id: str) -> bool | None:
        """
        Fragt die iNaturalist API ab und prüft ob die Beobachtung
        zur Überfamilie Formicoidea (taxon_id=1269340) gehört.
        Rückgabe:
          True  -- gehört zu Formicoidea
          False -- gehört nicht dazu (API hat geantwortet)
          None  -- API nicht erreichbar (Retry auslösen)
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
                logger.warning(f"⚠️ iNat API: keine Ergebnisse für obs {obs_id}")
                return False
            taxon = results[0].get("taxon")
            if not taxon:
                logger.warning(f"⚠️ iNat obs {obs_id}: kein Taxon gesetzt (unidentifiziert)")
                return False
            ancestor_ids = taxon.get("ancestor_ids", [])
            taxon_id     = taxon.get("id")
            return INAT_TAXON_ID in ancestor_ids or taxon_id == INAT_TAXON_ID
        except Exception as e:
            logger.error(f"❌ iNat API nicht erreichbar (obs {obs_id}): {e}")
            return None

    def _write_entry(
        self,
        ws: gspread.Worksheet,
        username: str,
        display_name: str,
        inat_url: str,
    ) -> int:
        """Schreibt A, B, D, E -- C bleibt unberührt. Gibt Zeilennummer zurück."""
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

    # ── Ranking-Snapshot ──────────────────────────────────────────────────────

    async def _post_ranking_snapshot(self) -> None:
        """
        Triggert optional das Apps Script, wartet bis Z2 im Übersicht-Tab leer
        ist, exportiert dann A1:E{letzte_Zeile} als PNG und postet es im Channel.
        """
        try:
            ws_ue = await asyncio.to_thread(self._sheet_uebersicht)

            # 0. Warten bis Z2 leer ist (evtl. läuft noch ein anderer Job)
            elapsed = 0
            while elapsed < INAT_Z2_TIMEOUT:
                z2 = await asyncio.to_thread(lambda: ws_ue.acell("Z2").value)
                if not z2:
                    break
                logger.info(
                    f"⏳ Z2='{z2}' (anderer Job aktiv) – warte 10s (bisher {elapsed}s) …"
                )
                await asyncio.sleep(10)
                elapsed += 10
            else:
                logger.warning(
                    f"⚠️ Ranking-Snapshot: Z2 nach {INAT_Z2_TIMEOUT}s noch belegt – abgebrochen"
                )
                return

            # 1. Apps Script triggern (Z2 ist jetzt frei)
            if INAT_WEBAPP_URL and INAT_WEBAPP_SECRET:
                try:
                    logger.info("🔁 Triggere Apps Script via Web App …")
                    resp_wa = await asyncio.to_thread(
                        requests.post,
                        INAT_WEBAPP_URL,
                        json={"secret": INAT_WEBAPP_SECRET},
                        timeout=30,
                    )
                    logger.info(
                        f"📡 Apps Script Antwort: {resp_wa.status_code} – {resp_wa.text[:200]}"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Apps Script Trigger fehlgeschlagen: {e}")
                # 10s warten damit das Script Z2 setzen kann
                await asyncio.sleep(10)

                # 1b. Warten bis der getriggerte Job fertig ist (Z2 wieder leer)
                elapsed = 0
                while elapsed < INAT_Z2_TIMEOUT:
                    z2 = await asyncio.to_thread(lambda: ws_ue.acell("Z2").value)
                    if not z2:
                        break
                    logger.info(
                        f"⏳ Z2='{z2}' (Apps Script aktiv) – "
                        f"warte 10s (bisher {elapsed}s) …"
                    )
                    await asyncio.sleep(10)
                    elapsed += 10
                else:
                    logger.warning(
                        f"⚠️ Ranking-Snapshot: Z2 nach {INAT_Z2_TIMEOUT}s noch belegt – abgebrochen"
                    )
                    return

            # 2. Letzte Zeile mit Inhalt in Spalte A ermitteln
            col_a    = await asyncio.to_thread(lambda: ws_ue.col_values(1))
            last_row = len([v for v in col_a if str(v).strip()])
            if last_row < 1:
                logger.warning("⚠️ Ranking-Snapshot: Übersicht Spalte A leer – abgebrochen")
                return

            # 3. OAuth-Token holen und PNG exportieren
            creds = Credentials.from_service_account_file(
                GOOGLE_CREDS_FILE, scopes=_SCOPES
            )
            auth_req = _ga_req.Request()
            await asyncio.to_thread(creds.refresh, auth_req)

            gid        = ws_ue.id  # numerische Sheet-GID
            export_url = (
                f"https://docs.google.com/spreadsheets/d/{INAT_SHEET_ID}/export"
                f"?format=png&gid={gid}&range=A1:E{last_row}"
            )
            resp = await asyncio.to_thread(
                requests.get,
                export_url,
                headers={"Authorization": f"Bearer {creds.token}"},
                timeout=30,
            )
            resp.raise_for_status()

            if "image" not in resp.headers.get("Content-Type", ""):
                logger.error(
                    f"❌ Ranking-Snapshot: unerwarteter Content-Type "
                    f"'{resp.headers.get('Content-Type')}' – abgebrochen"
                )
                return

            # 4. Bild im Channel posten
            channel = self.bot.get_channel(INAT_CHANNEL_ID)
            if channel is None:
                logger.warning("⚠️ Ranking-Snapshot: Channel nicht gefunden")
                return

            guild_id = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
            lang = await get_guild_lang(self.bot, guild_id) if guild_id else "en"

            buf = io.BytesIO(resp.content)
            buf.seek(0)
            await channel.send(
                l10n.get("inat_ranking_caption", lang),
                file=discord.File(buf, filename="ranking.png"),
            )
            logger.info(f"📊 Ranking-Snapshot gepostet (Übersicht A1:E{last_row})")

        except Exception as e:
            logger.error(f"❌ Ranking-Snapshot fehlgeschlagen: {e}", exc_info=True)

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

        # ── Manueller Ranglisten-Trigger ──────────────────────────────────────
        if message.content.strip() == "Rangliste":
            now = datetime.now(tz=BERLIN)
            if (
                self._last_manual_snapshot is not None
                and (now - self._last_manual_snapshot).total_seconds() < 60
            ):
                await message.add_reaction("⏱️")
            else:
                self._last_manual_snapshot = now
                asyncio.create_task(self._post_ranking_snapshot())
                await message.add_reaction("📊")
            return

        if not obs_ids:
            return

        try:
            ws = await asyncio.to_thread(self._sheet)
        except Exception as e:
            logger.error(f"❌ iNat-Sheet Verbindungsfehler: {e}")
            return

        username     = message.author.name
        display_name = message.author.display_name
        written      = 0
        last_row     = None

        for obs_id in obs_ids:
            url = f"https://www.inaturalist.org/observations/{obs_id}"

            # ---- Check 1: bereits im Sheet? ---------------------------------
            exists = await asyncio.to_thread(self._link_exists, ws, url)
            if exists:
                logger.info(
                    f"ℹ️ iNat obs {obs_id} bereits im Sheet – übersprungen "
                    f"(user={username}, {display_name})"
                )
                continue

            # ---- Check 2: gehört zu Formicoidea? ---------------------------
            is_ant = await self._is_formicoidea(obs_id)
            if is_ant is None:
                await message.add_reaction("⏳")
                asyncio.create_task(
                    self._retry_pending(message, obs_id, ws, username, display_name)
                )
                continue
            if not is_ant:
                logger.info(
                    f"iNat obs {obs_id} gehört nicht zu Formicoidea "
                    f"(taxon_id={INAT_TAXON_ID}) – übersprungen "
                    f"(user={username}, {display_name})"
                )
                continue

            # ---- Eintragen --------------------------------------------------
            try:
                row = await asyncio.to_thread(
                    self._write_entry, ws, username, display_name, url
                )
                logger.info(
                    f"✅ iNat Zeile {row}: user={username} ({display_name})  {url}"
                )
                written  += 1
                last_row  = row
            except Exception as e:
                logger.error(f"❌ iNat Schreibfehler (obs {obs_id}): {e}")
                self._ws = None

        if written:
            try:
                await message.add_reaction("✅")
            except discord.HTTPException as e:
                logger.error(f"❌ Reaktion fehlgeschlagen: {e}")

            # Ranking-Snapshot alle INAT_SNAPSHOT_EVERY Einträge
            # last_row - 1 = Daten-Zeilennummer (Zeile 1 = Header)
            if last_row is not None:
                data_count = last_row - 1
                if data_count > 0 and data_count % INAT_SNAPSHOT_EVERY == 0:
                    logger.info(
                        f"📊 Ranking-Snapshot ausgelöst nach {data_count} Einträgen"
                    )
                    asyncio.create_task(self._post_ranking_snapshot())

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
            await asyncio.sleep(300)
            logger.info(f"🔄 iNat Retry #{attempt} für obs {obs_id}")
            is_ant = await self._is_formicoidea(obs_id)
            if is_ant is None:
                logger.warning(
                    f"⚠️ iNat API noch nicht erreichbar (obs {obs_id}, Versuch {attempt})"
                )
                continue
            try:
                await message.remove_reaction("⏳", self.bot.user)
            except Exception:
                pass
            if not is_ant:
                logger.info(
                    f"🌿 iNat obs {obs_id} gehört nicht zu Formicoidea – übersprungen"
                )
                return
            if await asyncio.to_thread(self._link_exists, ws, url):
                logger.info(f"ℹ️ iNat obs {obs_id} inzwischen bereits im Sheet")
                return
            try:
                row = await asyncio.to_thread(
                    self._write_entry, ws, username, display_name, url
                )
                logger.info(f"✅ iNat Retry OK: Zeile {row}  {url}")
                await message.add_reaction("✅")
                # Snapshot-Check auch beim Retry
                data_count = row - 1
                if data_count > 0 and data_count % INAT_SNAPSHOT_EVERY == 0:
                    asyncio.create_task(self._post_ranking_snapshot())
            except Exception as e:
                logger.error(f"❌ iNat Retry Schreibfehler (obs {obs_id}): {e}")
            return


def setup(bot: discord.Bot):
    bot.add_cog(InatTrackerCog(bot))
