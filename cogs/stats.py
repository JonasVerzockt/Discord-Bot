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
cogs/stats.py – Statistik- und System-Cog.

Slash Commands (Admin/Mod):
  /stats   – Benachrichtigungs-Statistiken + Top-Arten
  /system  – Systemstatus (Uptime, CPU, RAM, Datenbankstatus)
  /help    – Befehlsübersicht
"""
import logging
import platform
from datetime import datetime

import discord
import psutil
from discord.ext import commands

from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from config import AI_CHAT_PUBLIC
from utils.availability import load_shop_data
from cogs.server_settings import admin_or_manage_messages, allowed_channel
from config import SHOPS_DATA_FILE

logger = logging.getLogger(__name__)

_start_time = datetime.utcnow()


class StatsCog(commands.Cog, name="Stats"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="stats", description="Show bot statistics (Admin/Mod)", description_localizations={"de": "Bot-Statistiken anzeigen (Admin/Mod)"})
    @admin_or_manage_messages()
    async def stats(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            rows = await execute_db(
                self.bot,
                """SELECT status, COUNT(*) AS cnt FROM notifications GROUP BY status""",
                fetch=True,
            )
            counts = {r["status"]: r["cnt"] for r in rows}

            stat_row = await execute_db(
                self.bot,
                "SELECT value FROM global_stats WHERE key='deleted_total'",
                fetch=True,
            )
            deleted_total = stat_row[0]["value"] if stat_row else 0

            top_rows = await execute_db(
                self.bot,
                """SELECT species, COUNT(*) AS cnt FROM notifications
                   GROUP BY species ORDER BY cnt DESC LIMIT 5""",
                fetch=True,
            )
            top_species = "\n".join(f"• {r['species']} ({r['cnt']}x)" for r in top_rows) or "–"

            await ctx.respond(
                l10n.get(
                    "stats_message", lang,
                    active=counts.get("active", 0),
                    completed=counts.get("completed", 0),
                    expired=counts.get("expired", 0),
                    deleted_total=deleted_total,
                    top_species=top_species,
                ),
            )
        except Exception as e:
            logger.error(f"❌ stats error: {e}")
            await ctx.respond(l10n.get("stats_error", lang))

    @discord.slash_command(name="system", description="Show system and bot status (Admin/Mod)", description_localizations={"de": "System- und Bot-Status anzeigen (Admin/Mod)"})
    @admin_or_manage_messages()
    async def system(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            # Uptime
            uptime = datetime.utcnow() - _start_time
            h, rem = divmod(int(uptime.total_seconds()), 3600)
            m      = rem // 60
            uptime_str = f"{h}h {m}m"

            servers = len(self.bot.guilds)
            users   = sum(g.member_count or 0 for g in self.bot.guilds)

            # DB-Integrität
            try:
                check = await execute_db(self.bot, "PRAGMA integrity_check", fetch=True)
                integrity = l10n.get("system_integrity_ok", lang) if check and check[0][0] == "ok" else l10n.get("system_integrity_error", lang)
            except Exception:
                integrity = l10n.get("system_integrity_unknown", lang)

            total_rows = await execute_db(
                self.bot, "SELECT COUNT(*) AS c FROM notifications", fetch=True
            )
            total = total_rows[0]["c"] if total_rows else 0

            # Shop-Datei-Status
            import os, time as _time
            try:
                mtime = os.path.getmtime(SHOPS_DATA_FILE)
                age   = datetime.utcnow() - datetime.utcfromtimestamp(mtime)
                ah, ar = divmod(int(age.total_seconds()), 3600)
                am    = ar // 60
                file_status = l10n.get(
                    "system_file_status", lang,
                    modified=datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                    age=f"{ah}h {am}m",
                )
            except FileNotFoundError:
                file_status = l10n.get("system_file_missing", lang)

            await ctx.respond(
                l10n.get(
                    "system_status", lang,
                    uptime=uptime_str, servers=servers, users=users,
                    integrity=integrity, total=total, file_status=file_status,
                ),
                ephemeral=True,
            )
            await ctx.followup.send(
                l10n.get(
                    "system_performance", lang,
                    latency=round(self.bot.latency * 1000),
                    cpu=psutil.cpu_percent(),
                    ram=psutil.virtual_memory().percent,
                    system=f"{platform.system()} {platform.release()}",
                ),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"❌ system error: {e}")
            await ctx.respond(l10n.get("system_error", lang), ephemeral=True)

    def _build_help_text(self, lang: str) -> str:
        """Baut den vollständigen Hilfetext (genutzt von /help und !help)."""
        user_keys = [
            "help_notification", "help_history", "help_test", "help_delete",
            "help_usersetting", "help_ch_delivery",
            "help_track_price", "help_my_price_tracking", "help_untrack_price",
            "help_price_history",
            "help_set_target",
            "help_achievements",
            "help_codes",
            "help_digest",
        ]
        admin_keys = [
            "help_startup", "help_status", "help_pending",
            "help_rescan", "help_reprocess", "help_export",
            "help_stats", "help_system",
            "help_reloadshops", "help_shopmapping", "help_shopurl",
            "help_codes_set", "help_codes_rescan",
        ]
        ai_keys = [
            "help_ai_chat",
            "help_test_admin",
            "help_ai_reset",
            "help_ai_prompt",
        ]
        user_commands  = "\n".join(l10n.get(k, lang) for k in user_keys)
        admin_commands = "\n".join(l10n.get(k, lang) for k in admin_keys)
        if AI_CHAT_PUBLIC:
            ai_commands = "\n".join(l10n.get(k, lang) for k in ai_keys)
            ai_section  = l10n.get("help_ai_section", lang, ai_commands=ai_commands)
        else:
            ai_section = ""
        return l10n.get(
            "help_full", lang,
            user_commands=user_commands,
            admin_commands=admin_commands,
            ai_section=ai_section,
        )

    @discord.slash_command(name="help", description="Show all available commands", description_localizations={"de": "Alle verfügbaren Befehle anzeigen"})
    @allowed_channel()
    async def help_cmd(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ctx.respond(self._build_help_text(lang))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Reagiert zusätzlich auf `!help` als Textbefehl (nur im Bot-Kanal)."""
        if message.author.bot or message.guild is None:
            return
        if message.content.strip().lower() not in ("!help", "!hilfe"):
            return
        # Kanal-Check analog zu allowed_channel(): ohne /startup überall erlaubt,
        # sonst nur im konfigurierten Bot-Kanal (falscher Kanal → stilles Ignorieren).
        rows = await execute_db(
            self.bot,
            "SELECT channel_id FROM server_settings WHERE server_id=?",
            (message.guild.id,),
            fetch=True,
        )
        if rows and rows[0]["channel_id"] is not None and message.channel.id != rows[0]["channel_id"]:
            return
        lang = await get_user_lang(self.bot, message.author.id, message.guild.id)
        try:
            await message.reply(self._build_help_text(lang), mention_author=False)
        except discord.Forbidden:
            logger.warning("❌ !help: Keine Berechtigung zum Antworten in Kanal %s", message.channel.id)


def setup(bot: discord.Bot):
    bot.add_cog(StatsCog(bot))
