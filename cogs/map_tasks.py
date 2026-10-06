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
cogs/map_tasks.py – Hintergrund-Tasks der Halter-Karte.

- Sofort-Löschung bei Server-Austritt (on_member_remove).
- Täglicher Abgleich: Einträge von Nicht-mehr-Mitgliedern entfernen.
- Jährliche Bestätigung (voller Zweit-Zyklus): nach MAP_REMIND_MONTHS erinnern (PN mit
  Buttons), nach MAP_DELETE_MONTHS ohne Reaktion löschen + informieren.
- Event-Cleanup: vergangene Einmal-Events (ohne RRULE) aufräumen.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from config import (MAP_ENABLED, MAP_GUILD_ID, MAP_REMIND_MONTHS, MAP_DELETE_MONTHS,
                    MAP_GEODATA_AUTO, MAP_GEODATA_REFRESH_DAYS)
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils import geo, map_geodata
from cogs.map import MapConfirmView, _delete_entry

logger = logging.getLogger(__name__)

_MONTH_DAYS = 30.44


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("T", " "))
    except ValueError:
        return None


class MapTasksCog(commands.Cog, name="MapTasks"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.daily_maintenance.start()
        self.event_cleanup.start()
        if MAP_GEODATA_AUTO:
            # Intervall in Stunden (Standard 30 Tage). Erste Ausführung läuft beim Start.
            self.geodata_refresh.change_interval(hours=max(1, 24 * MAP_GEODATA_REFRESH_DAYS))
            self.geodata_refresh.start()

    def cog_unload(self):
        self.daily_maintenance.cancel()
        self.event_cleanup.cancel()
        if MAP_GEODATA_AUTO:
            self.geodata_refresh.cancel()

    # ── Sofort-Löschung bei Austritt ──────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if MAP_GUILD_ID and member.guild and member.guild.id != MAP_GUILD_ID:
            return
        try:
            await _delete_entry(self.bot, member.id)
        except Exception as e:
            logger.debug("map on_member_remove delete: %s", e)

    # ── Täglicher Abgleich + Jahres-Bestätigung ───────────────────────────────
    @tasks.loop(hours=24)
    async def daily_maintenance(self):
        rows = await execute_db(self.bot,
            "SELECT user_id, consent_at, last_confirmed_at, reminder_sent_at "
            "FROM map_entries", fetch=True) or []
        if not rows:
            return
        guild = self.bot.get_guild(MAP_GUILD_ID) if MAP_GUILD_ID else None
        now = datetime.utcnow()
        remind_cut = now - timedelta(days=MAP_REMIND_MONTHS * _MONTH_DAYS)
        delete_cut = now - timedelta(days=MAP_DELETE_MONTHS * _MONTH_DAYS)
        pruned = reminded = expired = 0

        for r in rows:
            uid = r["user_id"]
            # 1) Mitgliedschaft prüfen (nur wenn Guild erreichbar und sicher kein Mitglied)
            if guild is not None:
                m = guild.get_member(int(uid))
                if m is None:
                    try:
                        m = await guild.fetch_member(int(uid))
                    except discord.NotFound:
                        await _delete_entry(self.bot, uid); pruned += 1
                        continue
                    except Exception:
                        pass  # unsicher -> diesen Zyklus überspringen

            # 2) Jahres-Bestätigung (voller Zweit-Zyklus)
            lc = _parse(r["last_confirmed_at"]) or _parse(r["consent_at"]) or now
            if lc < delete_cut:
                try:
                    user = await self.bot.fetch_user(int(uid))
                    lang = await get_user_lang(self.bot, int(uid), None)
                    await user.send(l10n.get("map_deleted_inactive_dm", lang))
                except Exception:
                    pass
                await _delete_entry(self.bot, uid); expired += 1
            elif lc < remind_cut:
                rs = _parse(r["reminder_sent_at"])
                if rs is None or rs < remind_cut:          # noch nicht in diesem Zyklus erinnert
                    try:
                        user = await self.bot.fetch_user(int(uid))
                        lang = await get_user_lang(self.bot, int(uid), None)
                        await user.send(l10n.get("map_reconfirm_dm", lang), view=MapConfirmView())
                        await execute_db(self.bot,
                            "UPDATE map_entries SET reminder_sent_at=datetime('now') WHERE user_id=?",
                            (uid,), commit=True)
                        reminded += 1
                    except Exception:
                        pass
        if pruned or reminded or expired:
            logger.info("🗺️ Map-Maintenance: %d entfernt (Austritt), %d erinnert, %d gelöscht (inaktiv)",
                        pruned, reminded, expired)

    @daily_maintenance.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()

    # ── Event-Cleanup (vergangene Einmal-Events) ──────────────────────────────
    @tasks.loop(hours=24)
    async def event_cleanup(self):
        cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        rc = await execute_db(self.bot,
            "DELETE FROM map_events WHERE status='approved' "
            "AND (rrule IS NULL OR rrule='') AND COALESCE(end_at, start_at) < ?",
            (cutoff,), commit=True)
        if rc:
            logger.info("🗺️ Event-Cleanup: %d vergangene Einmal-Events entfernt", rc)

    @event_cleanup.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ── Geodaten selbst bereitstellen & aktuell halten ────────────────────────
    @tasks.loop(hours=720)   # Intervall wird in __init__ aus MAP_GEODATA_REFRESH_DAYS gesetzt
    async def geodata_refresh(self):
        try:
            lines = await asyncio.to_thread(map_geodata.refresh)   # Leaflet + GeoJSON + GeoNames-PLZ
            geo.load()                       # PLZ-Datensatz nach Aktualisierung neu laden
            logger.info("🗺️ Geodaten-Refresh: %s", " · ".join(lines))
        except Exception as e:
            logger.warning("🗺️ Geodaten-Refresh fehlgeschlagen: %s", e)

    @geodata_refresh.before_loop
    async def _before_geodata(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    if not MAP_ENABLED:
        return
    bot.add_cog(MapTasksCog(bot))
