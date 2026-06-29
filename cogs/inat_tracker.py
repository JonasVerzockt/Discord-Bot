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

Alle 5 eingetragenen Beobachtungen wird ein Ranking-Bild aus dem "Übersicht"-Tab
(Spalten A=Rang, B=Name, C=Anzahl Arten) als Treppchen-Grafik (lokal mit
matplotlib gerendert) im Channel gepostet. Dabei wird gewartet bis Spalte Z2 im
Übersicht-Tab leer ist (Apps Script setzt Z2 als Sperr-Flag während es rechnet).
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
INAT_SNAPSHOT_EVERY = 15

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


# ── Ranking-Daten + Bild-Rendering ────────────────────────────────────────────

def _parse_ranking(rows: list) -> list:
    """
    rows = A1:C inkl. Kopfzeile (A=Rang, B=Name, C=Anzahl Arten).
    Gibt [(rang, name, anzahl), ...] zurück, sortiert nach Anzahl absteigend.
    """
    out = []
    for r in rows[1:]:                       # Kopfzeile überspringen
        name = (str(r[1]).strip() if len(r) > 1 else "")
        if not name:
            continue
        try:
            anzahl = int(str(r[2]).strip()) if len(r) > 2 and str(r[2]).strip() else 0
        except ValueError:
            anzahl = 0
        try:
            rang = int(str(r[0]).strip()) if len(r) > 0 and str(r[0]).strip() else None
        except ValueError:
            rang = None
        out.append((rang, name, anzahl))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _render_ranking_png(entries: list) -> bytes:
    """
    Rendert die Rangliste lokal als PNG: die obersten drei Ränge als farbiges
    Treppchen (Gold/Silber/Bronze), Platz 4+ als Tabelle. Personen mit gleicher
    Artenzahl teilen sich denselben Rang und dieselbe Treppchen-Stufe
    (Competition-Ranking: 1, 1, 3, …). Gibt PNG-Bytes zurück.
    Läuft blockierend → vom Aufrufer in asyncio.to_thread() aufrufen.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    BG, FG = "#0d1117", "#e6edf3"
    GOLD, SILVER, BRONZE = "#FFD700", "#C0C0C0", "#CD7F32"
    STEP_COLORS = [GOLD, SILVER, BRONZE]

    def _clip(s, n):
        s = str(s)
        return s if len(s) <= n else s[: n - 1] + "…"

    # Nach Artenzahl gruppieren → geteilte Ränge (gleiche Anzahl = gleicher Rang)
    places = []          # (rang, anzahl, [namen])
    i, rank = 0, 1
    while i < len(entries):
        val = entries[i][2]
        names = []
        j = i
        while j < len(entries) and entries[j][2] == val:
            names.append(entries[j][1])
            j += 1
        places.append((rank, val, names))
        rank += (j - i)
        i = j

    podium    = places[:3]
    rest      = places[3:]
    rest_rows = [(rg, nm, val) for (rg, val, names) in rest for nm in names]

    fig_h = 5.6 + 0.42 * len(rest_rows)
    fig = plt.figure(figsize=(8, fig_h), dpi=150)
    fig.patch.set_facecolor(BG)

    if rest_rows:
        gs = fig.add_gridspec(2, 1, height_ratios=[3.4, 0.8 + 0.42 * len(rest_rows)], hspace=0.18)
        ax  = fig.add_subplot(gs[0])
        axt = fig.add_subplot(gs[1])
    else:
        ax  = fig.add_subplot(111)
        axt = None

    ax.set_facecolor(BG)
    fig.suptitle("Arten-Rangliste", color=FG, fontsize=22, fontweight="bold", y=0.97)

    slot_for_step = {0: 1, 1: 0, 2: 2}        # Stufe -> x-Position (2. links, 1. Mitte, 3. rechts)
    step_heights  = {0: 3.0, 1: 2.0, 2: 1.0}  # gleichmäßiges Treppchen; echte Werte stehen als Label
    maxh = 3.0

    for step_idx, (rg, val, names) in enumerate(podium):
        xpos = slot_for_step.get(step_idx, step_idx)
        col  = STEP_COLORS[step_idx]
        h    = step_heights.get(step_idx, 1.0)
        ax.bar(xpos, h, width=0.72, color=col, edgecolor="white", linewidth=1.4, zorder=3)
        ax.text(xpos, h / 2, str(rg),
                ha="center", va="center", color=BG, fontsize=30, fontweight="bold", zorder=4)
        ax.text(xpos, h + maxh * 0.04, f"{val}", ha="center", va="bottom",
                color=FG, fontsize=14, fontweight="bold")
        shown = [_clip(n, 16) for n in names[:5]]
        if len(names) > 5:
            shown.append(f"+{len(names) - 5} weitere")
        ax.text(xpos, -maxh * 0.06, "\n".join(shown), ha="center", va="top",
                color=col, fontsize=11, fontweight="bold", linespacing=1.35)

    ax.set_xlim(-0.7, 2.7)
    ax.set_ylim(-maxh * 0.34, maxh * 1.22)
    ax.axis("off")

    if axt is not None:
        axt.axis("off")
        cell_text = [[str(rg), _clip(nm, 30), str(val)] for (rg, nm, val) in rest_rows]
        tbl = axt.table(
            cellText=cell_text,
            colLabels=["Platz", "Name", "Arten"],
            cellLoc="center", loc="center",
            colWidths=[0.18, 0.62, 0.20],
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(11)
        tbl.scale(1, 1.45)
        for (row, col), cell in tbl.get_celld().items():
            cell.set_edgecolor("#30363d")
            if row == 0:
                cell.set_facecolor("#1f6feb")
                cell.set_text_props(color="white", fontweight="bold")
            else:
                cell.set_facecolor("#161b22" if row % 2 else "#0d1117")
                cell.set_text_props(color=FG)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


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

    async def _wait_unlocked(self, ws_ue, settle: int = 2, interval: int = 5) -> bool:
        """
        Wartet bis das Sperr-Flag Z2 STABIL leer ist (mehrmals hintereinander
        leer). Verhindert, dass mitten in einer laufenden Validierung
        (Apps Script setzt Z2='block') gelesen oder gerendert wird.
        Gibt True zurück wenn frei, False bei Timeout.
        """
        elapsed = 0
        empty_streak = 0
        while elapsed < INAT_Z2_TIMEOUT:
            z2 = await asyncio.to_thread(lambda: ws_ue.acell("Z2").value)
            if z2:
                empty_streak = 0
                logger.info(f"⏳ Z2='{z2}' (Validierung läuft) – warte {interval}s …")
            else:
                empty_streak += 1
                if empty_streak >= settle:
                    return True
            await asyncio.sleep(interval)
            elapsed += interval
        logger.warning(
            f"⚠️ Ranking-Snapshot: Z2 nach {INAT_Z2_TIMEOUT}s nicht stabil frei – abgebrochen"
        )
        return False

    async def _post_ranking_snapshot(self) -> None:
        """
        Triggert optional das Apps Script, wartet bis Z2 im Übersicht-Tab leer ist,
        liest dann A=Rang/B=Name/C=Anzahl und rendert daraus lokal ein Treppchen-PNG.
        """
        try:
            ws_ue = await asyncio.to_thread(self._sheet_uebersicht)

            # 0. Sicherstellen, dass gerade KEINE Validierung läuft (Z2 stabil leer)
            if not await self._wait_unlocked(ws_ue):
                return

            # 1. Apps Script triggern (führt die Validierung aus, setzt/leert Z2 selbst)
            if INAT_WEBAPP_URL and INAT_WEBAPP_SECRET:
                try:
                    logger.info("🔁 Triggere Apps Script via Web App …")
                    resp_wa = await asyncio.to_thread(
                        requests.post,
                        INAT_WEBAPP_URL,
                        json={"secret": INAT_WEBAPP_SECRET},
                        timeout=120,
                    )
                    logger.info(
                        f"📡 Apps Script Antwort: {resp_wa.status_code} – {resp_wa.text[:200]}"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Apps Script Trigger fehlgeschlagen: {e}")

                # Kurz warten, damit das Script Z2 sicher gesetzt hat, dann warten bis
                # die Validierung KOMPLETT fertig ist (Z2 stabil leer). So wird
                # garantiert nie während der laufenden Prüfung gerendert.
                await asyncio.sleep(5)
                if not await self._wait_unlocked(ws_ue):
                    return

            # 2. Letzte Zeile mit Inhalt in Spalte A ermitteln
            col_a    = await asyncio.to_thread(lambda: ws_ue.col_values(1))
            last_row = len([v for v in col_a if str(v).strip()])
            if last_row < 2:
                logger.warning("⚠️ Ranking-Snapshot: Übersicht (A) leer – abgebrochen")
                return

            # 3. Ranking-Daten lesen (A=Rang, B=Name, C=Anzahl; Zeile 1 = Kopf)
            data    = await asyncio.to_thread(lambda: ws_ue.get(f"A1:C{last_row}"))
            entries = _parse_ranking(data)
            if not entries:
                logger.warning("⚠️ Ranking-Snapshot: keine Einträge – Text-Fallback")
                await self._post_ranking_text_fallback(ws_ue, last_row)
                return

            channel = self.bot.get_channel(INAT_CHANNEL_ID)
            if channel is None:
                logger.warning("⚠️ Ranking-Snapshot: Channel nicht gefunden")
                return
            guild_id = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
            lang = await get_guild_lang(self.bot, guild_id) if guild_id else "en"

            # 4. PNG lokal rendern (kein flaky Google-Export) und posten
            try:
                png = await asyncio.to_thread(_render_ranking_png, entries)
            except Exception as e:
                logger.error(f"❌ PNG-Render fehlgeschlagen: {e} – Text-Fallback", exc_info=True)
                await self._post_ranking_text_fallback(ws_ue, last_row)
                return

            buf = io.BytesIO(png)
            buf.seek(0)
            try:
                await channel.send(
                    l10n.get("inat_ranking_caption", lang),
                    file=discord.File(buf, filename="ranking.png"),
                )
                logger.info(f"📊 Ranking-Snapshot gepostet ({len(entries)} Einträge, lokal gerendert)")
            except discord.Forbidden:
                logger.error(
                    "⚠️ Keine Berechtigung zum Posten im iNat-Kanal – dem Bot fehlt "
                    "'Nachrichten senden' und/oder 'Dateien anhängen' in diesem Kanal."
                )
            except discord.HTTPException as e:
                logger.error(f"❌ Ranking-Snapshot Sende-Fehler: {e}")

        except Exception as e:
            logger.error(f"❌ Ranking-Snapshot fehlgeschlagen: {e}", exc_info=True)

    async def _post_ranking_text_fallback(self, ws_ue, last_row: int) -> None:
        """
        Fallback wenn das PNG-Rendering scheitert: liest A1:C{last_row} aus dem
        Übersicht-Tab und postet das Ranking als Text-Tabelle (bzw. als .txt,
        falls zu lang für eine Discord-Nachricht).
        """
        try:
            values = await asyncio.to_thread(lambda: ws_ue.get(f"A1:C{last_row}"))
        except Exception as e:
            logger.error(f"❌ Text-Fallback: Sheet-Lesefehler: {e}")
            return

        channel = self.bot.get_channel(INAT_CHANNEL_ID)
        if channel is None:
            logger.warning("⚠️ Text-Fallback: Channel nicht gefunden")
            return
        guild_id = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
        lang = await get_guild_lang(self.bot, guild_id) if guild_id else "en"

        ncols = max((len(r) for r in values), default=0)
        widths = [0] * ncols
        for r in values:
            for i, c in enumerate(r):
                widths[i] = max(widths[i], len(str(c)))

        def _row(r):
            cells = [str(r[i]) if i < len(r) else "" for i in range(ncols)]
            return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

        table   = "\n".join(_row(r) for r in values)
        caption = l10n.get("inat_ranking_fallback", lang)

        body = f"{caption}\n```\n{table}\n```"
        try:
            if len(body) <= 2000:
                await channel.send(body)
            else:
                buf = io.BytesIO(table.encode("utf-8"))
                buf.seek(0)
                await channel.send(caption, file=discord.File(buf, filename="ranking.txt"))
            logger.info(f"📊 Ranking-Text-Fallback gepostet (Übersicht A1:C{last_row})")
        except discord.Forbidden:
            logger.error(
                "⚠️ Keine Berechtigung zum Posten im iNat-Kanal "
                "(Nachrichten senden / Dateien anhängen)."
            )
        except discord.HTTPException as e:
            logger.error(f"❌ Text-Fallback Sende-Fehler: {e}")

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

            exists = await asyncio.to_thread(self._link_exists, ws, url)
            if exists:
                logger.info(
                    f"ℹ️ iNat obs {obs_id} bereits im Sheet – übersprungen "
                    f"(user={username}, {display_name})"
                )
                continue

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
                data_count = row - 1
                if data_count > 0 and data_count % INAT_SNAPSHOT_EVERY == 0:
                    asyncio.create_task(self._post_ranking_snapshot())
            except Exception as e:
                logger.error(f"❌ iNat Retry Schreibfehler (obs {obs_id}): {e}")
            return


def setup(bot: discord.Bot):
    bot.add_cog(InatTrackerCog(bot))
