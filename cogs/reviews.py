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
cogs/reviews.py – Review-Verarbeitung als discord.py Cog.

Enthält:
  • on_ready   → Sheet laden + Reconcile-Scan
  • on_message → neue Bewertungen verarbeiten
  • on_message_edit → bestehende Sheet-Zeile aktualisieren
  • on_raw_reaction_add → Retry bei 🟡

Interne Helfer:
  _process()         – parst und schreibt ins Sheet
  _reconcile_scan()  – gleicht Discord-History mit Sheet-Zeilen ab
"""
import asyncio
import logging
import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta

from config import REVIEW_CHANNEL_ID, SCAN_DAYS
from utils.sheet import sheet
from utils.tracking import (
    get_tracking, set_tracking, get_all_tracking,
    get_pending, set_pending, remove_pending, get_all_pending,
)
from utils.shop import (
    resolve_shop, extract_identifier, learn_shop,
    add_to_csv, reload_mapping, UnresolvableShop,
)
from utils.ai_parser import looks_like_review, parse_with_ai, build_row

logger = logging.getLogger(__name__)


class ReviewsCog(commands.Cog, name="Reviews"):
    """Kern-Logik: Bewertungen lesen, parsen und ins Sheet schreiben."""

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    # ── Hilfsmethoden ──────────────────────────────────────────────────────────
    async def _clean_react(self, msg: discord.Message, *remove: str, add: str) -> None:
        for e in remove:
            try:
                await msg.remove_reaction(e, self.bot.user)
            except Exception:
                pass
        await msg.add_reaction(add)

    async def _process(
        self,
        message: discord.Message,
        is_edit: bool = False,
        shop_override: str | None = None,
    ) -> None:
        """
        Parst die Nachricht mit KI und schreibt sie ins Sheet.
        Bei Erfolg wird das Tracking in der DB aktualisiert.
        """
        mid      = str(message.id)
        date_str = message.created_at.strftime("%d.%m.%Y")

        shop   = shop_override or resolve_shop(message.content, message.guild)
        parsed = parse_with_ai(message.content, shop, date_str)
        row    = build_row(parsed)

        existing_row = await get_tracking(self.bot, mid)
        if is_edit and existing_row is not None:
            if existing_row > sheet.row_count:
                logger.warning(
                    f"Tracking-Fehler: Zeile {existing_row} außerhalb des Sheets "
                    f"({sheet.row_count - 1} Einträge) – übersprungen"
                )
                return
            sheet.update(existing_row, row)
            logger.info(f"✏️  [{date_str}] {parsed['shop_name']} Zeile {existing_row}")
        else:
            row_num = sheet.append(row)
            await set_tracking(self.bot, mid, row_num)
            logger.info(f"➕ [{date_str}] {parsed['shop_name']} {parsed.get('bewertung')}/10")

    async def _mark_unresolvable(self, msg: discord.Message, e: UnresolvableShop) -> None:
        add_to_csv(e.identifier, str(msg.id), msg.created_at.strftime("%d.%m.%Y"))
        await set_pending(self.bot, str(msg.id), "unresolved_shop", e.identifier)
        await msg.add_reaction("🟡")
        logger.warning(f"🟡 Unaufgelöst: '{e.identifier}'")

    async def _mark_error(self, msg: discord.Message, e: Exception) -> None:
        await set_pending(self.bot, str(msg.id), "parse_error")
        await msg.add_reaction("🟡")
        logger.error(f"🟡 Fehler {msg.id}: {e}")

    async def _reconcile_scan(self, channel: discord.TextChannel) -> tuple[int, int]:
        """
        Gleicht Discord-History mit bestehenden Sheet-Zeilen ab.
        Gibt (gemappt, neu_geschrieben) zurück.
        """
        cutoff       = datetime.now(timezone.utc) - timedelta(days=SCAN_DAYS)
        all_tracking = await get_all_tracking(self.bot)
        all_pending  = await get_all_pending(self.bot)
        already_mapped = set(all_tracking.values())

        # 1. Nicht gemappte Review-Nachrichten sammeln
        msgs_by_date: dict[str, list[discord.Message]] = {}
        async for msg in channel.history(limit=None, after=cutoff, oldest_first=True):
            if msg.author.bot or not looks_like_review(msg.content):
                continue
            mid = str(msg.id)
            if mid in all_tracking or mid in all_pending:
                continue
            date_str = msg.created_at.strftime("%d.%m.%Y")
            msgs_by_date.setdefault(date_str, []).append(msg)

        if not msgs_by_date:
            return 0, 0

        # 2. Nicht gemappte Sheet-Zeilen nach Datum gruppieren
        rows_by_date: dict[str, list[int]] = {}
        for i, row in enumerate(sheet.rows[1:], start=2):
            if not row or not row[0]:
                break
            if i in already_mapped:
                continue
            try:
                dt = datetime.strptime(row[0].strip(), "%d.%m.%Y")
            except ValueError:
                continue
            rows_by_date.setdefault(dt.strftime("%d.%m.%Y"), []).append(i)

        # 3. Abgleich: Datum + Reihenfolge
        mapped_count = 0
        new_count    = 0

        for date_str, messages in msgs_by_date.items():
            existing = rows_by_date.get(date_str, [])
            for idx, msg in enumerate(messages):
                mid = str(msg.id)
                if idx < len(existing):
                    row_num = existing[idx]
                    await set_tracking(self.bot, mid, row_num)
                    mapped_count += 1
                    logger.info(f"🔗 [{date_str}] #{idx + 1} → Zeile {row_num}")
                    # Aus Ground Truth lernen
                    sheet_row  = sheet.rows[row_num - 1]
                    sheet_shop = sheet_row[2].strip() if len(sheet_row) > 2 else ""
                    if sheet_shop:
                        identifier = extract_identifier(msg.content)
                        if identifier:
                            learn_shop(identifier, sheet_shop)
                else:
                    try:
                        await self._process(msg)
                        new_count += 1
                        await asyncio.sleep(1.2)
                    except UnresolvableShop as e:
                        await self._mark_unresolvable(msg, e)
                    except Exception as e:
                        await self._mark_error(msg, e)

        return mapped_count, new_count

    # ── Bot-Events ─────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        channel = self.bot.get_channel(REVIEW_CHANNEL_ID)
        if not channel:
            logger.error("Review-Kanal nicht gefunden!")
            return

        sheet.load()
        logger.info(f"🔍 Gleiche letzte {SCAN_DAYS} Tage mit bestehenden Zeilen ab…")
        mapped, written = await self._reconcile_scan(channel)
        all_pending = await get_all_pending(self.bot)
        logger.info(
            f"✅ Fertig – {mapped} gemappt, {written} neu geschrieben, "
            f"{len(all_pending)} ausstehend (🟡)"
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.channel.id != REVIEW_CHANNEL_ID:
            return
        if not looks_like_review(message.content):
            return

        try:
            await self._process(message)
            await message.add_reaction("🟢")
        except UnresolvableShop as e:
            await self._mark_unresolvable(message, e)
        except Exception as e:
            await self._mark_error(message, e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.author.bot or after.channel.id != REVIEW_CHANNEL_ID:
            return
        if not looks_like_review(after.content) or before.content == after.content:
            return

        mid = str(after.id)
        try:
            await self._process(after, is_edit=True)
            await remove_pending(self.bot, mid)
            await self._clean_react(after, "🟡", "🔴", add="🟢")
        except UnresolvableShop as e:
            await self._mark_unresolvable(after, e)
        except Exception as e:
            await self._mark_error(after, e)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """🟡 von echtem User → Retry wenn CSV ausgefüllt."""
        if str(payload.emoji) != "🟡":
            return
        if payload.user_id == self.bot.user.id or payload.channel_id != REVIEW_CHANNEL_ID:
            return

        mid   = str(payload.message_id)
        entry = await get_pending(self.bot, mid)
        if not entry:
            return

        channel = self.bot.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        identifier    = entry.get("identifier", "")
        shop_override = reload_mapping().get(identifier) if identifier else None

        if entry["reason"] == "unresolved_shop" and not shop_override:
            logger.info(f"⏳ CSV für '{identifier}' noch nicht ausgefüllt")
            return

        existing_row = await get_tracking(self.bot, mid)
        try:
            await self._process(
                message,
                is_edit=(existing_row is not None),
                shop_override=shop_override,
            )
            await remove_pending(self.bot, mid)
            await self._clean_react(message, "🟡", "🔴", add="🟢")
            logger.info(f"✅ Retry OK: {mid}")
        except Exception as e:
            logger.error(f"❌ Retry fehlgeschlagen {mid}: {e}")
            await self._clean_react(message, "🟡", add="🔴")


def setup(bot: discord.Bot):
    bot.add_cog(ReviewsCog(bot))
