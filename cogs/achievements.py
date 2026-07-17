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
cogs/achievements.py – /achievements: eigene Erfolge abfragen (Embed).

Zeigt freigeschaltete, in Arbeit befindliche und versteckte Erfolge. Ein globaler
Listener protokolliert Befehlsnutzung (für Aktions-/Versteckt-Erfolge) und schaltet
neue Erfolge frei – mit dezenter DM.
"""
import asyncio
import logging

import discord
from discord.ext import commands

from utils.localization import l10n, get_user_lang
from utils.embeds import EMBED_COLOR
from utils.achievements import (
    ACHIEVEMENTS, evaluate, gather_stats, check_and_grant, log_event,
    rank_for, next_rank_threshold,
)

logger = logging.getLogger(__name__)

_CATS = ["avail", "price", "community", "usage", "hidden"]

# Achievement-Check pro User hoechstens 1x pro Cooldown (Sekunden) – entlastet
# gather_stats bei schnellen Command-Bursts; Trailing-Check garantiert Vollstaendigkeit.
_ACH_COOLDOWN = 15.0


def _bar(cur: int, tgt: int, width: int = 5) -> str:
    if tgt <= 0:
        return "▰" * width
    filled = max(0, min(width, round(cur / tgt * width)))
    return "▰" * filled + "▱" * (width - filled)


class AchievementsCog(commands.Cog, name="Achievements"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._ach_last: dict[int, float] = {}     # user_id -> letzte Check-Zeit (loop.time)
        self._ach_pending: set[int] = set()       # user_id mit eingeplantem Trailing-Check

    # ── globaler Listener: Befehlsnutzung protokollieren + freischalten ──────────
    @commands.Cog.listener()
    async def on_application_command_completion(self, ctx: discord.ApplicationContext):
        try:
            if ctx.author is None or ctx.author.bot:
                return
            name = getattr(ctx.command, "qualified_name", None) or getattr(ctx.command, "name", "?")
            # log_event ist billig (1 Insert) und laeuft IMMER – die Event-Historie
            # muss vollstaendig sein. Der teure gather_stats-Check wird gedrosselt.
            await log_event(self.bot, ctx.author.id, f"cmd:{name}")

            uid = ctx.author.id
            now = self.bot.loop.time()
            if now - self._ach_last.get(uid, 0.0) >= _ACH_COOLDOWN:
                self._ach_last[uid] = now
                lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
                await check_and_grant(self.bot, ctx.author, lang)
            elif uid not in self._ach_pending:
                # innerhalb des Cooldowns -> genau einen nachlaufenden Check einplanen,
                # damit die juengste Aktion garantiert geprueft wird (kein verpasster Erfolg)
                self._ach_pending.add(uid)
                asyncio.create_task(self._deferred_grant(ctx.author, ctx.guild_id))
        except Exception as e:
            logger.debug("achievement completion hook failed: %s", e)

    async def _deferred_grant(self, author, guild_id):
        """Nachlaufender Achievement-Check nach Ablauf des Cooldowns (Trailing-Debounce)."""
        try:
            await asyncio.sleep(_ACH_COOLDOWN)
            self._ach_last[author.id] = self.bot.loop.time()
            lang = await get_user_lang(self.bot, author.id, guild_id)
            await check_and_grant(self.bot, author, lang)
        except Exception as e:
            logger.debug("deferred achievement grant failed: %s", e)
        finally:
            self._ach_pending.discard(author.id)

    # ── /achievements ────────────────────────────────────────────────────────────
    @discord.slash_command(
        name="achievements",
        description="Show your achievements: unlocked, in progress and hidden ones.",
        description_localizations={"de": "Deine Erfolge anzeigen: freigeschaltet, in Arbeit und versteckt."},
    )
    @commands.guild_only()
    async def achievements(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        results = await check_and_grant(self.bot, ctx.author, lang)
        by_id = {a.id: (a, cur, tgt, unl) for a, cur, tgt, unl in results}

        total       = len(ACHIEVEMENTS)
        unlocked    = sum(1 for _, _, _, u in results if u)
        hidden_all  = sum(1 for a in ACHIEVEMENTS if a.hidden)
        hidden_found = sum(1 for a, _, _, u in results if a.hidden and u)

        sections = []
        for cat in _CATS:
            cat_lines = []
            for a in ACHIEVEMENTS:
                if a.cat != cat:
                    continue
                _, cur, tgt, unl = by_id[a.id]
                title = l10n.get(f"ach_{a.id}_t", lang)
                desc  = l10n.get(f"ach_{a.id}_d", lang)
                if unl:
                    cat_lines.append(f"{a.emoji} **{title}** — {desc}  ✅")
                elif a.hidden:
                    cat_lines.append(l10n.get("ach_hidden_masked", lang))
                else:
                    cat_lines.append(f"{a.emoji} **{title}** — {desc}  `{_bar(cur, tgt)} {cur}/{tgt}`")
            if cat_lines:
                sections.append(f"**{l10n.get('ach_cat_' + cat, lang)}**\n" + "\n".join(cat_lines))

        _idx, rkey, remoji = rank_for(unlocked)
        rank_line = l10n.get(
            "ach_rank_line", lang,
            emoji=remoji, rank=l10n.get(f"rank_{rkey}", lang),
            unlocked=unlocked, total=total,
        )
        nxt = next_rank_threshold(unlocked)
        if nxt is not None:
            rank_line += " · " + l10n.get("ach_rank_next", lang, n=nxt)

        embed = discord.Embed(
            title=l10n.get("ach_title", lang),
            description=rank_line + "\n\n" + "\n\n".join(sections),
            colour=EMBED_COLOR,
        )
        embed.set_footer(text=l10n.get(
            "ach_summary", lang,
            unlocked=unlocked, total=total,
            hidden_found=hidden_found, hidden_total=hidden_all,
        ))
        await ctx.respond(embed=embed, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(AchievementsCog(bot))
