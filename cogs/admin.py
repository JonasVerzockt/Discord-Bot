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
cogs/admin.py - Admin-Befehle als discord.py Cog.

Slash Commands (nur Admins / Manage-Messages):
  /status   - Anzahl Bewertungen / verarbeitet / ausstehend
  /pending  - Liste ausstehender Nachrichten
  /test     - KI-Parser testen ohne Sheet-Eintrag
  /rescan   - Letzte N Tage manuell neu abgleichen
  /export   - Sheet-Rohdaten als JSON exportieren (ephemeral)
"""
import json
import logging
from datetime import datetime

import discord
from discord.ext import commands

from config import MAPPING_FILE, SCAN_DAYS, REVIEW_CHANNEL_ID
from utils.sheet import sheet
from utils.tracking import get_all_pending, get_all_tracking, pending_count, tracking_count
from utils.ai_parser import parse_with_ai
from cogs.server_settings import admin_or_manage_messages
from utils.localization import l10n, get_user_lang

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog, name="Admin"):
    """Verwaltungsbefehle fuer Bot-Admins."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="status", description="Show review bot status (Admin/Mod)")
    @admin_or_manage_messages()
    async def cmd_status(self, ctx: discord.ApplicationContext):
        """Zeigt Bewertungsanzahl, verarbeitete und ausstehende Nachrichten."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            rows  = sheet.row_count - 1
            pend  = await pending_count(self.bot)
            track = await tracking_count(self.bot)
            await ctx.respond(
                l10n.get("review_status", lang, rows=rows, tracked=track, pending=pend),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"status error: {e}")
            await ctx.respond(l10n.get("admin_error", lang, error=e), ephemeral=True)

    @discord.slash_command(name="pending", description="List pending messages (Admin/Mod)")
    @admin_or_manage_messages()
    async def cmd_pending(self, ctx: discord.ApplicationContext):
        """Listet ausstehende Nachrichten auf."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        pending = await get_all_pending(self.bot)
        if not pending:
            await ctx.respond(l10n.get("review_pending_none", lang), ephemeral=True)
            return

        lines = [
            "* `" + mid + "` - " + v["reason"]
            + (" -> `" + v["identifier"] + "`" if v.get("identifier") else "")
            for mid, v in list(pending.items())[:10]
        ]
        extra = (
            "\n" + l10n.get("admin_pending_more", lang, count=len(pending) - 10)
            if len(pending) > 10 else ""
        )
        header   = l10n.get("review_pending_header", lang, count=len(pending))
        csv_note = l10n.get("review_pending_fill_csv", lang, file=MAPPING_FILE)
        await ctx.respond(
            header + "\n" + "\n".join(lines) + extra + "\n\n" + csv_note,
            ephemeral=True,
        )

    @discord.slash_command(name="test", description="Test AI parser without writing to sheet (Admin/Mod)")
    @admin_or_manage_messages()
    async def cmd_test(
        self,
        ctx: discord.ApplicationContext,
        text: discord.Option(str, "Message text to parse", required=True),
    ):
        """Testet den KI-Parser ohne Sheet-Eintrag."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            parsed = parse_with_ai(text, "TEST-SHOP", datetime.now().strftime("%d.%m.%Y"))
            await ctx.respond(
                "```json\n" + json.dumps(parsed, ensure_ascii=False, indent=2) + "\n```",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"test error: {e}")
            await ctx.respond(l10n.get("admin_error", lang, error=e), ephemeral=True)

    @discord.slash_command(name="rescan", description="Manually re-reconcile last N days (Admin/Mod)")
    @admin_or_manage_messages()
    async def cmd_rescan(self, ctx: discord.ApplicationContext):
        """Gleicht die letzten N Tage manuell erneut ab."""
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)

        reviews_cog = self.bot.cogs.get("Reviews")
        if not reviews_cog:
            await ctx.respond(l10n.get("admin_rescan_cog_missing", lang), ephemeral=True)
            return

        channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
        if not channel:
            await ctx.respond(l10n.get("admin_rescan_channel_missing", lang), ephemeral=True)
            return

        await ctx.followup.send(
            l10n.get("admin_rescan_scanning", lang, days=SCAN_DAYS), ephemeral=True
        )
        mapped, written = await reviews_cog._reconcile_scan(channel)
        all_pending = await get_all_pending(self.bot)
        await ctx.followup.send(
            l10n.get(
                "admin_rescan_result", lang,
                mapped=mapped, written=written, pending=len(all_pending),
            ),
            ephemeral=True,
        )

    @discord.slash_command(name="export", description="Export raw sheet data as JSON (Admin/Mod)")
    @admin_or_manage_messages()
    async def cmd_export(self, ctx: discord.ApplicationContext):
        """Exportiert Sheet-Rohdaten (erste 50 Zeilen) als JSON."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            rows  = sheet.rows[:51]
            lines = json.dumps(rows, ensure_ascii=False, indent=2)
            row_count = len(rows) - 1
            if len(lines) < 1900:
                header = l10n.get("admin_export_header", lang, rows=row_count)
                msg = f"{header}\n```json\n{lines}\n```"
            else:
                msg = l10n.get("admin_export_too_long", lang, rows=row_count)
            await ctx.respond(msg, ephemeral=True)
        except Exception as e:
            logger.error(f"export error: {e}")
            await ctx.respond(l10n.get("admin_error", lang, error=e), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
