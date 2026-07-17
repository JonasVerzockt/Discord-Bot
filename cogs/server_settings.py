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
import functools

from discord.ext import commands

from utils.db import execute_db
from utils.localization import l10n, get_user_lang

logger = logging.getLogger(__name__)


# ── Shared Decorators (importierbar von anderen Cogs) ─────────────────────────

def admin_or_manage_messages():
    async def predicate(ctx: discord.ApplicationContext) -> bool:
        if ctx.guild is not None:
            perms = ctx.author.guild_permissions
            if perms.administrator or perms.manage_messages:
                return True
        lang = await get_user_lang(ctx.bot, ctx.author.id, ctx.guild.id if ctx.guild else None)
        raise commands.CheckFailure(l10n.get("no_permission", lang))
    return commands.check(predicate)


class _ChannelConfirmView(discord.ui.View):
    """Ephemere Ja/Nein-Rückfrage für berechtigte User im falschen Kanal."""

    def __init__(self, owner_id: int, lang: str, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.lang = lang
        self.value = False
        self.responded = False                                # Nein-Button hat quittiert
        self.interaction: discord.Interaction | None = None  # Ja-Klick-Interaktion
        self.children[0].label = l10n.get("channel_confirm_yes", lang)
        self.children[1].label = l10n.get("channel_confirm_no", lang)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(style=discord.ButtonStyle.success)
    async def yes(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.value = True
        self.interaction = interaction   # NICHT beantworten – der Befehl nutzt sie
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def no(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.value = False
        self.responded = True
        try:
            await interaction.response.edit_message(
                content=l10n.get("wrong_channel_cancelled", self.lang), view=None
            )
        except Exception:
            pass
        self.stop()


def allowed_channel():
    """Kanal-Gate. Im konfigurierten Kanal (oder wenn keiner gesetzt ist) läuft der
    Befehl normal. In einem anderen Kanal wird er für normale User ephemer abgelehnt;
    User mit Admin-/„Nachrichten verwalten"-Recht bekommen eine ephemere Rückfrage –
    bei Bestätigung läuft der Befehl normal weiter (öffentlich), sonst gar nicht.

    Umgesetzt als signatur-erhaltender Wrapper (nicht als commands.check), damit die
    Slash-Optionen erhalten bleiben UND die Interaktion nach der Rückfrage frisch ist.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, ctx, *args, **kwargs):
            guild = ctx.guild
            channel_id = None
            if guild is not None:
                rows = await execute_db(
                    ctx.bot,
                    "SELECT channel_id FROM server_settings WHERE server_id=?",
                    (guild.id,), fetch=True,
                )
                channel_id = rows[0]["channel_id"] if rows else None

            # Richtiger Kanal oder kein Kanal konfiguriert -> normaler Ablauf (unverändert)
            if channel_id is None or ctx.channel_id == channel_id:
                return await func(self, ctx, *args, **kwargs)

            lang  = await get_user_lang(ctx.bot, ctx.author.id, guild.id if guild else None)
            perms = ctx.author.guild_permissions if guild is not None else None
            if not perms or not (perms.administrator or perms.manage_messages):
                await ctx.respond(l10n.get("wrong_channel", lang), ephemeral=True)
                return

            # Berechtigt -> ephemere Rückfrage
            view = _ChannelConfirmView(ctx.author.id, lang)
            await ctx.respond(l10n.get("wrong_channel_confirm", lang), view=view, ephemeral=True)
            await view.wait()

            if view.value and view.interaction is not None:
                try:
                    await ctx.interaction.edit_original_response(
                        content=l10n.get("wrong_channel_confirmed", lang), view=None
                    )
                except Exception:
                    pass
                # Befehls-Ausgabe auf die frische Button-Interaktion umleiten -> öffentlich
                ctx.interaction = view.interaction
                return await func(self, ctx, *args, **kwargs)

            # Timeout (kein Klick) -> ephemere Nachricht aktualisieren; bei "Nein" hat
            # der Button die Interaktion bereits quittiert und die Nachricht editiert.
            if not view.responded:
                try:
                    await ctx.edit(content=l10n.get("wrong_channel_cancelled", lang), view=None)
                except Exception:
                    pass
        return wrapper
    return decorator


# ── Cog ───────────────────────────────────────────────────────────────────────

class ServerSettingsCog(commands.Cog, name="ServerSettings"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_application_command_error(self, ctx: discord.ApplicationContext, error):
        """Bei abgelehntem Kanal-/Rechte-Check dem User kurz (ephemer) Bescheid geben,
        statt die Interaktion unbeantwortet in den Timeout laufen zu lassen."""
        if not isinstance(error, commands.CheckFailure):
            return
        msg = str(error).strip()
        if not msg:
            try:
                lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild.id if ctx.guild else None)
            except Exception:
                lang = "en"
            msg = l10n.get("command_check_failed", lang)
        try:
            if ctx.interaction.response.is_done():
                await ctx.followup.send(msg, ephemeral=True)
            else:
                await ctx.respond(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.slash_command(
        name="startup",
        description="Set server language and bot channel (Admin/Mod only)", description_localizations={"de": "Serversprache und Bot-Kanal festlegen (nur Admin/Mod)"},
    )
    @admin_or_manage_messages()
    @allowed_channel()
    async def startup(
        self,
        ctx: discord.ApplicationContext,
        language: discord.Option(
            str,
            "Bot language (de = Deutsch, en = English, eo = Esperanto)", description_localizations={"de": 'Bot-Sprache (de = Deutsch, en = English, eo = Esperanto)', "en-US": 'Bot language (de = German, en = English, eo = Esperanto)'},
            choices=["de", "en", "eo"],
            default="en",
        ),
        channel: discord.Option(
            discord.TextChannel,
            "Channel where bot commands are allowed (optional)", description_localizations={"de": 'Kanal, in dem Bot-Befehle erlaubt sind (optional)', "en-US": 'Channel where bot commands are allowed (optional)'},
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
        logger.info(f"✅ Server {server_id} eingerichtet: lang={language}, channel={channel_id}")

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
    bot.add_cog(ServerSettingsCog(bot))
