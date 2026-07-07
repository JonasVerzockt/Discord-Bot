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
cogs/digest.py – Wöchentlicher Digest, nur per DM an Opt-in-Abonnenten.

Inhalt:
  • Größte Preisstürze der letzten 7 Tage (aus price_history.db)
  • Neue Arten im Angebot (Diff gegen known_species)
  • Neue Shops (Diff gegen known_shops)

An-/Abmelden per /digest. Versand Montags 09:00 (Berliner Zeit).
Die Baseline-Tabellen werden beim ersten Lauf befüllt (dann keine "neu"-Meldung).
"""
import asyncio
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from config import DATA_DIRECTORY, DB_FILE
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data

logger = logging.getLogger(__name__)

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"
BERLIN = ZoneInfo("Europe/Berlin")

_MODE = {"aktivieren": "on", "deaktivieren": "off", "status": "status"}


# ── Sync-Datenzugriff ────────────────────────────────────────────────────────────

def _price_drops_sync(pid_info: dict, limit: int = 10) -> list:
    """
    Ermittelt die größten Preisstürze der letzten 7 Tage.
    pid_info: {product_id: {"species","shop","url","currency"}}
    """
    if not PRICE_HISTORY_DB.exists():
        return []
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT product_id FROM product_price_history "
            "WHERE recorded_at >= datetime('now', '-7 days')"
        )
        changed = [r[0] for r in cur.fetchall()]
        results = []
        for pid in changed:
            info = pid_info.get(int(pid))
            if not info:
                continue
            cur.execute(
                "SELECT min_price FROM product_price_history "
                "WHERE product_id=? AND recorded_at < datetime('now', '-7 days') "
                "ORDER BY recorded_at DESC LIMIT 1",
                (pid,),
            )
            b = cur.fetchone()
            if b:
                base = b[0]
            else:
                cur.execute(
                    "SELECT min_price FROM product_price_history "
                    "WHERE product_id=? ORDER BY recorded_at ASC LIMIT 1",
                    (pid,),
                )
                e = cur.fetchone()
                base = e[0] if e else None
            cur.execute(
                "SELECT min_price, currency_iso FROM product_price_history "
                "WHERE product_id=? ORDER BY recorded_at DESC LIMIT 1",
                (pid,),
            )
            c = cur.fetchone()
            if base is None or c is None:
                continue
            curr = c[0]
            currency = c[1] or info.get("currency") or "EUR"
            if base > 0 and curr < base:
                results.append({
                    "species":  info["species"],
                    "shop":     info["shop"],
                    "url":      info["url"],
                    "old":      base,
                    "new":      curr,
                    "pct":      (base - curr) / base * 100.0,
                    "currency": currency,
                })
        results.sort(key=lambda d: d["pct"], reverse=True)
        return results[:limit]
    finally:
        conn.close()


def _species_diff_sync(current: set) -> list:
    """Neue Arten gegenüber known_species. Beim ersten Lauf: nur befüllen (kein Report)."""
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute("SELECT species FROM known_species")
        known = {r[0] for r in cur.fetchall()}
        if not known:
            cur.executemany(
                "INSERT OR IGNORE INTO known_species (species) VALUES (?)",
                [(s,) for s in current],
            )
            conn.commit()
            return []
        new = sorted(current - known)
        if new:
            cur.executemany(
                "INSERT OR IGNORE INTO known_species (species) VALUES (?)",
                [(s,) for s in new],
            )
            conn.commit()
        return new
    finally:
        conn.close()


def _shops_diff_sync(current: dict) -> list:
    """Neue Shops (nach shop_id) gegenüber known_shops. Erster Lauf: nur befüllen."""
    conn = sqlite3.connect(DB_FILE)
    try:
        cur = conn.cursor()
        cur.execute("SELECT shop_id FROM known_shops")
        known = {r[0] for r in cur.fetchall()}
        if not known:
            cur.executemany(
                "INSERT OR IGNORE INTO known_shops (shop_id, name) VALUES (?, ?)",
                [(sid, nm) for sid, nm in current.items()],
            )
            conn.commit()
            return []
        new_ids = [sid for sid in current if sid not in known]
        if new_ids:
            cur.executemany(
                "INSERT OR IGNORE INTO known_shops (shop_id, name) VALUES (?, ?)",
                [(sid, current[sid]) for sid in new_ids],
            )
            conn.commit()
        return [current[sid] for sid in new_ids]
    finally:
        conn.close()


def _chunk_lines(lines: list, limit: int = 1990) -> list:
    """Fügt Zeilen zu Nachrichten <= limit Zeichen zusammen."""
    chunks, cur, cur_len = [], [], 0
    for ln in lines:
        add = len(ln) + 1
        if cur_len + add > limit and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ── Cog ────────────────────────────────────────────────────────────────────────

class DigestCog(commands.Cog, name="Digest"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.weekly_digest.start()

    def cog_unload(self):
        self.weekly_digest.cancel()

    # ── /digest ────────────────────────────────────────────────────────────────

    @discord.slash_command(
        name="digest",
        description="Weekly digest via DM: price drops, new species & shops.",
        description_localizations={"de": "Wöchentlicher Digest per DM: Preisstürze, neue Arten & Shops."},
    )
    @commands.guild_only()
    async def digest(
        self,
        ctx: discord.ApplicationContext,
        action: discord.Option(  # type: ignore[valid-type]
            str,
            "aktivieren = anmelden, deaktivieren = abmelden, status = Status prüfen",
            choices=["aktivieren", "deaktivieren", "status"],
        ),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        uid  = str(ctx.author.id)
        mode = _MODE[action]

        if mode == "on":
            await execute_db(
                self.bot,
                "INSERT OR IGNORE INTO digest_subscribers (user_id) VALUES (?)",
                (uid,), commit=True,
            )
            await ctx.respond(l10n.get("digest_subscribed", lang), ephemeral=True)
        elif mode == "off":
            await execute_db(
                self.bot,
                "DELETE FROM digest_subscribers WHERE user_id=?",
                (uid,), commit=True,
            )
            await ctx.respond(l10n.get("digest_unsubscribed", lang), ephemeral=True)
        else:
            rows = await execute_db(
                self.bot,
                "SELECT 1 FROM digest_subscribers WHERE user_id=?",
                (uid,), fetch=True,
            )
            key = "digest_status_on" if rows else "digest_status_off"
            await ctx.respond(l10n.get(key, lang), ephemeral=True)

    # ── Wochen-Task ─────────────────────────────────────────────────────────────

    @tasks.loop(time=dtime(hour=9, minute=0, tzinfo=BERLIN))
    async def weekly_digest(self):
        # tasks.loop(time=…) feuert täglich – wir handeln nur montags.
        if datetime.now(BERLIN).weekday() != 0:
            return
        try:
            await self._run_digest()
        except Exception as e:
            logger.error("❌ weekly_digest error: %s", e, exc_info=True)

    @weekly_digest.before_loop
    async def before_weekly_digest(self):
        await self.bot.wait_until_ready()

    async def _run_digest(self):
        subs = await execute_db(
            self.bot, "SELECT user_id FROM digest_subscribers", fetch=True
        )
        if not subs:
            return

        shop_data = await load_shop_data(self.bot)
        pid_info, cur_species, cur_shops = {}, set(), {}
        for shop_id, shop in shop_data.items():
            name = shop.get("name") or str(shop_id)
            cur_shops[str(shop_id)] = name
            for prod in shop.get("products", []):
                sp  = (prod.get("species") or "").strip()
                pid = prod.get("id")
                if sp:
                    cur_species.add(sp)
                if pid is not None:
                    pid_info[int(pid)] = {
                        "species":  sp or f"#{pid}",
                        "shop":     name,
                        "url":      prod.get("antcheck_url") or "",
                        "currency": prod.get("currency_iso") or "EUR",
                    }

        drops       = await asyncio.to_thread(_price_drops_sync, pid_info, 10)
        new_species = await asyncio.to_thread(_species_diff_sync, cur_species)
        new_shops   = await asyncio.to_thread(_shops_diff_sync, cur_shops)

        logger.info(
            "📰 Digest: %d Abonnenten, %d Preisstürze, %d neue Arten, %d neue Shops",
            len(subs), len(drops), len(new_species), len(new_shops),
        )

        for row in subs:
            uid = row["user_id"]
            try:
                lang = await get_user_lang(self.bot, uid, None)
                chunks = self._build_chunks(lang, drops, new_species, new_shops)
                user = await self.bot.fetch_user(int(uid))
                for chunk in chunks:
                    await user.send(chunk)
            except discord.Forbidden:
                logger.info("📪 Digest: DMs für User %s gesperrt – übersprungen", uid)
            except Exception as e:
                logger.warning("⚠️ Digest an %s fehlgeschlagen: %s", uid, e)

    def _build_chunks(self, lang: str, drops: list, new_species: list, new_shops: list) -> list:
        lines = [l10n.get("digest_title", lang)]
        has_content = False

        if drops:
            lines.append("")
            lines.append(l10n.get("digest_drops_header", lang))
            for d in drops:
                lines.append(l10n.get(
                    "digest_drops_line", lang,
                    species=d["species"], shop=d["shop"],
                    old=f"{d['old']:.2f} {d['currency']}",
                    new=f"{d['new']:.2f} {d['currency']}",
                    pct=f"{d['pct']:.0f}", url=d["url"] or "",
                ))
            has_content = True

        if new_species:
            lines.append("")
            lines.append(l10n.get("digest_new_species_header", lang))
            for sp in new_species[:15]:
                lines.append(l10n.get("digest_item", lang, name=sp))
            if len(new_species) > 15:
                lines.append(l10n.get("digest_more", lang, count=len(new_species) - 15))
            has_content = True

        if new_shops:
            lines.append("")
            lines.append(l10n.get("digest_new_shops_header", lang))
            for nm in new_shops[:15]:
                lines.append(l10n.get("digest_item", lang, name=nm))
            if len(new_shops) > 15:
                lines.append(l10n.get("digest_more", lang, count=len(new_shops) - 15))
            has_content = True

        if not has_content:
            lines.append("")
            lines.append(l10n.get("digest_nothing", lang))

        lines.append("")
        lines.append("-# " + l10n.get("digest_footer", lang))
        return _chunk_lines(lines)


def setup(bot: discord.Bot):
    bot.add_cog(DigestCog(bot))
