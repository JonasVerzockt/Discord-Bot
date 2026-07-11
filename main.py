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
main.py – Einstiegspunkt für den AAM Discord Bot (modulare Version).

Startet den Bot, richtet Logging ein, initialisiert die DB
und lädt alle Cogs dynamisch.
"""
import asyncio
import logging

import discord
from discord.ext import commands

from config import DISCORD_TOKEN
from utils.logging_setup import setup_logging
from utils.db import init_db

# ── Logging früh einrichten ───────────────────────────────────────────────────
setup_logging()
logger = logging.getLogger(__name__)

# ── Bot-Konfiguration ─────────────────────────────────────────────────────────
intents                  = discord.Intents.default()
intents.message_content  = True
intents.members          = True
intents.reactions        = True

bot = discord.Bot(intents=intents)

# ── Alle Cogs ─────────────────────────────────────────────────────────────────
INITIAL_COGS = [
    "cogs.server_settings",   # /startup, guild events, allowed_channel/admin decorators
    "cogs.reviews",           # on_message / on_message_edit / on_raw_reaction_add
    "cogs.admin",             # /status / /pending / /test / /rescan / /export
    "cogs.user_settings",     # /usersetting language / blacklist_* / shop_list
    "cogs.notifications",     # /notification / /delete_notifications / /history / /testnotification
    "cogs.stats",             # /stats / /system / /help
    "cogs.shop_admin",        # /reloadshops / /shopmapping / /ch_delivery
    "cogs.shop_mapping",      # /shopmap set|list|remove (Review-CSV: Shop-Text → URL)
    "cogs.tasks",             # Background tasks (alle 5 Min, stündlich, täglich, wöchentlich)
    "cogs.ai_chat",           # KI-Chat-Bot (AI_CHAT_CHANNEL_IDS + @-Erwähnung)
    "cogs.inat_tracker",      # iNaturalist-Links → Google Sheets
    "cogs.price_tracking",    # /track_price /my_price_tracking /untrack_price + stündl. Preischeck
    "cogs.price_history",     # /price_history (Preisverlauf-Chart mit Bestpreis-Marker)
    "cogs.price_targets",     # /set_target (Zielpreis-Alerts)
    "cogs.discount_codes",    # /codes /codes_rescan + Haiku-Rabattcode-Tracker
    "cogs.digest",            # /digest + wöchentlicher DM-Digest (Preisstürze, neue Arten/Shops)
    "cogs.achievements",      # /achievements + Erfolge-Freischaltung/DM-Ping
    "cogs.sells",             # /sells – Preisvergleich einer Art/Gattung über alle Shops
    "cogs.offers",            # /offers – alle lagernden Angebote eines Shops
]


async def main():
    async with bot:
        # DB initialisieren (Tabellen anlegen / EU-Länder seeden)
        await init_db(bot)
        logger.info("✅ Datenbank initialisiert")

        # Cogs laden
        for cog in INITIAL_COGS:
            try:
                bot.load_extension(cog)
                logger.info(f"📦 Cog geladen: {cog}")
            except Exception as e:
                logger.error(f"❌ Fehler beim Laden von {cog}: {e}", exc_info=True)

        # Alle Slash-Befehle nur auf Servern erlauben (nicht in DMs/PNs).
        # Discord blendet sie damit in der Bot-PN aus und lehnt sie ab.
        # Betrifft NUR Befehle – DMs senden/empfangen (Benachrichtigungen,
        # Preis-DMs, Feedback-Reaktionen) läuft über Events und bleibt aktiv.
        for _cmd in bot.walk_application_commands():
            _cmd.guild_only = True

        logger.info("🚀 Bot verbindet sich mit Discord…")
        await bot.start(DISCORD_TOKEN)


@bot.event
async def on_ready():
    logger.info(f"✅ Bot online als {bot.user} ({bot.user.id})")
    logger.info(f"   Verbunden mit {len(bot.guilds)} Server(n)")


if __name__ == "__main__":
    asyncio.run(main())
