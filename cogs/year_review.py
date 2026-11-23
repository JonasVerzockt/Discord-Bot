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
cogs/year_review.py – Jahresrückblick.

  • 31.12. um 23:00 (Berliner Zeit): Community-Rückblick mit Guten-Rutsch-Gruß im Kanal
    YEAR_REVIEW_CHANNEL_ID (einmal je Jahr, auch nach Neustart nicht doppelt).
  • /rueckblick [jahr]   – persönlicher Rückblick, nur für dich sichtbar.
  • /rueckblick_post     – (Admin) Vorschau des Community-Posts oder sofort posten.
"""
import asyncio
import logging
from datetime import datetime, time as dtime

import discord
from discord.ext import commands, tasks

from config import YEAR_REVIEW_CHANNEL_ID, BOARD_PUBLIC_URL
from utils import year_review as YR
from utils import directory as D
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.timez import BERLIN
from cogs.server_settings import admin_or_manage_messages, allowed_channel

logger = logging.getLogger(__name__)
_COLOR = 0xD29922


def _shop_name(dom: str) -> str:
    sh = D.all_shops().get(dom)
    return sh["name"] if sh else dom


def _eur(v) -> str:
    return f"{v:.2f}".replace(".", ",") + " €"


def community_embed(d: dict, lang: str) -> discord.Embed:
    t = lambda k, **kw: l10n.get(k, lang, **kw)
    y = d["year"]
    e = discord.Embed(title=t("yr_title", year=y), color=_COLOR,
                      url=(f"{BOARD_PUBLIC_URL}/rueckblick/{y}" if BOARD_PUBLIC_URL else None))
    e.description = t("yr_intro", year=y, next=y + 1)
    rv = d["reviews"]
    lines = [t("yr_reviews", n=rv["count"])]
    if rv["avg"] is not None:
        lines.append(t("yr_reviews_avg", avg=f"{rv['avg']:.2f}".replace(".", ",")))
    if rv["most"]:
        lines.append(t("yr_most", list=", ".join(f"{_shop_name(s)} ({n})" for s, n in rv["most"])))
    if rv["best"]:
        lines.append(t("yr_best", list=", ".join(
            f"{_shop_name(s)} ({a:.2f})".replace(".", ",") for s, a, _ in rv["best"])))
    if d["fastest"]:
        s, a, n = d["fastest"]
        lines.append(t("yr_fastest", shop=_shop_name(s), d=f"{a:.1f}".replace(".", ","), n=n))
    e.add_field(name=t("yr_h_reviews"), value="\n".join(lines)[:1020], inline=False)

    lines = [t("yr_new_species", n=d["new_species"]),
             t("yr_new_shops", n=len(d["new_shops"]))]
    if d["new_shops"]:
        lines[-1] += ": " + ", ".join(d["new_shops"][:6]) + (" …" if len(d["new_shops"]) > 6 else "")
    for sp, shop, a, b, pct in d["drops"][:3]:
        lines.append(t("yr_drop", species=sp, shop=shop, a=_eur(a), b=_eur(b), p=pct))
    lines.append(t("yr_codes", n=d["codes"]))
    e.add_field(name=t("yr_h_market"), value="\n".join(lines)[:1020], inline=False)

    lines = [t("yr_keepers", n=YR.mask(d["keepers"])), t("yr_events", n=d["events"]),
             t("yr_achievements", n=YR.mask(d["achievements"])),
             t("yr_commands", n=YR.mask(d["commands"])), t("yr_notifications", n=YR.mask(d["notifications"]))]
    e.add_field(name=t("yr_h_community"), value="\n".join(lines), inline=False)
    i = d.get("inat") or {}
    if i.get("obs"):
        lines = [t("yr_inat_line", obs=i["obs"], species=i["species"]),
                 t("yr_inat_people", n=YR.mask(i["people"]))]
        if i.get("new"):
            lines.append(t("yr_inat_new", n=i["new"]))
        if i.get("top"):
            lines.append(t("yr_inat_top", name=i["top"][0], n=i["top"][1]))
        e.add_field(name=t("yr_h_inat"), value="\n".join(lines), inline=False)
    sw = d.get("swarm") or {}
    if sw.get("n"):
        lines = [t("yr_swarm_line", n=sw["n"], taxa=sw["taxa"]), t("yr_swarm_people", n=YR.mask(sw["people"]))]
        if sw.get("start"):
            lines.append(t("yr_swarm_start", name=sw["start"][0],
                           date=datetime.strptime(sw["start"][1], "%Y-%m-%d").strftime("%d.%m.")))
        if sw.get("top"):
            lines.append(t("yr_swarm_top", name=sw["top"][0], n=sw["top"][1]))
        if sw.get("peak"):
            lines.append(t("yr_swarm_peak", w=sw["peak"]))
        if sw.get("abroad"):
            lines.append(t("yr_swarm_abroad", n=sw["abroad"], c=sw.get("abroad_countries", 0)))
        e.add_field(name=t("yr_h_swarm"), value="\n".join(lines), inline=False)
    e.add_field(name=t("yr_h_personal"), value=t("yr_personal_hint", year=y), inline=False)
    e.set_footer(text=t("yr_footer", next=y + 1))
    return e


def personal_embed(p: dict, lang: str, user: discord.abc.User) -> discord.Embed:
    t = lambda k, **kw: l10n.get(k, lang, **kw)
    e = discord.Embed(title=t("yrp_title", year=p["year"], name=user.display_name), color=_COLOR)
    if not (p["commands"] or p["achievements"] or p["reviews"]
            or p["notifications"] or p["watches"] or p["tracked"] or p.get("inat", {}).get("obs")
            or p.get("swarm")):
        e.description = t("yrp_empty", year=p["year"])
        return e
    if p["reviews"]:
        lines = [t("yrp_reviews", n=p["reviews"])]
        if p["reviews_avg"] is not None:
            lines[0] += " · " + t("yrp_reviews_avg", avg=f"{p['reviews_avg']:.1f}".replace(".", ","))
        if p["reviews_top"]:
            lines.append(t("yrp_reviews_top", list=", ".join(
                f"{_shop_name(s)} ({n})" for s, n in p["reviews_top"])))
        if p["reviews_first"]:
            lines.append(t("yrp_reviews_first", date=datetime.strptime(
                p["reviews_first"], "%Y-%m-%d").strftime("%d.%m.%Y")))
        e.add_field(name=t("yrp_h_reviews"), value="\n".join(lines), inline=False)
    lines = [t("yrp_commands", n=p["commands"])]
    if p["top_commands"]:
        lines.append(t("yrp_top", list=", ".join(f"/{c} ({n})" for c, n in p["top_commands"])))
    if p["first"]:
        try:
            lines.append(t("yrp_first", date=datetime.fromisoformat(p["first"]).strftime("%d.%m.%Y")))
        except ValueError:
            pass
    e.add_field(name=t("yrp_h_bot"), value="\n".join(lines), inline=False)
    lines = [t(k, n=p[f]) for k, f in (("yrp_notifications", "notifications"),
                                        ("yrp_watches", "watches"), ("yrp_tracked", "tracked")) if p[f]]
    if p["rsvps"]:
        lines.append(t("yrp_rsvps", n=p["rsvps"]))
    if p.get("inat", {}).get("obs"):
        lines.append(t("yrp_inat", obs=p["inat"]["obs"], species=p["inat"]["species"]))
    if p.get("swarm"):
        lines.append(t("yrp_swarm", n=p["swarm"], taxa=p.get("swarm_taxa", 0)))
    if lines:                                    # Nullwerte ausgeblendet
        e.add_field(name=t("yrp_h_ants"), value="\n".join(lines), inline=False)
    if p["achievements"]:
        try:
            from utils.achievements import ACHIEVEMENTS
            emo = {a.id: a.emoji for a in ACHIEVEMENTS}
        except Exception:
            emo = {}
        names = [f"{emo.get(a, '🏅')} {l10n.get(f'ach_{a}_t', lang)}" for a in p["achievements"]]
        e.add_field(name=t("yrp_h_ach", n=len(names)), value="\n".join(names)[:1020], inline=False)
    e.set_footer(text=t("yrp_footer", next=p["year"] + 1))
    return e


class YearReviewCog(commands.Cog, name="YearReview"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.new_year_post.start()

    def cog_unload(self):
        self.new_year_post.cancel()

    async def _post(self, year: int) -> bool:
        if not YEAR_REVIEW_CHANNEL_ID:
            logger.warning("🎆 Jahresrückblick: YEAR_REVIEW_CHANNEL_ID nicht gesetzt, kein Post")
            return False
        ch = self.bot.get_channel(YEAR_REVIEW_CHANNEL_ID)
        if ch is None:
            logger.warning("🎆 Jahresrückblick: Kanal %s nicht gefunden", YEAR_REVIEW_CHANNEL_ID)
            return False
        lang = await get_user_lang(self.bot, 0, ch.guild.id if getattr(ch, "guild", None) else None)
        data = await YR.community(self.bot, year)
        await ch.send(embed=community_embed(data, lang))
        await execute_db(self.bot, "INSERT OR IGNORE INTO year_review_posted (year) VALUES (?)",
                         (year,), commit=True)
        logger.info("🎆 Jahresrückblick %s gepostet", year)
        return True

    @tasks.loop(time=dtime(hour=23, minute=0, tzinfo=BERLIN))
    async def new_year_post(self):
        now = datetime.now(BERLIN)
        if (now.month, now.day) != (12, 31):
            return
        done = await execute_db(self.bot, "SELECT 1 FROM year_review_posted WHERE year=?",
                                (now.year,), fetch=True)
        if done:
            return
        try:
            await self._post(now.year)
        except Exception as e:
            logger.error("❌ Jahresrückblick fehlgeschlagen: %s", e, exc_info=True)

    @new_year_post.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ── /rueckblick ───────────────────────────────────────────────────────────
    @discord.slash_command(
        name="rueckblick",
        description="Your personal year in review (only visible to you)",
        description_localizations={"de": "Dein persönlicher Jahresrückblick (nur für dich sichtbar)"},
    )
    @allowed_channel()
    async def rueckblick(
        self, ctx: discord.ApplicationContext,
        jahr: discord.Option(int, "Year (default: the year currently under review)", required=False, default=None,
                             min_value=2020, max_value=2100,
                             description_localizations={"de": "Jahr (Standard: das gerade betrachtete Jahr)"}),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        ry = YR.review_year()
        fy = await YR.first_year(self.bot)
        if not YR.is_open() and not jahr:
            # Ohne Jahr nur vom 1. November bis 31. Januar; sonst Hinweis plus Ameisen-Fakt.
            # Abgeschlossene Jahre gehen weiterhin über jahr:.
            from utils import ant_facts
            f = ant_facts.pick(lang)
            nxt = YR.next_open()
            e = discord.Embed(title=l10n.get("yrp_closed_h", lang),
                              description=l10n.get("yrp_closed", lang, year=nxt.year,
                                                   date=nxt.strftime("%d.%m.%Y")), color=_COLOR)
            e.add_field(name=l10n.get("yrp_fact_h", lang),
                        value=f"{f['text']}\n[{f['src']}]({f['url']})", inline=False)
            if fy <= ry:
                e.set_footer(text=l10n.get("yrp_closed_past", lang, year=ry))
            await ctx.followup.send(embed=e, ephemeral=True)
            return
        year = jahr or ry
        if year < fy or year > ry:
            await ctx.followup.send(l10n.get("yrp_bad_year", lang, year=year, first=fy, last=ry),
                                    ephemeral=True)
            return
        p = await YR.personal(self.bot, str(ctx.author.id), year)
        await ctx.followup.send(embed=personal_embed(p, lang, ctx.author), ephemeral=True)

    # ── /rueckblick_post (Admin) ──────────────────────────────────────────────
    @discord.slash_command(
        name="rueckblick_post",
        description="🔒 [Admin] Preview or post the community year in review now",
        description_localizations={"de": "🔒 [Admin] Community-Jahresrückblick als Vorschau zeigen oder jetzt posten"},
    )
    @discord.default_permissions(manage_messages=True)
    @admin_or_manage_messages()
    async def rueckblick_post(
        self, ctx: discord.ApplicationContext,
        posten: discord.Option(bool, "True = post to the channel now, False = preview only",
                               default=False, description_localizations={
                                   "de": "Ja = jetzt im Kanal posten, Nein = nur Vorschau"}),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        year = datetime.now(BERLIN).year
        if posten:
            ok = await self._post(year)
            await ctx.followup.send("✅ gepostet" if ok else "❌ Kanal fehlt (YEAR_REVIEW_CHANNEL_ID)",
                                    ephemeral=True)
            return
        data = await YR.community(self.bot, year)
        await ctx.followup.send(embed=community_embed(data, lang), ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(YearReviewCog(bot))
