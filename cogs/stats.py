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
from utils.availability import load_shop_data
from cogs.server_settings import admin_or_manage_messages, allowed_channel
from config import SHOPS_DATA_FILE

logger = logging.getLogger(__name__)

_start_time = datetime.utcnow()


class StatsCog(commands.Cog, name="Stats"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="stats", description="Show bot statistics (Admin/Mod)")
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
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"stats error: {e}")
            await ctx.respond(l10n.get("stats_error", lang), ephemeral=True)

    @discord.slash_command(name="system", description="Show system and bot status (Admin/Mod)")
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
                integrity = "✅ OK" if check and check[0][0] == "ok" else "⚠️ Fehler"
            except Exception:
                integrity = "❌ Unbekannt"

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
            logger.error(f"system error: {e}")
            await ctx.respond(l10n.get("system_error", lang), ephemeral=True)

    @discord.slash_command(name="help", description="Show all available commands")
    @allowed_channel()
    async def help_cmd(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        keys = [
            "help_notification", "help_history", "help_test", "help_delete",
            "help_usersetting", "help_startup",
            "help_stats", "help_system",
            "help_reloadshops", "help_shopmapping",
        ]
        commands_text = "\n".join(l10n.get(k, lang) for k in keys)
        await ctx.respond(
            l10n.get("help_full", lang, commands=commands_text),
            ephemeral=True,
        )


async def setup(bot: discord.Bot):
    await bot.add_cog(StatsCog(bot))
