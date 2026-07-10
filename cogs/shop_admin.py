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
cogs/shop_admin.py – Shop-Verwaltungsbefehle als discord.py Cog.

Slash Commands (Admin/Mod):
  /reloadshops          – shops_data.json neu laden und in DB schreiben
  /shopmapping add      – Externen Shopnamen → interne Shop-ID mappen
  /shopmapping show     – Alle Mappings anzeigen
  /shopmapping remove   – Mapping löschen
  /ch_delivery add      – Shop zur CH-Lieferliste hinzufügen
  /ch_delivery remove   – Shop von CH-Lieferliste entfernen
  /ch_delivery list     – CH-Lieferliste anzeigen
"""
import logging
from datetime import datetime

import discord
from discord.ext import commands
from rapidfuzz import process

from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data
from cogs.server_settings import admin_or_manage_messages, allowed_channel

logger = logging.getLogger(__name__)


class ShopAdminCog(commands.Cog, name="ShopAdmin"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /reloadshops ──────────────────────────────────────────────────────────
    @discord.slash_command(name="reloadshops", description="Reload shop data from JSON file (Admin/Mod)", description_localizations={"de": "Shop-Daten aus JSON-Datei neu laden (Admin/Mod)"})
    @admin_or_manage_messages()
    async def reloadshops(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ctx.defer(ephemeral=True)
        try:
            shop_data = await load_shop_data(self.bot)
            for sid, sd in shop_data.items():
                await execute_db(
                    self.bot,
                    """INSERT INTO shops (id, name, country, url)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           name=excluded.name,
                           country=excluded.country,
                           url=excluded.url""",
                    (sid, sd.get("name"), sd.get("country"), sd.get("url")),
                    commit=True,
                )
            await ctx.respond(l10n.get("reloadshops_success", lang), ephemeral=True)
            logger.info(f"🏪 Shop-Daten neu geladen von {ctx.author.id}: {len(shop_data)} Shops")
        except Exception as e:
            logger.error(f"❌ reloadshops error: {e}")
            await ctx.respond(l10n.get("general_error", lang), ephemeral=True)

    # ── /shopmapping ──────────────────────────────────────────────────────────
    shopmapping = discord.SlashCommandGroup(
        name="shopmapping",
        description="Manage shop name mappings for Google Sheets imports",
    )

    @shopmapping.command(name="add", description="Add an external shop name → shop ID mapping")
    @admin_or_manage_messages()
    async def shopmapping_add(
        self,
        ctx: discord.ApplicationContext,
        external: discord.Option(str, "External shop name (as it appears in Google Sheets)", description_localizations={"de": 'Externer Shop-Name (wie im Google Sheet)', "en-US": 'External shop name (as it appears in Google Sheets)'}, required=True),
        shop_id: discord.Option(str, "Internal shop ID", description_localizations={"de": 'Interne Shop-ID', "en-US": 'Internal shop ID'}, required=True),
    ):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)
        try:
            sid = int(shop_id)
        except ValueError:
            await ctx.respond(l10n.get("shopmapping_add_invalid_id", lang), ephemeral=True)
            return
        if str(sid) not in shop_data:
            await ctx.respond(l10n.get("shopmapping_add_invalid_id", lang), ephemeral=True)
            return
        try:
            await execute_db(
                self.bot,
                "INSERT OR REPLACE INTO shop_name_mappings (external_name, shop_id) VALUES (?, ?)",
                (external.strip(), sid), commit=True,
            )
            shop_name = shop_data[str(sid)].get("name", str(sid))
            await ctx.respond(
                l10n.get("shopmapping_add_success", lang, external=external, id=sid, shop=shop_name),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"❌ shopmapping_add error: {e}")
            await ctx.respond(l10n.get("shopmapping_add_error", lang), ephemeral=True)

    @shopmapping.command(name="show", description="Show all current shop name mappings")
    @admin_or_manage_messages()
    async def shopmapping_show(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(
            self.bot,
            "SELECT external_name, shop_id FROM shop_name_mappings ORDER BY external_name",
            fetch=True,
        )
        if not rows:
            await ctx.respond(l10n.get("shopmapping_show_none", lang), ephemeral=True)
            return
        shop_data = await load_shop_data(self.bot)
        lines     = [l10n.get("shopmapping_show_header", lang)]
        for r in rows:
            shop_name = shop_data.get(str(r["shop_id"]), {}).get("name", str(r["shop_id"]))
            lines.append(l10n.get("shopmapping_show_entry", lang,
                                   external=r["external_name"], id=r["shop_id"], shop=shop_name))
        await ctx.respond("\n".join(lines), ephemeral=True)

    @shopmapping.command(name="remove", description="Remove an external shop name mapping")
    @admin_or_manage_messages()
    async def shopmapping_remove(
        self,
        ctx: discord.ApplicationContext,
        external: discord.Option(str, "External shop name to remove", description_localizations={"de": 'Zu entfernender externer Shop-Name', "en-US": 'External shop name to remove'}, required=True),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rc   = await execute_db(
            self.bot,
            "DELETE FROM shop_name_mappings WHERE external_name=?",
            (external.strip(),), commit=True,
        )
        key = "shopmapping_remove_success" if rc else "shopmapping_remove_none"
        await ctx.respond(l10n.get(key, lang, external=external), ephemeral=True)

    # ── /ch_delivery ──────────────────────────────────────────────────────────
    ch_delivery = discord.SlashCommandGroup(
        name="ch_delivery",
        description="Manage shops delivering to Switzerland",
    )

    @ch_delivery.command(name="add", description="Add a shop to the CH delivery list")
    @allowed_channel()
    async def ch_delivery_add(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop name", description_localizations={"de": 'Shop-Name', "en-US": 'Shop name'}, required=True),
    ):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)
        shop_names = {sid: sd["name"] for sid, sd in shop_data.items()}
        matches    = process.extract(shop, shop_names.values(), limit=3)
        best       = next((m for m in matches if m[1] > 75), None)
        if not best:
            await ctx.respond(l10n.get("shop_not_found", lang), ephemeral=True)
            return
        shop_name = best[0]
        shop_id   = next(sid for sid, name in shop_names.items() if name == shop_name)
        # ch_delivery_shops wird zentral in utils/db.py:init_db() angelegt.
        rc = await execute_db(
            self.bot,
            "INSERT OR IGNORE INTO ch_delivery_shops (shop_id, added_by, added_at) VALUES (?, ?, ?)",
            (shop_id, str(ctx.author.id), datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
            commit=True,
        )
        key = "ch_delivery_add_success" if rc else "ch_delivery_exists"
        await ctx.respond(l10n.get(key, lang, shop=shop_name), ephemeral=True)

    @ch_delivery.command(name="remove", description="Remove a shop from the CH delivery list")
    @allowed_channel()
    async def ch_delivery_remove(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop name", description_localizations={"de": 'Shop-Name', "en-US": 'Shop name'}, required=True),
    ):
        lang      = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        shop_data = await load_shop_data(self.bot)
        shop_names = {sid: sd["name"] for sid, sd in shop_data.items()}
        matches    = process.extract(shop, shop_names.values(), limit=3)
        best       = next((m for m in matches if m[1] > 75), None)
        if not best:
            await ctx.respond(l10n.get("shop_not_found", lang), ephemeral=True)
            return
        shop_name = best[0]
        shop_id   = next(sid for sid, name in shop_names.items() if name == shop_name)

        # Prüfen ob Eintrag existiert und wer ihn hinzugefügt hat
        existing = await execute_db(
            self.bot,
            "SELECT added_by FROM ch_delivery_shops WHERE shop_id=?",
            (shop_id,), fetch=True,
        )
        if not existing:
            await ctx.respond(l10n.get("ch_delivery_not_found", lang, shop=shop_name), ephemeral=True)
            return

        is_admin = ctx.guild and (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_messages
        )
        is_owner = existing[0]["added_by"] == str(ctx.author.id)

        if not is_admin and not is_owner:
            await ctx.respond(l10n.get("ch_delivery_remove_no_permission", lang, shop=shop_name), ephemeral=True)
            return

        await execute_db(
            self.bot,
            "DELETE FROM ch_delivery_shops WHERE shop_id=?",
            (shop_id,), commit=True,
        )
        await ctx.respond(l10n.get("ch_delivery_remove_success", lang, shop=shop_name), ephemeral=True)

    @ch_delivery.command(name="list", description="Show all shops delivering to Switzerland")
    @allowed_channel()
    async def ch_delivery_list(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            rows = await execute_db(
                self.bot,
                "SELECT shop_id, added_by, added_at FROM ch_delivery_shops ORDER BY added_at DESC",
                fetch=True,
            )
        except Exception:
            rows = []
        if not rows:
            await ctx.respond(l10n.get("ch_delivery_empty", lang), ephemeral=True)
            return
        shop_data = await load_shop_data(self.bot)
        lines = [l10n.get("ch_delivery_header", lang)]
        for r in rows:
            shop_name = shop_data.get(str(r["shop_id"]), {}).get("name", r["shop_id"])
            lines.append(l10n.get(
                "ch_delivery_entry", lang,
                shop=shop_name, user=f"<@{r['added_by']}>", timestamp=r["added_at"],
            ))
        await ctx.respond("\n".join(lines), ephemeral=True)


    # ── /shopurl ──────────────────────────────────────────────────────────────
    shopurl = discord.SlashCommandGroup(
        name="shopurl",
        description="Shop-URL manuell überschreiben (wenn API falsche URL liefert)",
    )

    @shopurl.command(name="set", description="Manuelle URL für einen Shop setzen")
    @admin_or_manage_messages()
    async def shopurl_set(
        self,
        ctx: discord.ApplicationContext,
        shop_id: discord.Option(str, "Interne Shop-ID (z.B. 2 für ANTSTORE)", description_localizations={"de": 'Interne Shop-ID (z.B. 2 für ANTSTORE)', "en-US": 'Internal shop ID (e.g. 2 for ANTSTORE)'}, required=True),
        url: discord.Option(str, "Korrekte Shop-URL (z.B. https://antstore.net)", description_localizations={"de": 'Korrekte Shop-URL (z.B. https://antstore.net)', "en-US": 'Correct shop URL (e.g. https://antstore.net)'}, required=True),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            sid = int(shop_id)
        except ValueError:
            await ctx.respond(l10n.get("shopurl_not_found", lang, id=shop_id), ephemeral=True)
            return
        shop_data = await load_shop_data(self.bot)
        if str(sid) not in shop_data:
            await ctx.respond(l10n.get("shopurl_not_found", lang, id=sid), ephemeral=True)
            return
        clean_url = url.strip()
        if clean_url and not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url
        await execute_db(
            self.bot,
            "UPDATE shops SET url_override=? WHERE id=?",
            (clean_url, sid), commit=True,
        )
        shop_name = shop_data[str(sid)].get("name", str(sid))
        await ctx.respond(
            l10n.get("shopurl_set_success", lang, shop=shop_name, id=sid, url=clean_url),
            ephemeral=True,
        )
        logger.info(f"🏪 shopurl_set: Shop {sid} ({shop_name}) → {url.strip()} von {ctx.author.id}")

    @shopurl.command(name="clear", description="Manuelle URL entfernen (API-URL wird wieder genutzt)")
    @admin_or_manage_messages()
    async def shopurl_clear(
        self,
        ctx: discord.ApplicationContext,
        shop_id: discord.Option(str, "Interne Shop-ID", description_localizations={"de": 'Interne Shop-ID', "en-US": 'Internal shop ID'}, required=True),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            sid = int(shop_id)
        except ValueError:
            await ctx.respond(l10n.get("shopurl_not_found", lang, id=shop_id), ephemeral=True)
            return
        rc = await execute_db(
            self.bot,
            "UPDATE shops SET url_override=NULL WHERE id=? AND url_override IS NOT NULL",
            (sid,), commit=True,
        )
        shop_data = await load_shop_data(self.bot)
        shop_name = shop_data.get(str(sid), {}).get("name", str(sid))
        key = "shopurl_clear_success" if rc else "shopurl_clear_none"
        await ctx.respond(l10n.get(key, lang, shop=shop_name, id=sid), ephemeral=True)

    @shopurl.command(name="list", description="Alle manuellen URL-Overrides anzeigen")
    @admin_or_manage_messages()
    async def shopurl_list(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(
            self.bot,
            "SELECT id, name, url, url_override FROM shops WHERE url_override IS NOT NULL ORDER BY name",
            fetch=True,
        )
        if not rows:
            await ctx.respond(l10n.get("shopurl_list_none", lang), ephemeral=True)
            return
        lines = [l10n.get("shopurl_list_header", lang)]
        for r in rows:
            lines.append(l10n.get(
                "shopurl_list_entry", lang,
                shop=r["name"], id=r["id"], url=r["url_override"],
            ))
        await ctx.respond("\n".join(lines), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(ShopAdminCog(bot))
