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
cogs/user_settings.py – Benutzereinstellungen als discord.py Cog.

Slash Command Gruppe /usersetting:
  language          – Eigene Sprache setzen
  blacklist_add     – Shop zur Blacklist hinzufügen
  blacklist_remove  – Shop von Blacklist entfernen
  blacklist_list    – Eigene Blacklist anzeigen
  shop_list         – Alle Shops anzeigen (optional nach Region filtern)
"""
import logging
import discord
from discord.ext import commands
from rapidfuzz import process

from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data, format_rating
from cogs.server_settings import allowed_channel

logger = logging.getLogger(__name__)

# ── Hilfsfunktion ─────────────────────────────────────────────────────────────

def _split_message(text: str, max_length: int = 2000) -> list[str]:
    lines, blocks, current = text.split("\n"), [], ""
    for line in lines:
        if len(current) + len(line) + 1 > max_length:
            blocks.append(current)
            current = ""
        current += line + "\n"
    if current:
        blocks.append(current)
    return blocks


# ── Cog ───────────────────────────────────────────────────────────────────────

class UserSettingsCog(commands.Cog, name="UserSettings"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    settings = discord.SlashCommandGroup(
        name="usersetting",
        description="Manage your personal settings", description_localizations={"de": "Deine persönlichen Einstellungen verwalten"},
    )

    @settings.command(description="Set your language", description_localizations={"de": "Deine Sprache festlegen"})
    @allowed_channel()
    async def language(
        self,
        ctx: discord.ApplicationContext,
        language: discord.Option(
            str,
            "Language (de = Deutsch, en = English, eo = Esperanto)", description_localizations={"de": 'Sprache (de = Deutsch, en = English, eo = Esperanto)', "en-US": 'Language (de = German, en = English, eo = Esperanto)'},
            choices=["de", "en", "eo"],
            default="en",
        ),
    ):
        await execute_db(
            self.bot,
            """INSERT INTO user_settings (user_id, language) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET language=excluded.language""",
            (ctx.author.id, language),
            commit=True,
        )
        await ctx.respond(l10n.get("user_setting_success", language), ephemeral=True)

    @settings.command(description="Add a shop to your blacklist", description_localizations={"de": "Einen Shop auf deine Blacklist setzen"})
    @allowed_channel()
    async def blacklist_add(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop name", description_localizations={"de": 'Shop-Name', "en-US": 'Shop name'}, required=True),
    ):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)
        shop_names = {sid: sd["name"] for sid, sd in shop_data.items()}

        matches   = process.extract(shop, shop_names.values(), limit=3)
        best      = next((m for m in matches if m[1] > 75), None)
        if not best:
            suggestions = "\n".join(f"- {m[0]}" for m in matches)
            await ctx.respond(
                l10n.get("shop_not_found_suggest", lang, shop=shop, suggestions=suggestions),
                ephemeral=True,
            )
            return

        shop_name = best[0]
        shop_id   = next(sid for sid, name in shop_names.items() if name == shop_name)
        await execute_db(
            self.bot,
            "INSERT OR IGNORE INTO user_shop_blacklist (user_id, shop_id) VALUES (?, ?)",
            (str(ctx.author.id), shop_id),
            commit=True,
        )
        await ctx.respond(l10n.get("blacklist_add_success", lang, shop=shop_name), ephemeral=True)

    @settings.command(description="Remove a shop from your blacklist", description_localizations={"de": "Einen Shop von deiner Blacklist entfernen"})
    @allowed_channel()
    async def blacklist_remove(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop name", description_localizations={"de": 'Shop-Name', "en-US": 'Shop name'}, required=True),
    ):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)
        shop_names = {sid: sd["name"] for sid, sd in shop_data.items()}

        matches = process.extract(shop, shop_names.values(), limit=3)
        best    = next((m for m in matches if m[1] > 75), None)
        if not best:
            suggestions = "\n".join(f"- {m[0]}" for m in matches)
            await ctx.respond(
                l10n.get("shop_not_found_suggest", lang, shop=shop, suggestions=suggestions),
                ephemeral=True,
            )
            return

        shop_name = best[0]
        shop_id   = next(sid for sid, name in shop_names.items() if name == shop_name)
        rowcount  = await execute_db(
            self.bot,
            "DELETE FROM user_shop_blacklist WHERE user_id=? AND shop_id=?",
            (str(ctx.author.id), shop_id),
            commit=True,
        )
        key = "blacklist_remove_success" if rowcount > 0 else "shop_not_in_blacklist"
        await ctx.respond(
            l10n.get(key, lang, shop=shop_name),
            ephemeral=True,
        )

    @settings.command(description="Show your blacklisted shops", description_localizations={"de": "Deine Blacklist anzeigen"})
    @allowed_channel()
    async def blacklist_list(self, ctx: discord.ApplicationContext):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)
        rows      = await execute_db(
            self.bot,
            "SELECT shop_id FROM user_shop_blacklist WHERE user_id=?",
            (str(ctx.author.id),),
            fetch=True,
        )
        shops = [shop_data[str(r["shop_id"])]["name"]
                 for r in rows if str(r["shop_id"]) in shop_data]
        if not shops:
            await ctx.respond(l10n.get("blacklist_empty", lang), ephemeral=True)
            return
        await ctx.respond(
            l10n.get("blacklist_list", lang, shops="\n- ".join(shops)),
            ephemeral=True,
        )

    @settings.command(description="List all available shops", description_localizations={"de": "Alle verfügbaren Shops anzeigen"})
    @allowed_channel()
    async def shop_list(
        self,
        ctx: discord.ApplicationContext,
        country: discord.Option(
            str,
            "Filter by country code (de, at, ch for Swiss delivery, ...)", description_localizations={"de": 'Nach Ländercode filtern (de, at, ch für CH-Lieferung, ...)', "en-US": 'Filter by country code (de, at, ch for Swiss delivery, ...)'},
            required=False,
            default=None,
        ),
    ):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)

        if country:
            cl = country.lower()
            if cl == "ch":
                ch_rows = await execute_db(
                    self.bot,
                    "SELECT shop_id FROM ch_delivery_shops",
                    fetch=True,
                )
                manual_ids = {str(r["shop_id"]) for r in ch_rows}
                auto_ids   = {sid for sid, sd in shop_data.items()
                              if sd.get("country", "").lower() == "ch"}
                ch_ids     = manual_ids | auto_ids
                filtered   = [sd for sid, sd in shop_data.items() if sid in ch_ids]
            else:
                filtered = [sd for sd in shop_data.values()
                            if sd.get("country", "").lower() == cl]
        else:
            filtered = list(shop_data.values())

        if not filtered:
            await ctx.respond(l10n.get("no_shops_found", lang))
            return

        # Nach Bewertung sortieren
        filtered.sort(key=lambda s: (
            s.get("average_rating") is None,
            -(s.get("average_rating") or 0),
            s.get("name", "").lower(),
        ))

        entries = [
            f"`{s.get('id')}` | {s.get('name', '?')} - {format_rating(s.get('average_rating'))}"
            for s in filtered
        ]
        text   = l10n.get("available_shops", lang, shops="\n- " + "\n- ".join(entries))
        blocks = _split_message(text)

        await ctx.respond(blocks[0])
        for block in blocks[1:]:
            await ctx.followup.send(block)


def setup(bot: discord.Bot):
    bot.add_cog(UserSettingsCog(bot))
