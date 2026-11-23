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
import time
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
        await ctx.defer(ephemeral=True)
        try:
            rows  = sheet.row_count - 1
            pend  = await pending_count(self.bot)
            track = await tracking_count(self.bot)
            text = l10n.get("review_status", lang, rows=rows, tracked=track, pending=pend)
            await ctx.followup.send(text, embed=await self._health_embed(lang), ephemeral=True)
        except Exception as e:
            logger.error(f"❌ status error: {e}")
            await ctx.followup.send(l10n.get("admin_error", lang, error=e), ephemeral=True)

    async def _health_embed(self, lang: str) -> discord.Embed:
        """Gesundheits-Übersicht (dieselben Checks wie die Status-Seite des Boards)
        plus Größe der Datenbanken."""
        import os
        from config import DB_FILE, BOARD_DB_FILE, DATA_DIRECTORY, BOARD_PUBLIC_URL
        icon = {"ok": "🟢", "warn": "🟡", "down": "🔴", "off": "⚪"}
        e = discord.Embed(title=l10n.get("health_title", lang), color=ADMIN_COLOR)
        try:
            from cogs.board import _collect_health
            overall, sections = await _collect_health({"bot": self.bot}, lang)
            e.description = f"{icon.get(overall[0], '❔')} **{overall[1]}**"
            for sec in sections:
                checks = sec["checks"]
                bad = [c for c in checks if c["state"] in ("warn", "down")]
                n_ok = sum(1 for c in checks if c["state"] == "ok")
                lines = [f"{icon[c['state']]} **{c['name']}**: {str(c.get('detail', ''))[:90]}" for c in bad]
                lines.append(l10n.get("health_ok_count", lang, ok=n_ok, total=len(checks)))
                e.add_field(name=sec["title"], value="\n".join(lines)[:1020], inline=False)
        except Exception as ex:
            e.description = l10n.get("health_unavailable", lang, error=str(ex)[:150])
        sizes = []
        for label, path in (("Bot", DB_FILE), ("Board", BOARD_DB_FILE),
                            ("Preise", os.path.join(DATA_DIRECTORY, "price_history.db"))):
            try:
                sizes.append(f"{label}: {os.path.getsize(path) / 1_048_576:.1f} MB")
            except OSError:
                pass
        if sizes:
            e.add_field(name=l10n.get("health_db_h", lang), value=" · ".join(sizes), inline=False)
        if BOARD_PUBLIC_URL:
            e.set_footer(text=l10n.get("health_footer", lang, url=BOARD_PUBLIC_URL))
        return e

    # Reihenfolge = sinnvolle Ausführungsreihenfolge. (label, Cog-Name, Loop-Attribut)
    # Bewusst NICHT dabei: weekly_digest (würde ggf. den Wochen-Digest an alle Abonnenten
    # verschicken) – der bleibt seinem festen Montags-Zeitplan überlassen.
    _RUNNABLE_JOBS = [
        ("Daten-Pipeline (Reload → Preis → Arten)", "Tasks",         "reload_shops_task"),
        ("Verfügbarkeits-Check",                    "Tasks",         "check_availability"),
        ("Shop-Bewertungen synchronisieren",        "Tasks",         "sync_shop_ratings"),
        ("Alte Benachrichtigungen entfernen",       "Tasks",         "expire_old_notifications"),
        ("DB-Optimierung (VACUUM)",                 "Tasks",         "optimize_db"),
        ("Bot-Statusanzeige aktualisieren",         "Tasks",         "update_bot_status"),
        ("Entfallene Varianten (Sammel-DM)",        "PriceTracking", "flush_removed_variants"),
        ("Command-Log schreiben",                   "CommandLog",    "flush_log"),
        ("Command-Log aufräumen (Retention)",       "CommandLog",    "cleanup_log"),
        ("KI-Chat · Verläufe aufräumen",            "AiChatCog",     "cleanup_loop"),
        ("KI-Chat · Shop-Daten-Refresh",            "AiChatCog",     "shop_data_loop"),
        ("iNaturalist · Abgleich (letzte 6 Monate)", "InatTracker",   "daily"),
        ("Schwarmflug · Wetter nachtragen",         "Swarm",         "weather_loop"),
    ]

    @discord.slash_command(name="run_jobs", description="🔒 [Admin] Run all background jobs once now", description_localizations={"de": "🔒 [Admin] Alle Hintergrund-Jobs einmalig sofort ausführen"})
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_run_jobs(self, ctx: discord.ApplicationContext):
        """Stößt alle Hintergrund-Jobs einmalig sofort an (unabhängig vom Timer) und
        meldet je Job Erfolg/Fehler + Dauer. Nützlich nach Deploy/Neustart, um nicht
        auf den nächsten uhr-ausgerichteten Lauf warten zu müssen."""
        await ctx.defer(ephemeral=True)
        lines = []
        for label, cog_name, attr in self._RUNNABLE_JOBS:
            cog  = self.bot.get_cog(cog_name)
            loop = getattr(cog, attr, None) if cog else None
            if loop is None:
                lines.append(f"⏭️ {label} – übersprungen (nicht geladen)")
                continue
            t0 = time.monotonic()
            try:
                await loop()   # tasks.Loop einmalig ausführen (unabhängig vom Zeitplan)
                lines.append(f"✅ {label} · {time.monotonic() - t0:.1f}s")
            except Exception as e:
                logger.error("run_jobs: %s (%s) fehlgeschlagen: %s", attr, cog_name, e, exc_info=True)
                lines.append(f"❌ {label} – {str(e)[:80]}")
        embed = discord.Embed(
            title="🔄 Hintergrund-Jobs einmalig ausgeführt",
            description="\n".join(lines) + "\n\n*Wochen-Digest bewusst ausgenommen (fester Montags-Versand).*",
            color=ADMIN_COLOR,
        )
        await ctx.followup.send(embed=embed, ephemeral=True)

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
                from utils.account import export_user
                # Gleicher Umfang wie der Selbst-Download in „Mein Konto“ (Board /konto).
                data = await export_user(self.bot, uid)

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
                "user_species_watch_variant_seen",
                "pending_variant_removed",
                "server_user_mappings",
                "ch_delivery_shops",
                "ai_chat_budget",
                "ai_chat_history",
                "ai_chat_user_model",
                "ai_chat_user_spend",
                "digest_subscribers",
                "achievements",
                "user_events",
                "command_log",
                "offer_keywords",
                "offer_alert_seen",
                "offer_alert_msgs",
                "discount_codes",
                "review_tracking",
                "review_pending",
                "server_settings",
                "map_entries",
                "map_entry_tags",
                "map_entry_regions",
                "map_events",
                "map_event_rsvp",
                "map_rsvp_reminded",
                "review_authors",
                "inat_accounts",
                "inat_obs",
                "user_prefs",
                "dm_log",
                "quiet_offer_queue",
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
                force=True,   # /reprocess = bewusste manuelle Korrektur
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


    # ── /known_users ─────────────────────────────────────────────────────────
    @discord.slash_command(
        name="known_users",
        description="🔒 [Admin] List all users who ever used the bot (ID → name)",
        description_localizations={"de": "🔒 [Admin] Alle je aktiven Nutzer auflisten (ID → Name)"},
    )
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    @allowed_channel()
    async def cmd_known_users(self, ctx: discord.ApplicationContext):
        """Sammelt distinct User-IDs aus ALLEN User-Tabellen und loest sie zu Namen auf.

        Vollstaendigste Quelle = Union aller Tabellen mit User-IDs (nicht nur das
        Command-Log, das eine Retention hat und nur Slash-Befehle erfasst).
        discount_codes speichert nur Usernamen (keine ID) und das Feedback-Board
        nur HMAC-Hashes (nicht rueckrechenbar) -> beide bleiben aussen vor.
        """
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        from utils.db import execute_db

        sources = [
            ("user_settings", "user_id"), ("notifications", "user_id"),
            ("user_shop_blacklist", "user_id"), ("server_user_mappings", "user_id"),
            ("user_seen_products", "user_id"), ("ch_delivery_shops", "added_by"),
            ("user_price_tracking", "user_id"), ("user_species_watch", "user_id"),
            ("user_species_watch_seen", "user_id"),
            ("user_species_watch_variant_seen", "user_id"),
            ("pending_variant_removed", "user_id"), ("ai_chat_budget", "user_id"),
            ("ai_chat_history", "user_id"), ("ai_chat_user_model", "user_id"),
            ("ai_chat_user_spend", "user_id"), ("digest_subscribers", "user_id"),
            ("achievements", "user_id"), ("user_events", "user_id"),
            ("command_log", "user_id"),
            ("offer_keywords", "user_id"), ("offer_alert_seen", "user_id"),
            ("map_entries", "user_id"), ("map_event_rsvp", "user_id"),
            ("inat_accounts", "discord_id"), ("inat_obs", "discord_id"),
        ]

        ids: set[int] = set()
        for tbl, col in sources:
            try:
                rows = await execute_db(
                    self.bot, f"SELECT DISTINCT {col} AS uid FROM {tbl}", fetch=True
                )
            except Exception:
                continue  # Tabelle (noch) nicht vorhanden
            for r in rows or []:
                v = r["uid"]
                if v is None:
                    continue
                s = str(v).strip()
                if s.isdigit() and s != "0":     # 0 = globaler AI-Budget-Sammeleintrag
                    ids.add(int(s))

        if not ids:
            await ctx.respond(l10n.get("known_users_none", lang), ephemeral=True)
            return

        guild      = ctx.guild
        left       = l10n.get("known_users_left", lang)
        unresolved = l10n.get("known_users_unresolved", lang)

        lines: list[str] = []
        for uid in sorted(ids):
            member = guild.get_member(uid) if guild else None
            if member:                                   # noch auf dem Server (Cache)
                lines.append(f"`{uid}` – {member.display_name} (@{member.name})")
                continue
            user = self.bot.get_user(uid)                # globaler User-Cache
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)   # API-Fallback
                except Exception:
                    user = None
            if user:
                lines.append(f"`{uid}` – @{user.name} {left}")
            else:
                lines.append(f"`{uid}` – {unresolved}")

        header = l10n.get("known_users_header", lang, count=len(ids))
        await send_embeds(ctx, header + "\n" + "\n".join(lines),
                          ephemeral=True, color=ADMIN_COLOR)


def setup(bot: discord.Bot):
    bot.add_cog(AdminCog(bot))
