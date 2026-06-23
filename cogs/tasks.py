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
cogs/tasks.py – Automatisierte Hintergrundaufgaben.

Zeitplan:
  • Verfügbarkeitsprüfung    alle 5 Minuten
  • Shop-Daten-Reload        stündlich
  • Shop-Ratings-Sync        alle 48 Stunden (von Google Sheets)
  • Abgelaufene Benachricht. täglich (nach 1 Jahr → 'expired')
  • DB-Optimierung           wöchentlich
  • Bot-Status               jede Minute (Uptime / Server-Anzahl)
"""
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from utils.db import execute_db
from utils.availability import load_shop_data

logger = logging.getLogger(__name__)


class TasksCog(commands.Cog, name="Tasks"):

    def __init__(self, bot: discord.Bot):
        self.bot       = bot
        self._start_time = datetime.utcnow()
        # Tasks starten
        self.check_availability.start()
        self.reload_shops_task.start()
        self.expire_old_notifications.start()
        self.optimize_db.start()
        self.update_bot_status.start()

    def cog_unload(self):
        self.check_availability.cancel()
        self.reload_shops_task.cancel()
        self.expire_old_notifications.cancel()
        self.optimize_db.cancel()
        self.update_bot_status.cancel()

    # ── Verfügbarkeitsprüfung alle 5 Minuten ──────────────────────────────────
    @tasks.loop(minutes=5)
    async def check_availability(self):
        try:
            rows = await execute_db(
                self.bot,
                "SELECT user_id, species, regions, excluded_species FROM notifications WHERE status='active'",
                fetch=True,
            )
            if not rows:
                return

            notifications_cog = self.bot.cogs.get("Notifications")
            if not notifications_cog:
                logger.warning("NotificationsCog nicht geladen – Verfügbarkeitsprüfung übersprungen")
                return

            for row in rows:
                try:
                    excluded = set()
                    if row["excluded_species"]:
                        excluded = {s.strip().lower() for s in row["excluded_species"].split(",")}
                    await notifications_cog.trigger_availability_check(
                        row["user_id"], row["species"], row["regions"],
                        excluded_species_list=excluded,
                    )
                except Exception as e:
                    logger.error(f"Verfügbarkeitsprüfung fehlgeschlagen ({row['user_id']}, {row['species']}): {e}")
        except Exception as e:
            logger.error(f"check_availability task error: {e}", exc_info=True)

    @check_availability.before_loop
    async def before_check_availability(self):
        await self.bot.wait_until_ready()

    # ── Shop-Daten-Reload stündlich ────────────────────────────────────────────
    @tasks.loop(hours=1)
    async def reload_shops_task(self):
        try:
            shop_data = await load_shop_data(self.bot)
            for sid, sd in shop_data.items():
                await execute_db(
                    self.bot,
                    "INSERT OR REPLACE INTO shops (id, name, country, url) VALUES (?, ?, ?, ?)",
                    (sid, sd.get("name"), sd.get("country"), sd.get("url")),
                    commit=True,
                )
            logger.info(f"Shop-Daten neu geladen: {len(shop_data)} Shops")
        except Exception as e:
            logger.error(f"reload_shops_task error: {e}")

    @reload_shops_task.before_loop
    async def before_reload_shops(self):
        await self.bot.wait_until_ready()

    # ── Abgelaufene Benachrichtigungen täglich markieren ──────────────────────
    @tasks.loop(hours=24)
    async def expire_old_notifications(self):
        try:
            cutoff = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
            rows   = await execute_db(
                self.bot,
                """SELECT user_id, species, regions, server_id FROM notifications
                   WHERE status='active' AND created_at < ?""",
                (cutoff,), fetch=True,
            )
            for row in rows:
                await execute_db(
                    self.bot,
                    "UPDATE notifications SET status='expired' WHERE user_id=? AND species=? AND regions=?",
                    (row["user_id"], row["species"], row["regions"]), commit=True,
                )
                # User per DM benachrichtigen
                try:
                    user = await self.bot.fetch_user(int(row["user_id"]))
                    from utils.localization import get_user_lang, l10n
                    lang = await get_user_lang(self.bot, row["user_id"], row["server_id"])
                    await user.send(l10n.get(
                        "notification_expired_dm", lang,
                        species=row["species"], regions=row["regions"],
                    ))
                except Exception:
                    pass
            if rows:
                logger.info(f"{len(rows)} Benachrichtigungen als abgelaufen markiert")
        except Exception as e:
            logger.error(f"expire_old_notifications error: {e}")

    @expire_old_notifications.before_loop
    async def before_expire(self):
        await self.bot.wait_until_ready()

    # ── DB-Optimierung wöchentlich ─────────────────────────────────────────────
    @tasks.loop(hours=168)
    async def optimize_db(self):
        try:
            await execute_db(self.bot, "VACUUM", commit=True)
            await execute_db(self.bot, "ANALYZE", commit=True)
            logger.info("DB VACUUM + ANALYZE abgeschlossen")
        except Exception as e:
            logger.error(f"optimize_db error: {e}")

    @optimize_db.before_loop
    async def before_optimize(self):
        await self.bot.wait_until_ready()

    # ── Bot-Status jede Minute aktualisieren ──────────────────────────────────
    @tasks.loop(minutes=1)
    async def update_bot_status(self):
        try:
            uptime  = datetime.utcnow() - self._start_time
            hours   = int(uptime.total_seconds() // 3600)
            minutes = int((uptime.total_seconds() % 3600) // 60)
            servers = len(self.bot.guilds)
            users   = sum(g.member_count or 0 for g in self.bot.guilds)
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"{servers} Server | {users} User | ⏲️ {hours}h {minutes}m",
                )
            )
        except Exception as e:
            logger.debug(f"update_bot_status error: {e}")

    @update_bot_status.before_loop
    async def before_status(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    await bot.add_cog(TasksCog(bot))
