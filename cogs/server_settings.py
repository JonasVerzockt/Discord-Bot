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
cogs/server_settings.py – Server-Konfiguration als discord.py Cog.

Slash Commands:
  /startup  – Sprache + optionaler Channel pro Server setzen (Admin/Mod)

Stellt außerdem den allowed_channel()- und admin_or_manage_messages()-
Decorator für andere Cogs bereit (via utils.checks).
"""
import logging
import discord
from discord.ext import commands

from utils.db import execute_db
from utils.localization import l10n, get_user_lang

logger = logging.getLogger(__name__)


# ── Shared Decorators (importierbar von anderen Cogs) ──────────────────────────

def admin_or_manage_messages():
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        if ctx.guild is None:
            return True
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.manage_messages
    return commands.check(predicate)


def allowed_channel():
    """Prüft ob der Befehl im konfigurierten Server-Kanal ausgeführt wird."""
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        if ctx.guild is None:
            return True
        rows = await execute_db(
            ctx.bot,
            "SELECT channel_id FROM server_settings WHERE server_id=?",
            (ctx.guild.id,),
            fetch=True,
        )
        if not rows or rows[0]["channel_id"] is None:
            return True
        if ctx.channel_id == rows[0]["channel_id"]:
            return True
        lang = await get_user_lang(ctx.bot, ctx.author.id, ctx.guild.id)
        raise commands.CheckFailure(l10n.get("wrong_channel", lang))
    return commands.check(predicate)


# ── Cog ────────────────────────────────────────────────────────────────────────

class ServerSettingsCog(commands.Cog, name="ServerSettings"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="startup",
        description="Set server language and bot channel (Admin/Mod only)",
    )
    @admin_or_manage_messages()
    async def startup(
        self,
        ctx: discord.ApplicationContext,
        language: discord.Option(
            str,
            "Bot language (de = Deutsch, en = English, eo = Esperanto)",
            choices=["de", "en", "eo"],
            default="en",
        ),
        channel: discord.Option(
            discord.TextChannel,
            "Channel where bot commands are allowed (optional)",
            required=False,
            default=None,
        ),
    ):
        server_id  = ctx.guild.id
        channel_id = channel.id if channel else None

        if channel_id:
            await execute_db(
                self.bot,
                """INSERT INTO server_settings (server_id, channel_id, language)
                   VALUES (?, ?, ?)
                   ON CONFLICT(server_id) DO UPDATE SET
                       channel_id=excluded.channel_id,
                       language=excluded.language""",
                (server_id, channel_id, language),
                commit=True,
            )
        else:
            await execute_db(
                self.bot,
                """INSERT INTO server_settings (server_id, language)
                   VALUES (?, ?)
                   ON CONFLICT(server_id) DO UPDATE SET
                       language=excluded.language""",
                (server_id, language),
                commit=True,
            )

        channel_mention = channel.mention if channel else l10n.get("all_channels", language)
        await ctx.respond(
            l10n.get("server_setup_success", language, channel=channel_mention),
            ephemeral=True,
        )
        logger.info(f"Server {server_id} eingerichtet: lang={language}, channel={channel_id}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Server-Info beim Beitritt speichern."""
        await self._upsert_server_info(guild)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        await self._upsert_server_info(after)

    async def _upsert_server_info(self, guild: discord.Guild):
        await execute_db(
            self.bot,
            """INSERT INTO server_info
               (server_id, server_name, member_count, created_at, icon_url, splash_url, banner_url, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                   server_name=excluded.server_name,
                   member_count=excluded.member_count,
                   icon_url=excluded.icon_url,
                   splash_url=excluded.splash_url,
                   banner_url=excluded.banner_url,
                   description=excluded.description""",
            (
                guild.id,
                guild.name,
                guild.member_count,
                guild.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                str(guild.icon.url) if guild.icon else None,
                str(guild.splash.url) if guild.splash else None,
                str(guild.banner.url) if guild.banner else None,
                guild.description,
            ),
            commit=True,
        )


def setup(bot: discord.Bot):
    await bot.add_cog(ServerSettingsCog(bot))
