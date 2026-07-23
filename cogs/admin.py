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
cogs/admin.py - Admin-Befehle als discord.py Cog.

Slash Commands (nur Admins / Manage-Messages):
  /status   - Anzahl Bewertungen / verarbeitet / ausstehend
  /pending  - Liste ausstehender Nachrichten
  /test     - KI-Parser testen ohne Sheet-Eintrag
  /rescan   - Letzte N Tage manuell neu abgleichen
  /export   - Sheet-Rohdaten als JSON exportieren (ephemeral)
  /export user:<id> - Alle gespeicherten Daten zu einem User als JSON per DM
"""
import io
import json
import logging
from datetime import datetime

import discord
from discord.ext import commands

from config import MAPPING_FILE, SCAN_DAYS, REVIEW_CHANNEL_ID
from utils.sheet import sheet
from utils.tracking import (
    get_all_pending, get_all_tracking, pending_count, tracking_count,
    get_tracking, remove_pending,
)
from utils.ai_parser import parse_with_ai
from cogs.server_settings import admin_or_manage_messages, allowed_channel
from utils.localization import l10n, get_user_lang
from utils.embeds import send_embeds, ADMIN_COLOR

logger = logging.getLogger(__name__)


class AdminCog(commands.Cog, name="Admin"):
    """Verwaltungsbefehle für Bot-Admins."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(name="status", description="🔒 [Admin] Show review bot status", description_localizations={"de": "🔒 [Admin] Review-Bot-Status anzeigen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_status(self, ctx: discord.ApplicationContext):
        """Zeigt Bewertungsanzahl, verarbeitete und ausstehende Nachrichten."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            rows  = sheet.row_count - 1
            pend  = await pending_count(self.bot)
            track = await tracking_count(self.bot)
            await ctx.respond(
                l10n.get("review_status", lang, rows=rows, tracked=track, pending=pend),
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"❌ status error: {e}")
            await ctx.respond(l10n.get("admin_error", lang, error=e), ephemeral=True)

    @discord.slash_command(name="pending", description="🔒 [Admin] List pending messages", description_localizations={"de": "🔒 [Admin] Ausstehende Nachrichten auflisten"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_pending(self, ctx: discord.ApplicationContext):
        """Listet ausstehende Nachrichten auf."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        pending = await get_all_pending(self.bot)
        if not pending:
            await ctx.respond(l10n.get("review_pending_none", lang), ephemeral=True)
            return

        lines = [
            "* `" + mid + "` - " + v["reason"]
            + (" -> `" + v["identifier"] + "`" if v.get("identifier") else "")
            for mid, v in list(pending.items())[:10]
        ]
        extra = (
            "\n" + l10n.get("admin_pending_more", lang, count=len(pending) - 10)
            if len(pending) > 10 else ""
        )
        header   = l10n.get("review_pending_header", lang, count=len(pending))
        csv_note = l10n.get("review_pending_fill_csv", lang, file=MAPPING_FILE)
        await send_embeds(
            ctx,
            header + "\n" + "\n".join(lines) + extra + "\n\n" + csv_note,
            ephemeral=True, color=ADMIN_COLOR,
        )

    @discord.slash_command(name="test", description="🔒 [Admin] Test AI parser without writing to sheet", description_localizations={"de": "🔒 [Admin] KI-Parser ohne Sheet-Eintrag testen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_test(
        self,
        ctx: discord.ApplicationContext,
        text: discord.Option(str, "Message text to parse", description_localizations={"de": 'Zu parsender Bewertungstext', "en-US": 'Message text to parse'}, required=True),
    ):
        """Testet den KI-Parser ohne Sheet-Eintrag."""
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            parsed = parse_with_ai(text, "TEST-SHOP", datetime.now().strftime("%d.%m.%Y"))
            await ctx.respond(
                "```json\n" + json.dumps(parsed, ensure_ascii=False, indent=2) + "\n```",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"❌ test error: {e}")
            await ctx.respond(l10n.get("admin_error", lang, error=e), ephemeral=True)

    @discord.slash_command(name="rescan", description="🔒 [Admin] Manually re-reconcile last N days", description_localizations={"de": "🔒 [Admin] Letzte N Tage manuell neu abgleichen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_rescan(self, ctx: discord.ApplicationContext):
        """Gleicht die letzten N Tage manuell erneut ab."""
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)

        reviews_cog = self.bot.cogs.get("Reviews")
        if not reviews_cog:
            await ctx.respond(l10n.get("admin_rescan_cog_missing", lang), ephemeral=True)
            return

        channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
        if not channel:
            await ctx.respond(l10n.get("admin_rescan_channel_missing", lang), ephemeral=True)
            return

        await ctx.followup.send(
            l10n.get("admin_rescan_scanning", lang, days=SCAN_DAYS), ephemeral=True
        )
        mapped, written = await reviews_cog._reconcile_scan(channel)
        all_pending = await get_all_pending(self.bot)
        await ctx.followup.send(
            l10n.get(
                "admin_rescan_result", lang,
                mapped=mapped, written=written, pending=len(all_pending),
            ),
            ephemeral=True,
        )

    @discord.slash_command(name="export", description="🔒 [Admin] Export DB data as JSON", description_localizations={"de": "🔒 [Admin] DB-Daten als JSON exportieren"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_export(
        self,
        ctx: discord.ApplicationContext,
        user_id: discord.Option(str, "Discord-ID des Users (leer = Alle-User-Export)", description_localizations={"de": 'Discord-ID des Users (leer = Alle-User-Export)', "en-US": 'Discord user ID (empty = export all users)'}, required=False, default=None),
    ):
        """DB-Export aller User ODER einzelner User-Daten per DM."""
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)

        # ── User-Daten-Export ─────────────────────────────────────────────────
        if user_id:
            try:
                uid = str(int(user_id))
            except ValueError:
                await ctx.followup.send(l10n.get("invalid_ids", lang), ephemeral=True)
                return
            try:
                from utils.db import execute_db
                data: dict = {"user_id": uid, "exported_at": datetime.utcnow().isoformat() + "Z"}

                data["settings"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_settings WHERE user_id=?", (uid,), fetch=True)]

                data["notifications"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM notifications WHERE user_id=?", (uid,), fetch=True)]

                data["seen_products"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_seen_products WHERE user_id=?", (uid,), fetch=True)]

                data["shop_blacklist"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_shop_blacklist WHERE user_id=?", (uid,), fetch=True)]

                data["price_tracking"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_price_tracking WHERE user_id=?", (uid,), fetch=True)]

                data["server_mappings"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM server_user_mappings WHERE user_id=?", (uid,), fetch=True)]

                data["ch_delivery_added"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM ch_delivery_shops WHERE added_by=?", (uid,), fetch=True)]

                try:
                    ai_rows = await execute_db(
                        self.bot,
                        "SELECT date, cost_usd FROM ai_chat_budget WHERE user_id=? ORDER BY date DESC LIMIT 30",
                        (int(uid),), fetch=True,
                    )
                    data["ai_budget_last30"] = [dict(r) for r in ai_rows]
                except Exception:
                    data["ai_budget_last30"] = []

                data["species_watch"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_species_watch WHERE user_id=?", (uid,), fetch=True)]

                data["species_watch_seen"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_species_watch_seen WHERE user_id=?", (uid,), fetch=True)]

                data["digest_subscription"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM digest_subscribers WHERE user_id=?", (uid,), fetch=True)]

                data["achievements"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM achievements WHERE user_id=?", (uid,), fetch=True)]

                data["events"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM user_events WHERE user_id=?", (uid,), fetch=True)]

                data["command_log"] = [dict(r) for r in await execute_db(
                    self.bot, "SELECT * FROM command_log WHERE user_id=?", (uid,), fetch=True)]

                try:
                    ai_hist = await execute_db(
                        self.bot,
                        "SELECT message_id, channel_id, created_at, expires_at, history_json "
                        "FROM ai_chat_history WHERE user_id=?",
                        (int(uid),), fetch=True,
                    )
                    data["ai_chat_history"] = [dict(r) for r in ai_hist]
                except Exception:
                    data["ai_chat_history"] = []

                # Geposteten Rabattcodes (nach aktuellem Discord-Username des Users)
                try:
                    _u     = await self.bot.fetch_user(int(uid))
                    _uname = getattr(_u, "name", None)
                    data["discount_codes_posted"] = [dict(r) for r in await execute_db(
                        self.bot, "SELECT * FROM discount_codes WHERE author=?", (_uname,), fetch=True)] if _uname else []
                except Exception:
                    data["discount_codes_posted"] = []

                payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
                buf = io.BytesIO(payload.encode("utf-8"))
                buf.seek(0)
                file = discord.File(buf, filename=f"user_data_{uid}.json")

                try:
                    admin_user = await self.bot.fetch_user(ctx.author.id)
                    await admin_user.send(
                        l10n.get("data_export_success", lang),
                        file=file,
                    )
                    await ctx.followup.send(l10n.get("admin_dm_sent", lang), ephemeral=True)
                except discord.Forbidden:
                    await ctx.followup.send(l10n.get("data_export_error", lang), ephemeral=True)
            except Exception as e:
                logger.error(f"❌ user data export error: {e}")
                await ctx.followup.send(l10n.get("admin_error", lang, error=e), ephemeral=True)
            return

        # ── DB-Export (Standard, alle User) ──────────────────────────────────
        try:
            from utils.db import execute_db
            tables = [
                "user_settings",
                "notifications",
                "user_shop_blacklist",
                "user_seen_products",
                "user_price_tracking",
                "user_species_watch",
                "user_species_watch_seen",
                "server_user_mappings",
                "ch_delivery_shops",
                "ai_chat_budget",
                "ai_chat_history",
                "digest_subscribers",
                "achievements",
                "user_events",
                "command_log",
                "discount_codes",
                "review_tracking",
                "review_pending",
                "server_settings",
            ]
            export: dict = {"exported_at": datetime.utcnow().isoformat() + "Z", "tables": {}}
            for table in tables:
                try:
                    rows = await execute_db(
                        self.bot, f"SELECT * FROM {table} LIMIT 500", fetch=True
                    )
                    export["tables"][table] = [dict(r) for r in rows]
                except Exception:
                    export["tables"][table] = None  # Tabelle existiert nicht

            payload = json.dumps(export, ensure_ascii=False, indent=2, default=str)
            buf = io.BytesIO(payload.encode("utf-8"))
            buf.seek(0)
            file = discord.File(buf, filename="db_export.json")
            total = sum(len(v) for v in export["tables"].values() if v is not None)
            await ctx.followup.send(
                l10n.get("admin_export_header", lang, rows=total),
                file=file,
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"❌ export error: {e}")
            await ctx.followup.send(l10n.get("admin_error", lang, error=e), ephemeral=True)


    @discord.slash_command(
        name="reprocess",
        description="🔒 [Admin] Re-process review message(s) by ID – separate multiple IDs with spaces", description_localizations={"de": "🔒 [Admin] Bewertungsnachricht(en) per ID neu verarbeiten (mehrere per Leerzeichen)"},
    )
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_reprocess(
        self,
        ctx: discord.ApplicationContext,
        message_ids: discord.Option(
            str,
            "Eine oder mehrere Message-IDs (leerzeichen- oder kommagetrennt)", description_localizations={"de": 'Eine oder mehrere Message-IDs (leerzeichen- oder kommagetrennt)', "en-US": 'One or more message IDs (space- or comma-separated)'},
            required=True,
        ),
    ):
        """Verarbeitet Bewertungsnachrichten neu. Mehrere IDs werden zu einer Review zusammengeführt."""
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)

        reviews_cog = self.bot.cogs.get("Reviews")
        if not reviews_cog:
            await ctx.respond(l10n.get("admin_rescan_cog_missing", lang), ephemeral=True)
            return

        channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
        if not channel:
            await ctx.respond(l10n.get("admin_rescan_channel_missing", lang), ephemeral=True)
            return

        # IDs parsen (Leerzeichen oder Komma als Trenner)
        import re as _re
        raw_ids = _re.split(r"[\s,]+", message_ids.strip())
        raw_ids = [x for x in raw_ids if x]

        # Alle Nachrichten laden (Reihenfolge: älteste zuerst)
        messages: list[discord.Message] = []
        for mid in raw_ids:
            try:
                msg = await channel.fetch_message(int(mid))
                messages.append(msg)
            except (discord.NotFound, discord.HTTPException, ValueError):
                await ctx.followup.send(
                    l10n.get("admin_reprocess_not_found", lang, mid=mid), ephemeral=True
                )
                return

        messages.sort(key=lambda m: m.created_at)
        anchor = messages[0]
        extra  = messages[1:]
        combined = "\n".join(m.content for m in messages) if extra else None
        anchor_id = str(anchor.id)

        try:
            existing_row = await get_tracking(self.bot, anchor_id)
            await reviews_cog._process(
                anchor,
                is_edit=(existing_row is not None),
                combined_content=combined,
            )
            await remove_pending(self.bot, anchor_id)
            await reviews_cog._clean_react(anchor, "🟡", "🔴", add="🟢")
            for m in extra:
                try:
                    await m.add_reaction("🟢")
                except Exception:
                    pass
            from utils.shop import resolve_shop
            try:
                shop = resolve_shop(combined or anchor.content, anchor.guild)
            except Exception:
                shop = "?"
            ids_str = " + ".join(str(m.id) for m in messages)
            await ctx.followup.send(
                l10n.get("admin_reprocess_success", lang, mid=ids_str, shop=shop),
                ephemeral=True,
            )
            logger.info(f"♻️  Reprocess OK: {ids_str} → {shop}")
        except Exception as e:
            logger.error(f"❌ reprocess error {raw_ids}: {e}")
            await ctx.followup.send(l10n.get("admin_error", lang, error=e), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
