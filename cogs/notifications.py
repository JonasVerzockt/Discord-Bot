"""
cogs/notifications.py – Benachrichtigungs-Cog für den AAM Discord Bot.

Slash Commands:
  /notification          – Art/Gattung + Region überwachen
  /delete_notifications  – Eigene Benachrichtigungen löschen
  /history               – Benachrichtigungshistorie anzeigen
  /testnotification      – Test-PN senden

Interne Helfer:
  trigger_availability_check()  – Prüft Verfügbarkeit und sendet PN
  ask_for_feedback()            – Fragt nach Reaction-Feedback (👍 / 🔄)
  handle_dm_failure()           – Fallback auf Server-Kanal wenn DM blockiert
"""
import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.availability import (
    check_availability_for_species,
    species_exists,
    expand_regions,
    load_shop_data,
    format_rating,
    split_availability_messages,
)
from cogs.server_settings import allowed_channel

logger = logging.getLogger(__name__)


class NotificationsCog(commands.Cog, name="Notifications"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── Öffentliche Helfer (werden auch von tasks.py genutzt) ─────────────────

    async def trigger_availability_check(
        self,
        user_id: str,
        species: str,
        regions: str,
        ch_mode: bool = False,
        excluded_species_list: set | None = None,
    ) -> bool | None:
        """
        Prüft Verfügbarkeit und sendet PN.
        Returns: True=gesendet, False=nicht verfügbar, None=Fehler
        """
        if excluded_species_list is None:
            excluded_species_list = set()
        try:
            row = await execute_db(
                self.bot,
                "SELECT server_id FROM notifications WHERE user_id=? AND species=? AND regions=?",
                (user_id, species, regions),
                fetch=True,
            )
            server_id = row[0]["server_id"] if row and row[0]["server_id"] else None
            lang      = await get_user_lang(self.bot, user_id, server_id)

            if ch_mode:
                ch_rows = await execute_db(self.bot, "SELECT shop_id FROM ch_delivery_shops", fetch=True)
                ch_shops    = {str(r["shop_id"]) for r in ch_rows}
                shop_data   = await load_shop_data(self.bot)
                auto_ch     = {sid for sid, sd in shop_data.items() if sd.get("country", "").lower() == "ch"}
                ch_shops   |= auto_ch
                regions_list = ["ch"]
            else:
                regions_list = await expand_regions(self.bot, regions.split(","))

            available = await check_availability_for_species(
                self.bot, species, regions_list,
                user_id=user_id, ch_mode=ch_mode,
                ch_shops=ch_shops if ch_mode else None,
                excluded_species_list=excluded_species_list,
            )
            if not available:
                return False

            # Bereits gesehene Produkte herausfiltern
            seen_rows = await execute_db(
                self.bot,
                "SELECT product_id FROM user_seen_products WHERE user_id=?",
                (user_id,), fetch=True,
            )
            seen_ids     = {r["product_id"] for r in seen_rows}
            new_products = [p for p in available if str(p["id"]) not in seen_ids]
            if not new_products:
                return False

            try:
                user = await self.bot.fetch_user(int(user_id))
            except Exception as e:
                logger.error(f"User {user_id} nicht abrufbar: {e}")
                return None

            # Nach Bewertung sortieren
            sorted_products = sorted(
                new_products,
                key=lambda p: (p.get("rating") is None, -(p.get("rating") or 0),
                               p.get("shop_name", "").lower()),
            )
            header  = l10n.get("availability_header", lang, species=species)
            entries = [header] + [
                l10n.get(
                    "availability_entry", lang,
                    species=p["species"], shop=p["shop_name"],
                    min_price=p["min_price"], max_price=p["max_price"],
                    currency=p["currency_iso"], product_url=p["antcheck_url"],
                    shop_url=p["shop_url"] or "N/A",
                    rating=format_rating(p.get("rating")),
                )
                for p in sorted_products
            ]
            chunks = split_availability_messages(entries)

            try:
                for chunk in chunks:
                    await user.send(chunk)
            except discord.Forbidden:
                await self._handle_dm_failure(user_id, species, regions, lang)
                return None

            # Gesehene Produkte aktualisieren
            for p in new_products:
                await execute_db(
                    self.bot,
                    "INSERT OR IGNORE INTO user_seen_products (user_id, product_id) VALUES (?, ?)",
                    (user_id, str(p["id"])), commit=True,
                )
            current_ids = {str(p["id"]) for p in available}
            for pid in seen_ids - current_ids:
                await execute_db(
                    self.bot,
                    "DELETE FROM user_seen_products WHERE user_id=? AND product_id=?",
                    (user_id, pid), commit=True,
                )

            await execute_db(
                self.bot,
                """UPDATE notifications SET status='completed', notified_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND species=? AND regions=?""",
                (user_id, species, regions), commit=True,
            )
            await self._ask_for_feedback(user, user_id, species, regions)
            return True

        except Exception as e:
            logger.error(f"trigger_availability_check error ({user_id}, {species}): {e}", exc_info=True)
            await execute_db(
                self.bot,
                "UPDATE notifications SET status='failed' WHERE user_id=? AND species=? AND regions=?",
                (user_id, species, regions), commit=True,
            )
            return None

    async def _handle_dm_failure(self, user_id: str, species: str, regions: str, lang: str):
        """Benachrichtigt User im Server-Kanal wenn DMs blockiert sind."""
        try:
            servers = await execute_db(
                self.bot,
                "SELECT DISTINCT server_id FROM server_user_mappings WHERE user_id=?",
                (user_id,), fetch=True,
            )
            regions_list = [r.strip() for r in regions.split(",")]
            for row in servers:
                server_id  = row["server_id"]
                ch_row     = await execute_db(
                    self.bot,
                    "SELECT channel_id FROM server_settings WHERE server_id=?",
                    (server_id,), fetch=True,
                )
                channel_id = ch_row[0]["channel_id"] if ch_row else None
                channel    = self.bot.get_channel(channel_id) if channel_id else None
                if not channel:
                    guild   = self.bot.get_guild(server_id)
                    channel = guild.system_channel if guild else None
                if channel:
                    try:
                        await channel.send(
                            f"<@{user_id}>, {l10n.get('dm_failed', lang)}\n"
                            f"**Art:** {species}\n**Regionen:** {', '.join(regions_list)}"
                        )
                    except discord.HTTPException:
                        pass
        except Exception as e:
            logger.error(f"handle_dm_failure error: {e}")

    async def _ask_for_feedback(self, user: discord.User, user_id: str, species: str, regions: str):
        """Sendet Feedback-Frage nach erfolgreicher Benachrichtigung."""
        row = await execute_db(
            self.bot,
            "SELECT server_id FROM notifications WHERE user_id=? AND species=? AND regions=?",
            (user_id, species, regions), fetch=True,
        )
        server_id = row[0]["server_id"] if row and row[0]["server_id"] else None
        lang      = await get_user_lang(self.bot, user_id, server_id)

        question = await user.send(l10n.get("feedback_question", lang))
        await question.add_reaction("👍")
        await question.add_reaction("🔄")

        pending_until = datetime.utcnow() + timedelta(hours=48)
        await execute_db(
            self.bot,
            """UPDATE notifications SET status='pending_feedback', pending_feedback_until=?
               WHERE user_id=? AND species=? AND regions=? AND status='completed'""",
            (pending_until.strftime("%Y-%m-%d %H:%M:%S"), user_id, species, regions),
            commit=True,
        )

        def check(reaction, reactor):
            return (
                reactor.id == int(user_id)
                and reaction.message.id == question.id
                and str(reaction.emoji) in ["👍", "🔄"]
            )
        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=48 * 3600, check=check)
            if str(reaction.emoji) == "👍":
                await execute_db(
                    self.bot,
                    "DELETE FROM user_seen_products WHERE user_id=?",
                    (user_id,), commit=True,
                )
                await execute_db(
                    self.bot,
                    """UPDATE notifications SET status='completed', pending_feedback_until=NULL
                       WHERE user_id=? AND species=? AND regions=?""",
                    (user_id, species, regions), commit=True,
                )
                await user.send(l10n.get("feedback_positive_ack", lang))
            else:
                await execute_db(
                    self.bot,
                    """UPDATE notifications SET status='active', pending_feedback_until=NULL
                       WHERE user_id=? AND species=? AND regions=?""",
                    (user_id, species, regions), commit=True,
                )
                await user.send(l10n.get("feedback_continue_ack", lang))
        except asyncio.TimeoutError:
            await execute_db(
                self.bot,
                """UPDATE notifications SET status='expired', pending_feedback_until=NULL
                   WHERE user_id=? AND species=? AND regions=?""",
                (user_id, species, regions), commit=True,
            )
            try:
                await user.send(l10n.get("feedback_timeout", lang, species=species, regions=regions))
            except Exception:
                pass

    # ── Slash Commands ─────────────────────────────────────────────────────────

    @discord.slash_command(name="notification", description="Set up availability notification for an ant species or genus")
    @allowed_channel()
    async def notification(
        self,
        ctx: discord.ApplicationContext,
        species: discord.Option(str, "Specific species (e.g. Messor barbarus)", required=False, default=None),
        genus: discord.Option(str, "Genus (e.g. Messor) – notifies for ALL species in this genus", required=False, default=None),
        exclude_species: discord.Option(str, "Comma-separated species to exclude (genus only)", required=False, default=None),
        regions: discord.Option(str, "Regions comma-separated (e.g. de,at,eu)", required=False, default=None),
        swiss_only: discord.Option(bool, "Only shops delivering to Switzerland", default=False),
        force: discord.Option(bool, "Force notification even if already active", default=False),
    ):
        server_id = ctx.guild_id
        lang      = await get_user_lang(self.bot, ctx.author.id, server_id)

        if species and genus:
            await ctx.respond(l10n.get("notification_error_both_genus_species", lang), ephemeral=True)
            return
        if not species and not genus:
            await ctx.respond(l10n.get("notification_error_neither_genus_species", lang), ephemeral=True)
            return
        if species and " " not in species:
            await ctx.respond(l10n.get("notification_error_species_format", lang, species=species), ephemeral=True)
            return

        search_term = species or genus
        excluded_str = exclude_species.strip().lower() if (genus and exclude_species) else None

        # Regionen validieren
        shop_data = await load_shop_data(self.bot)
        ch_shops  = None
        if swiss_only:
            ch_rows  = await execute_db(self.bot, "SELECT shop_id FROM ch_delivery_shops", fetch=True) if False else []
            ch_shops = {str(r["shop_id"]) for r in ch_rows}
            ch_shops |= {sid for sid, sd in shop_data.items() if sd.get("country", "").lower() == "ch"}
            valid_regions = ["ch"]
        else:
            if not regions:
                avail = sorted({sd.get("country", "").lower() for sd in shop_data.values() if sd.get("country")})
                await ctx.respond(l10n.get("notification_error_no_region", lang, regions=", ".join(avail)), ephemeral=True)
                return
            expanded = await expand_regions(self.bot, [r.strip().lower() for r in regions.split(",")])
            avail_countries = {sd.get("country", "").lower() for sd in shop_data.values() if sd.get("country")}
            valid_regions = [r for r in expanded if r in avail_countries]
            if not valid_regions:
                await ctx.respond(l10n.get("invalid_regions", lang, regions=", ".join(sorted(avail_countries))), ephemeral=True)
                return

        # Art-Existenz prüfen
        term_exists = await species_exists(self.bot, search_term)
        if not term_exists and not force:
            await ctx.respond(l10n.get("species_or_genus_not_found", lang, term=search_term), ephemeral=True)
            return

        regions_str  = ",".join(valid_regions)
        user_id_str  = str(ctx.author.id)

        await execute_db(
            self.bot,
            """INSERT INTO notifications (user_id, species, regions, status, excluded_species, server_id)
               VALUES (?, ?, ?, 'active', ?, ?)
               ON CONFLICT(user_id, species, regions)
               DO UPDATE SET created_at=CURRENT_TIMESTAMP, status='active',
                   excluded_species=excluded.excluded_species, server_id=excluded.server_id""",
            (user_id_str, search_term, regions_str, excluded_str, server_id),
            commit=True,
        )
        if server_id:
            await execute_db(
                self.bot,
                "INSERT OR IGNORE INTO server_user_mappings (user_id, server_id) VALUES (?, ?)",
                (user_id_str, server_id), commit=True,
            )

        key    = "notification_set_with_exclude" if excluded_str else "notification_set"
        params = {"species": search_term, "regions": regions_str}
        if excluded_str:
            params["excluded"] = excluded_str
        await ctx.respond(l10n.get(key, lang, **params))
        await ctx.followup.send(l10n.get("checking_availability", lang, species=search_term))

        excluded_set = {s.strip().lower() for s in excluded_str.split(",")} if excluded_str else set()
        result = await self.trigger_availability_check(
            user_id_str, search_term, regions_str,
            ch_mode=swiss_only, excluded_species_list=excluded_set,
        )
        if result is True:
            await ctx.followup.send(l10n.get("availability_check_success", lang, species=search_term))
        elif result is False:
            await ctx.followup.send(l10n.get("availability_check_not_found", lang, species=search_term))
        else:
            await ctx.followup.send(l10n.get("availability_check_error", lang, species=search_term))

    @discord.slash_command(name="delete_notifications", description="Delete your notifications by ID")
    @allowed_channel()
    async def delete_notifications(
        self,
        ctx: discord.ApplicationContext,
        ids: discord.Option(str, "Comma-separated notification IDs", required=True),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            id_list = [int(x.strip()) for x in ids.split(",")]
        except ValueError:
            await ctx.respond(l10n.get("invalid_ids", lang), ephemeral=True)
            return

        deleted = []
        for nid in id_list:
            rc = await execute_db(
                self.bot,
                "DELETE FROM notifications WHERE id=? AND user_id=?",
                (nid, str(ctx.author.id)), commit=True,
            )
            if rc:
                deleted.append(str(nid))
                await execute_db(
                    self.bot,
                    "UPDATE global_stats SET value=value+1 WHERE key='deleted_total'",
                    commit=True,
                )
                await execute_db(
                    self.bot,
                    "INSERT OR IGNORE INTO global_stats (key, value) VALUES ('deleted_total', 1)",
                    commit=True,
                )
        if deleted:
            await ctx.respond(l10n.get("deleted_success", lang, ids=", ".join(deleted)), ephemeral=True)
        else:
            await ctx.respond(l10n.get("delete_error", lang), ephemeral=True)

    @discord.slash_command(name="history", description="Show your notification history")
    @allowed_channel()
    async def history(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(
            self.bot,
            """SELECT id, species, regions, status, created_at, notified_at
               FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 20""",
            (str(ctx.author.id),), fetch=True,
        )
        if not rows:
            await ctx.respond(l10n.get("history_no_entries", lang), ephemeral=True)
            return

        buckets: dict[str, list[str]] = {
            "completed": [], "expired": [], "active": [], "other": []
        }
        for r in rows:
            status = r["status"]
            if r["notified_at"]:
                entry = l10n.get("history_entry_completed", lang,
                                  species=r["species"], regions=r["regions"],
                                  created=r["created_at"], notified=r["notified_at"], id=r["id"])
            else:
                entry = l10n.get("history_entry", lang,
                                  species=r["species"], regions=r["regions"],
                                  created=r["created_at"], id=r["id"])
            bucket = status if status in buckets else "other"
            buckets[bucket].append(entry)

        lines = [l10n.get("history_header", lang)]
        for bucket, key in [
            ("completed", "history_completed"), ("expired", "history_expired"),
            ("active", "history_active"), ("other", "history_other"),
        ]:
            if buckets[bucket]:
                lines.append(l10n.get(key, lang))
                lines.extend(f"  {e}" for e in buckets[bucket])
        await ctx.respond("\n".join(lines), ephemeral=True)

    @discord.slash_command(name="testnotification", description="Send a test DM to yourself")
    @allowed_channel()
    async def testnotification(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        try:
            await ctx.author.send(l10n.get("testnotification_dm", lang))
            await ctx.respond(l10n.get("testnotification_success", lang), ephemeral=True)
        except discord.Forbidden:
            await ctx.respond(l10n.get("testnotification_forbidden", lang), ephemeral=True)


async def setup(bot: discord.Bot):
    await bot.add_cog(NotificationsCog(bot))
