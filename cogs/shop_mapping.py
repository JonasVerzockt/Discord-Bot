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
cogs/shop_mapping.py – /shopmap: Review-Bot-Shopzuordnung (shop_mapping.csv) per Befehl.

Ordnet einen Shop-Text aus einer Bewertung (identifier) einer Shop-Domain/URL zu.
Genau das löst ein 🟡 (Shop nicht erkannt) auf. Anders als /shopmapping (externer
Name → interne AntCheck-ID) schreibt dies in die CSV, die die Review-Auflösung nutzt,
und aktualisiert den Live-Cache – die Änderung greift sofort (ohne Neustart).
"""
import logging

import discord
from discord.ext import commands

from utils.localization import l10n, get_user_lang
from utils.shop import set_mapping, remove_mapping, all_mappings
from cogs.server_settings import admin_or_manage_messages

logger = logging.getLogger(__name__)


class ShopMappingCsvCog(commands.Cog, name="ShopMappingCsv"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    shopmap = discord.SlashCommandGroup(
        "shopmap",
        "Review-Bot: Shop-Text einer URL zuordnen (löst 🟡 auf)",
        description_localizations={"de": "Review-Bot: Shop-Text einer URL zuordnen (löst 🟡 auf)"},
    )

    @shopmap.command(
        name="set",
        description="Map a review shop text to a shop URL (resolves the 🟡).",
        description_localizations={"de": "Shop-Text aus einer Bewertung einer Shop-URL zuordnen (löst 🟡 auf)."},
    )
    @admin_or_manage_messages()
    async def shopmap_set(
        self,
        ctx: discord.ApplicationContext,
        identifier: discord.Option(  # type: ignore[valid-type]
            str, "Shop-Text aus der Bewertung (z. B. Home of Insects)",
            description_localizations={"de": "Shop-Text aus der Bewertung (z. B. Home of Insects)",
                                       "en-US": "Shop text from the review (e.g. Home of Insects)"},
            required=True,
        ),
        url: discord.Option(  # type: ignore[valid-type]
            str, "Shop-Domain/URL (z. B. home-of-insects.com)",
            description_localizations={"de": "Shop-Domain/URL (z. B. home-of-insects.com)",
                                       "en-US": "Shop domain/URL (e.g. home-of-insects.com)"},
            required=True,
        ),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        set_mapping(identifier, url, msg_id="manual",
                    hint=f"manuell von {ctx.author.name}")
        logger.info("🗂️ shopmap set: '%s' → '%s' (by %s)", identifier, url, ctx.author.id)
        await ctx.respond(
            l10n.get("shopmap_set_ok", lang, identifier=identifier.strip(), url=url.strip()),
            ephemeral=True,
        )

    @shopmap.command(
        name="remove",
        description="Remove a review shop mapping.",
        description_localizations={"de": "Eine Shop-Zuordnung entfernen."},
    )
    @admin_or_manage_messages()
    async def shopmap_remove(
        self,
        ctx: discord.ApplicationContext,
        identifier: discord.Option(  # type: ignore[valid-type]
            str, "Shop-Text der zu entfernenden Zuordnung",
            description_localizations={"de": "Shop-Text der zu entfernenden Zuordnung",
                                       "en-US": "Shop text of the mapping to remove"},
            required=True,
        ),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        ok = remove_mapping(identifier)
        key = "shopmap_removed" if ok else "shopmap_not_found"
        await ctx.respond(l10n.get(key, lang, identifier=identifier.strip()), ephemeral=True)

    @shopmap.command(
        name="list",
        description="Show all review shop mappings.",
        description_localizations={"de": "Alle Shop-Zuordnungen anzeigen."},
    )
    @admin_or_manage_messages()
    async def shopmap_list(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = all_mappings()
        if not rows:
            await ctx.respond(l10n.get("shopmap_list_empty", lang), ephemeral=True)
            return
        lines = [l10n.get("shopmap_list_header", lang)]
        for identifier, url in rows:
            if url:
                lines.append(f"• `{identifier}` → `{url}`")
            else:
                lines.append(l10n.get("shopmap_pending_line", lang, identifier=identifier))
        text = "\n".join(lines)
        await ctx.respond(text[:1990], ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(ShopMappingCsvCog(bot))
