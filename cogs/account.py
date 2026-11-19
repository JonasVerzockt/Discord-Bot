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
cogs/account.py – „Mein Konto“: Grundlagen auf der Bot-Seite.

  • /konto            – Link zur Board-Seite „Mein Konto“ (nur für dich sichtbar)
  • PN-Verlauf        – Hook auf Messageable.send (utils.account.install_dm_hook)
  • Einstellungen     – beim Start in den Speicher laden (Ruhezeiten, Sitzungen …)
  • täglich 03:41     – PN-Verlauf aufräumen (30 Tage, max. 50 je Person)

Die Seite selbst liegt in cogs/board.py (/konto), die Daten in utils/account.py.
"""
import logging
from datetime import time as dtime

import discord
from discord.ext import commands, tasks

from config import BOARD_PUBLIC_URL, BOARD_ENABLED
from utils import account as A
from utils.localization import l10n, get_user_lang
from utils.timez import BERLIN
from cogs.server_settings import allowed_channel

logger = logging.getLogger(__name__)


class AccountCog(commands.Cog, name="Account"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        A.install_dm_hook(bot)
        self._ready_task = bot.loop.create_task(self._init())
        self.prune.start()

    def cog_unload(self):
        self.prune.cancel()

    async def _init(self):
        await self.bot.wait_until_ready()
        try:
            n = await A.load_prefs(self.bot)
            logger.info("👤 Konto-Einstellungen geladen: %d", n)
        except Exception as e:
            logger.warning("👤 Konto-Einstellungen nicht geladen: %s", e)

    @tasks.loop(time=dtime(hour=3, minute=41, tzinfo=BERLIN))
    async def prune(self):
        try:
            await A.prune_dm_log(self.bot)
        except Exception as e:
            logger.warning("👤 PN-Verlauf aufräumen fehlgeschlagen: %s", e)

    @prune.before_loop
    async def _before_prune(self):
        await self.bot.wait_until_ready()

    @discord.slash_command(
        name="konto",
        description="Open your account page on the board (settings, data, history)",
        description_localizations={"de": "Deine Kontoseite im Board öffnen (Einstellungen, Daten, Verlauf)"},
    )
    @allowed_channel()
    async def konto(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        if not (BOARD_ENABLED and BOARD_PUBLIC_URL):
            await ctx.respond(l10n.get("konto_unavailable", lang), ephemeral=True)
            return
        url = f"{BOARD_PUBLIC_URL}/konto?lang={lang if lang in ('de', 'en', 'eo') else 'de'}"
        await ctx.respond(l10n.get("konto_link", lang, url=url), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(AccountCog(bot))
