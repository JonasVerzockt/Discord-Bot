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
cogs/board.py – Startet die Web-Oberfläche des Bots (Feedback-Board, Karte, Konto, …).

Seit 5.0.0 liegt die eigentliche Web-App im Paket ``web/``:

  web/__init__.py      build_app(bot) – registriert alle Routen
  web/core/            Sicherheit/Sitzungen, Rendering, statische Dateien, Partner-Daten
  web/features/        je Bereich ein Modul (feedback, map, events, account, swarm, …)
  web/templates/       Jinja2-Templates als .html-Dateien
  utils/health.py      Status-Kacheln, Vorfall-Historie, Alarm-DMs

Dieses Cog macht nur noch drei Dinge: aiohttp-Server im Bot-Prozess starten/stoppen
(auf dem Bot-Loop, via AppRunner/TCPSite), die Board-DB anlegen und minütlich die
Health-Checks auswerten. Standardmäßig AUS (``BOARD_ENABLED``).

Sicherheit: nur an 127.0.0.1 binden (Reverse-Proxy/HTTPS davor), Honeypot,
Rate-Limits, HMAC-gehashte IPs (keine Roh-IP), CSRF auf Admin-Aktionen,
Jinja2-Autoescape gegen XSS.
"""
import logging

import discord
from aiohttp import web
from discord.ext import commands, tasks

from config import (BOARD_ENABLED, BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL, BOARD_OWNER_ID,
                    BOARD_OAUTH_CLIENT_ID, BOARD_OAUTH_REDIRECT_URI)
from utils.board_db import board_init
from utils.health import _process_alerts, _record_incidents
from web import build_app
from web.core.security import _admin_ids
from web.core.static import _DebugAccessLogger

logger = logging.getLogger(__name__)




class BoardCog(commands.Cog, name="Board"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.runner: web.AppRunner | None = None
        if BOARD_ENABLED:
            self._task = bot.loop.create_task(self._start())

    async def _start(self):
        await self.bot.wait_until_ready()
        if not _admin_ids() or not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
            logger.warning("⚠️ Board aktiv, aber Admin-Login unmöglich: BOARD_OWNER_ID und "
                           "Discord-OAuth (BOARD_OAUTH_*) müssen gesetzt sein.")
        if not BOARD_OWNER_ID:
            logger.warning("⚠️ Board aktiv, aber BOARD_OWNER_ID=0 – Owner-DMs werden übersprungen.")
        try:
            await board_init()
            self.runner = web.AppRunner(build_app(self.bot),
                                        access_log_class=_DebugAccessLogger)
            await self.runner.setup()
            await web.TCPSite(self.runner, BOARD_BIND, BOARD_PORT).start()
            logger.info("🌐 Feedback-Board läuft auf http://%s:%d (öffentlich: %s)",
                        BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL or "—")
            self.incident_monitor.start()   # Vorfall-Historie der Status-Kacheln
        except Exception as e:
            logger.error("❌ Board-Start fehlgeschlagen: %s", e, exc_info=True)

    @tasks.loop(minutes=1)
    async def incident_monitor(self):
        """Wertet minütlich die Health-Checks aus und schreibt die Vorfall-Historie fort."""
        await _record_incidents(self.bot)
        try:
            await _process_alerts(self.bot)
        except Exception as e:
            logger.warning("🔔 Alarm-Verarbeitung fehlgeschlagen: %s", e)

    @incident_monitor.before_loop
    async def _before_incident_monitor(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        if self.incident_monitor.is_running():
            self.incident_monitor.cancel()
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())


def setup(bot: discord.Bot):
    bot.add_cog(BoardCog(bot))
