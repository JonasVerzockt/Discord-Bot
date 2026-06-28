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
cogs/discount_codes.py – Rabattcode-Tracker.

Liest in einem konfigurierten Kanal (DISCOUNT_CHANNEL_ID) alle Nachrichten,
extrahiert per Claude Haiku Rabattcodes und speichert sie in der DB. Jede
Nachricht wird – über ihre message_id – nur EINMAL an Haiku geschickt
(Tabelle discount_scanned). Kein Keyword-Vorfilter: Haiku entscheidet selbst.

Gültigkeit (Zustand eines Codes):
  status_override = 'valid'   → immer gültig (manuell)
  status_override = 'invalid' → immer ungültig (manuell)
  status_override = NULL      → automatisch:
      • permanent                                  → gültig
      • valid_until gesetzt und >= heute           → gültig, sonst abgelaufen
      • kein valid_until, aber Nachricht älter als
        _UNDATED_MAX_AGE_DAYS Tage                 → abgelaufen (Saison-Leiche)
      • kein valid_until, Nachricht jung           → gültig

  • on_ready  → einmaliger Komplett-Backfill (überspringt bereits Gescanntes)
  • on_message→ live
  • /codes [show_expired] → gültige (optional auch abgelaufene/deaktivierte)
  • /codes_set            → (Admin) Code gültig/ungültig/automatisch markieren
  • /codes_rescan [force] → (Admin) Kanal scannen (force = alles neu aufbauen)
"""
import re
import asyncio
import logging
from datetime import date, datetime, timedelta

import discord
from discord.ext import commands

from config import DISCOUNT_CHANNEL_ID
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.discount_parser import parse_codes
from cogs.server_settings import allowed_channel, admin_or_manage_messages

logger = logging.getLogger(__name__)

# Codes ohne Enddatum und ohne "permanent"-Flag gelten nach so vielen Tagen
# (gerechnet ab Nachrichtendatum) als wahrscheinlich abgelaufen.
_UNDATED_MAX_AGE_DAYS = 90


def _fmt_date(iso: str | None) -> str:
    """ISO YYYY-MM-DD → DD.MM.YYYY (sonst Originalwert)."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _domain(url: str | None) -> str:
    """Extrahiert die nackte Domain (ohne Schema/www/Pfad), lowercase."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].strip()


def _shop_key(shop: str | None, url: str | None) -> str:
    """Normalisierter Dedup-Schlüssel: Domain wenn vorhanden, sonst Shop-Name."""
    return _domain(url) or (shop or "").strip().lower()


def _shop_display(shop: str | None, url: str | None) -> str:
    """Anzeigename: echter Shop-Name, sonst Domain, sonst '?'."""
    s = (shop or "").strip()
    if s and s != "?":
        return s
    return _domain(url) or "?"


def _chunks(text: str, max_len: int = 1990) -> list[str]:
    """Teilt Text an Zeilenumbrüchen in Discord-taugliche Stücke."""
    if len(text) <= max_len:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_len:
            out.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip())
    return out


def _state(row, today: str, cutoff: str) -> str:
    """Gibt 'valid' | 'expired' | 'invalid' anhand Override + Datum + Alter."""
    ov = row["status_override"]
    if ov == "invalid":
        return "invalid"
    if ov == "valid":
        return "valid"
    if row["is_permanent"]:
        return "valid"
    vu = row["valid_until"]
    if vu:
        return "valid" if vu >= today else "expired"
    # Kein Enddatum: anhand Alter der Quellnachricht
    md = (row["message_date"] or "")[:10]
    if md and md < cutoff:
        return "expired"
    return "valid"


class DiscountCodesCog(commands.Cog, name="DiscountCodes"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._backfill_done = False
        self._lock = asyncio.Lock()   # serialisiert Backfill/Live-Verarbeitung

    # ── Verarbeitung ───────────────────────────────────────────────────────────
    async def _is_scanned(self, message_id: str) -> bool:
        rows = await execute_db(
            self.bot, "SELECT 1 FROM discount_scanned WHERE message_id=?",
            (message_id,), fetch=True,
        )
        return bool(rows)

    async def _process_message(self, msg: discord.Message) -> int:
        """
        Schickt eine Nachricht an Haiku und speichert gefundene Codes.
        Leere Nachrichten (nur Bild/Anhang) werden ohne API-Call als gescannt
        markiert. Gibt die Anzahl neuer Codes zurück. Caller stellt sicher, dass
        die Nachricht noch nicht gescannt wurde.
        """
        mid     = str(msg.id)
        content = (msg.content or "").strip()
        found   = 0

        if content:
            date_str = msg.created_at.strftime("%Y-%m-%d")
            try:
                codes = await asyncio.to_thread(parse_codes, content, date_str)
            except Exception as e:
                logger.error(f"❌ Haiku-Parse-Fehler (msg {mid}): {e}")
                codes = []

            for c in codes:
                await execute_db(
                    self.bot,
                    """INSERT OR IGNORE INTO discount_codes
                       (message_id, shop, shop_url, code, discount, valid_from,
                        valid_until, is_permanent, min_order, message_date, author)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mid, c["shop"], c["shop_url"], c["code"], c["discount"],
                        c["valid_from"], c["valid_until"], 1 if c["permanent"] else 0,
                        c["min_order"], msg.created_at.strftime("%Y-%m-%d %H:%M"),
                        getattr(msg.author, "name", ""),
                    ),
                    commit=True,
                )
                found += 1

        await execute_db(
            self.bot, "INSERT OR IGNORE INTO discount_scanned (message_id) VALUES (?)",
            (mid,), commit=True,
        )
        return found

    async def _backfill(self, channel: discord.TextChannel) -> tuple[int, int]:
        """Geht den ganzen Kanal durch, überspringt bereits gescannte Nachrichten."""
        scanned_rows = await execute_db(
            self.bot, "SELECT message_id FROM discount_scanned", fetch=True
        )
        scanned = {r["message_id"] for r in scanned_rows}

        checked = found = 0
        async with self._lock:
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author.bot or str(msg.id) in scanned:
                    continue
                has_text = bool((msg.content or "").strip())
                found  += await self._process_message(msg)
                checked += 1
                if has_text:
                    await asyncio.sleep(1.0)   # nur nach echten Haiku-Calls bremsen
        return checked, found

    # ── Events ─────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        if self._backfill_done:
            return
        channel = self.bot.get_channel(DISCOUNT_CHANNEL_ID)
        if not channel:
            if DISCOUNT_CHANNEL_ID:
                logger.warning("⚠️ Rabattcode-Kanal nicht gefunden (DISCOUNT_CHANNEL_ID)")
            return  # nicht konfiguriert → Feature inaktiv
        logger.info("🏷️ Rabattcode-Backfill startet…")
        checked, found = await self._backfill(channel)
        self._backfill_done = True
        logger.info(f"🏷️ Rabattcode-Backfill fertig – {checked} neue Nachrichten, {found} Codes")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not DISCOUNT_CHANNEL_ID or message.channel.id != DISCOUNT_CHANNEL_ID:
            return
        if await self._is_scanned(str(message.id)):
            return
        async with self._lock:
            found = await self._process_message(message)
        if found:
            try:
                await message.add_reaction("🏷️")
            except discord.HTTPException:
                pass

    # ── Slash Commands ─────────────────────────────────────────────────────────
    @discord.slash_command(
        name="codes",
        description="Show valid discount codes (optionally including expired ones)",
        description_localizations={"de": "Gültige Rabattcodes anzeigen (optional inkl. abgelaufener)"},
    )
    @allowed_channel()
    async def codes(
        self,
        ctx: discord.ApplicationContext,
        show_expired: discord.Option(
            bool, "Also show expired / manually disabled codes", default=False,
            name_localizations={"de": "abgelaufene_anzeigen"},
            description_localizations={"de": "Auch abgelaufene / deaktivierte Codes anzeigen"},
        ),
    ):
        await ctx.defer()
        lang   = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        today  = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=_UNDATED_MAX_AGE_DAYS)).isoformat()

        rows = await execute_db(
            self.bot,
            """SELECT shop, shop_url, code, discount, valid_until, is_permanent,
                      min_order, status_override, message_date
               FROM discount_codes
               ORDER BY shop COLLATE NOCASE, valid_until""",
            fetch=True,
        )

        # Zustand bestimmen, gültige zuerst → Dedup behält den gültigen Eintrag
        rank = {"valid": 0, "expired": 1, "invalid": 2}
        enriched = sorted(
            ((_state(r, today, cutoff), r) for r in rows),
            key=lambda t: (rank[t[0]], _shop_key(t[1]["shop"], t[1]["shop_url"])),
        )
        seen, items = set(), []
        for state, r in enriched:
            key = (_shop_key(r["shop"], r["shop_url"]), r["code"].lower())
            if key in seen:
                continue
            seen.add(key)
            items.append((state, r))

        if not show_expired:
            items = [(s, r) for s, r in items if s == "valid"]

        if not items:
            await ctx.followup.send(l10n.get("discount_list_empty", lang))
            return

        lines = [l10n.get("discount_list_header", lang, count=len(items))]
        for state, r in items:
            if state == "invalid":
                marker, validity = "🚫 ", l10n.get("discount_invalid", lang)
            elif state == "expired":
                marker = "⌛ "
                validity = (
                    l10n.get("discount_expired", lang, date=_fmt_date(r["valid_until"]))
                    if r["valid_until"] else l10n.get("discount_expired_old", lang)
                )
            else:  # valid
                marker = ""
                if r["status_override"] == "valid":
                    validity = l10n.get("discount_forced_valid", lang)
                elif r["is_permanent"]:
                    validity = l10n.get("discount_perm", lang)
                elif r["valid_until"]:
                    validity = l10n.get("discount_until", lang, date=_fmt_date(r["valid_until"]))
                else:
                    validity = l10n.get("discount_open", lang)

            min_part = (
                l10n.get("discount_min_order", lang, min_order=r["min_order"])
                if r["min_order"] else ""
            )
            entry = l10n.get(
                "discount_entry", lang,
                marker=marker, shop=_shop_display(r["shop"], r["shop_url"]),
                code=r["code"], discount=r["discount"] or "?",
                validity=validity, min_part=min_part,
            )
            if r["shop_url"]:
                entry += f"\n<{r['shop_url']}>"
            lines.append(entry)

        chunks = _chunks("\n".join(lines))
        await ctx.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await ctx.followup.send(chunk)

    @discord.slash_command(
        name="codes_set",
        description="(Admin) Mark a code as valid / invalid / automatic",
        description_localizations={"de": "(Admin) Code als gültig / ungültig / automatisch markieren"},
    )
    @admin_or_manage_messages()
    async def codes_set(
        self,
        ctx: discord.ApplicationContext,
        code: discord.Option(str, "The discount code", required=True),
        status: discord.Option(
            str, "valid = always valid, invalid = always invalid, auto = by date",
            choices=["valid", "invalid", "auto"], required=True,
            description_localizations={"de": "valid = immer gültig, invalid = immer ungültig, auto = nach Datum"},
        ),
        shop: discord.Option(str, "Limit to this shop (optional)", required=False, default=None),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)

        override = None if status == "auto" else status
        params: list = [override, code.strip()]
        query = "UPDATE discount_codes SET status_override=? WHERE lower(code)=lower(?)"
        if shop:
            query += " AND lower(shop)=lower(?)"
            params.append(shop.strip())

        rc = await execute_db(self.bot, query, tuple(params), commit=True)
        if not rc:
            await ctx.followup.send(l10n.get("discount_set_none", lang, code=code), ephemeral=True)
            return

        status_word = l10n.get(f"discount_set_state_{status}", lang)
        await ctx.followup.send(
            l10n.get("discount_set_done", lang, count=rc, code=code, status=status_word),
            ephemeral=True,
        )
        logger.info(f"🏷️ codes_set: '{code}' (shop={shop or '*'}) → {override} ({rc} Zeilen) von {ctx.author.id}")

    @discord.slash_command(
        name="codes_rescan",
        description="(Admin) Re-scan the discount channel",
        description_localizations={"de": "(Admin) Rabattcode-Kanal erneut scannen"},
    )
    @admin_or_manage_messages()
    async def codes_rescan(
        self,
        ctx: discord.ApplicationContext,
        force: discord.Option(
            bool, "Full rebuild: delete all stored codes + scan history, re-parse everything",
            default=False,
            description_localizations={"de": "Komplett neu: alle Codes + Scan-Historie löschen und alles neu parsen"},
        ),
    ):
        await ctx.defer(ephemeral=True)
        lang    = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        channel = self.bot.get_channel(DISCOUNT_CHANNEL_ID)
        if not channel:
            await ctx.followup.send(l10n.get("discount_channel_missing", lang), ephemeral=True)
            return

        if force:
            await execute_db(self.bot, "DELETE FROM discount_codes", commit=True)
            await execute_db(self.bot, "DELETE FROM discount_scanned", commit=True)

        await ctx.followup.send(l10n.get("discount_rescan_start", lang), ephemeral=True)
        checked, found = await self._backfill(channel)
        await ctx.followup.send(
            l10n.get("discount_rescan_done", lang, scanned=checked, codes=found),
            ephemeral=True,
        )


def setup(bot: discord.Bot):
    bot.add_cog(DiscountCodesCog(bot))
