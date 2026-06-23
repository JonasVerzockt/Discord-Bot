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
from cogs.server_settings import admin_or_manage_messages

logger = logging.getLogger(__name__)


class ShopAdminCog(commands.Cog, name="ShopAdmin"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── /reloadshops ───────────────────────────────────────────────────────────
    @discord.slash_command(name="reloadshops", description="Reload shop data from JSON file (Admin/Mod)")
    @admin_or_manage_messages()
    async def reloadshops(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ctx.defer(ephemeral=True)
        try:
            shop_data = await load_shop_data(self.bot)
            for sid, sd in shop_data.items():
                await execute_db(
                    self.bot,
                    "INSERT OR REPLACE INTO shops (id, name, country, url) VALUES (?, ?, ?, ?)",
                    (sid, sd.get("name"), sd.get("country"), sd.get("url")),
                    commit=True,
                )
            await ctx.respond(l10n.get("reloadshops_success", lang), ephemeral=True)
            logger.info(f"Shop-Daten neu geladen von {ctx.author.id}: {len(shop_data)} Shops")
        except Exception as e:
            logger.error(f"reloadshops error: {e}")
            await ctx.respond(l10n.get("general_error", lang), ephemeral=True)

    # ── /shopmapping ───────────────────────────────────────────────────────────
    shopmapping = discord.SlashCommandGroup(
        name="shopmapping",
        description="Manage shop name mappings for Google Sheets imports",
    )

    @shopmapping.command(name="add", description="Add an external shop name → shop ID mapping")
    @admin_or_manage_messages()
    async def shopmapping_add(
        self,
        ctx: discord.ApplicationContext,
        external: discord.Option(str, "External shop name (as it appears in Google Sheets)", required=True),
        shop_id: discord.Option(str, "Internal shop ID", required=True),
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
            logger.error(f"shopmapping_add error: {e}")
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
        external: discord.Option(str, "External shop name to remove", required=True),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rc   = await execute_db(
            self.bot,
            "DELETE FROM shop_name_mappings WHERE external_name=?",
            (external.strip(),), commit=True,
        )
        key = "shopmapping_remove_success" if rc else "shopmapping_remove_none"
        await ctx.respond(l10n.get(key, lang, external=external), ephemeral=True)

    # ── /ch_delivery ───────────────────────────────────────────────────────────
    ch_delivery = discord.SlashCommandGroup(
        name="ch_delivery",
        description="Manage shops delivering to Switzerland",
    )

    @ch_delivery.command(name="add", description="Add a shop to the CH delivery list")
    @admin_or_manage_messages()
    async def ch_delivery_add(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop name", required=True),
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

        # Prüfen ob schon vorhanden
        existing = await execute_db(
            self.bot,
            "SELECT 1 FROM ch_delivery_shops WHERE shop_id=?",
            (shop_id,), fetch=True,
        ) if False else []   # Tabelle optional – via migrate
        # Tabelle anlegen falls nicht vorhanden
        await execute_db(
            self.bot,
            """CREATE TABLE IF NOT EXISTS ch_delivery_shops (
               shop_id   TEXT PRIMARY KEY,
               added_by  TEXT,
               added_at  TEXT
            )""",
            commit=True,
        )
        rc = await execute_db(
            self.bot,
            "INSERT OR IGNORE INTO ch_delivery_shops (shop_id, added_by, added_at) VALUES (?, ?, ?)",
            (shop_id, str(ctx.author.id), datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
            commit=True,
        )
        key = "ch_delivery_add_success" if rc else "ch_delivery_exists"
        await ctx.respond(l10n.get(key, lang, shop=shop_name), ephemeral=True)

    @ch_delivery.command(name="remove", description="Remove a shop from the CH delivery list")
    @admin_or_manage_messages()
    async def ch_delivery_remove(
        self,
        ctx: discord.ApplicationContext,
        shop: discord.Option(str, "Shop name", required=True),
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
        rc = await execute_db(
            self.bot,
            "DELETE FROM ch_delivery_shops WHERE shop_id=?",
            (shop_id,), commit=True,
        )
        key = "ch_delivery_remove_success" if rc else "ch_delivery_not_found"
        await ctx.respond(l10n.get(key, lang, shop=shop_name), ephemeral=True)

    @ch_delivery.command(name="list", description="Show all shops delivering to Switzerland")
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


async def setup(bot: discord.Bot):
    await bot.add_cog(ShopAdminCog(bot))
