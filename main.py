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

# ══════════════════════════════════════════════════════════════════════════════
#  🔒 SICHERHEIT: GUILD-LOCK – dieser Bot-Account arbeitet NUR auf EINEM Server.
# ══════════════════════════════════════════════════════════════════════════════
#  Diese Instanz ist fest an den unten stehenden Discord-Server gebunden
#  ("Ameisen an die Macht"). Wird der Bot-Account auf einen anderen (fremden)
#  Server eingeladen, funktioniert dort KEIN Befehl und der Bot verlässt den
#  Server automatisch wieder (siehe on_guild_join / on_ready / _guild_lock_check).
#
#  Der Quellcode ist AGPLv3 und darf frei geforkt/betrieben werden – aber dann
#  mit EIGENEM Bot-Token/eigener Instanz. Dieser spezifische Account läuft nur
#  für den einen Server. Zum Betrieb einer eigenen Instanz einfach die ID unten
#  auf die eigene Server-ID setzen (oder per Env ALLOWED_GUILD_ID überschreiben).
#
#  Ändern erlaubt (AGPLv3) – für die offizielle AAM-Instanz aber unverändert
#  lassen. Override optional via Umgebungsvariable ALLOWED_GUILD_ID.
# ══════════════════════════════════════════════════════════════════════════════
import os as _os
ALLOWED_GUILD_ID: int = int(_os.getenv("ALLOWED_GUILD_ID", "375031723601297409"))

# ── Bot-Konfiguration ─────────────────────────────────────────────────────────
intents                  = discord.Intents.default()
intents.message_content  = True
intents.members          = True
intents.reactions        = True

bot = discord.Bot(intents=intents)


# ── Guild-Lock-Durchsetzung ───────────────────────────────────────────────────
async def _enforce_guild_lock(guild: "discord.Guild") -> bool:
    """Verlässt einen fremden Server automatisch. Gibt True zurück, wenn der
    Server erlaubt ist, sonst False (nachdem der Bot ihn verlassen hat)."""
    if guild is None:
        return True
    if guild.id == ALLOWED_GUILD_ID:
        return True
    logger.warning(
        f"⛔ Guild-Lock: fremder Server '{guild.name}' ({guild.id}) – "
        f"nur {ALLOWED_GUILD_ID} ist erlaubt. Verlasse den Server."
    )
    try:
        await guild.leave()
    except Exception as e:
        logger.error(f"❌ Konnte fremden Server {guild.id} nicht verlassen: {e}")
    return False


async def _guild_lock_check(ctx: "discord.ApplicationContext") -> bool:
    """Globaler Befehls-Check: erlaubt Befehle ausschließlich auf dem gebundenen
    Server. Greift zusätzlich zur Leave-Automatik (Defense-in-Depth)."""
    if getattr(ctx, "guild_id", None) == ALLOWED_GUILD_ID:
        return True
    logger.warning(
        f"⛔ Guild-Lock: Befehl auf nicht erlaubtem Server "
        f"{getattr(ctx, 'guild_id', None)} blockiert."
    )
    raise commands.CheckFailure("")   # leer -> lokalisierter Fallback im Error-Handler


bot.add_check(_guild_lock_check)

# ── Alle Cogs ─────────────────────────────────────────────────────────────────
INITIAL_COGS = [
    "cogs.server_settings",   # /startup, guild events, allowed_channel/admin decorators
    "cogs.reviews",           # on_message / on_message_edit / on_raw_reaction_add
    "cogs.admin",             # /status / /pending / /test / /rescan / /export
    "cogs.user_settings",     # /usersetting language / blacklist_* / shop_list
    "cogs.notifications",     # /notification / /delete_notifications / /history / /testnotification
    "cogs.stats",             # /stats / /system / /help
    "cogs.command_log",       # Befehls-Nutzungsprotokoll (Mod-Kanal + DB)
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
    # 🔒 Guild-Lock: beim Start jeden fremden Server sofort verlassen.
    for g in list(bot.guilds):
        await _enforce_guild_lock(g)


@bot.event
async def on_guild_join(guild: discord.Guild):
    """🔒 Guild-Lock: wird der Bot auf einen fremden Server eingeladen, verlässt
    er ihn sofort wieder. Nur der gebundene Server (ALLOWED_GUILD_ID) bleibt."""
    await _enforce_guild_lock(guild)


if __name__ == "__main__":
    asyncio.run(main())
