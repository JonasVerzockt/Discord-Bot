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
cogs/inat_tracker.py – iNaturalist-Statistik im iNat-Kanal (dauerhaft, ohne Wettbewerb).

  • Wer einen iNaturalist-Beobachtungslink im Kanal postet, bekommt ihn gemerkt.
    Gezählt wird er erst, wenn das eigene iNat-Konto per /inat_verknuepfen bestätigt
    ist und die Beobachtung diesem Konto gehört (fremde Beobachtungen zählen nie).
  • Reaktionen: ✅ gezählt · 🆕 neue Art für dich · 🌟 neu für die Community ·
    🔗 Konto noch nicht verknüpft · ❌ fremde Beobachtung · ⏳ iNat gerade nicht erreichbar.
  • /inat_statistik zeigt die eigenen Zahlen, „Statistik“ im Kanal das Treppchen
    (Arten im laufenden Jahr). Am 1. jedes Monats postet der Bot das Treppchen selbst.
  • Bestimmungen werden stündlich (neue) bzw. täglich (letzte 6 Monate) aktualisiert.
Logik und Speicherung: utils/inat.py. Die frühere Google-Tabelle wird nicht mehr genutzt.
"""
import asyncio
import io
import logging
from datetime import datetime, time as dtime, timedelta

import discord
from discord.ext import commands, tasks

from cogs.server_settings import allowed_channel
from utils import inat as I
from utils.localization import l10n, get_guild_lang, get_user_lang
from utils.timez import BERLIN

logger = logging.getLogger(__name__)

INAT_CHANNEL_ID = 1166491005814579300
_TRIGGERS = {"statistik", "rangliste", "statistics", "statistiko"}
_MANUAL_COOLDOWN = 3 * 3600


def _render_ranking_png(entries: list, title: str, col_labels: list, more: str) -> bytes:
    """
    Rendert die Rangliste lokal als PNG: die obersten drei Ränge als farbiges
    Treppchen (Gold/Silber/Bronze), Platz 4+ als Tabelle. Personen mit gleicher
    Artenzahl teilen sich denselben Rang und dieselbe Treppchen-Stufe
    (Competition-Ranking: 1, 1, 3, …). Gibt PNG-Bytes zurück.
    Läuft blockierend → vom Aufrufer in asyncio.to_thread() aufrufen.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    BG, FG = "#0d1117", "#e6edf3"
    GOLD, SILVER, BRONZE = "#FFD700", "#C0C0C0", "#CD7F32"
    STEP_COLORS = [GOLD, SILVER, BRONZE]

    def _clip(s, n):
        s = str(s)
        return s if len(s) <= n else s[: n - 1] + "…"

    # Nach Artenzahl gruppieren → geteilte Ränge (gleiche Anzahl = gleicher Rang)
    places = []          # (rang, anzahl, [namen])
    i, rank = 0, 1
    while i < len(entries):
        val = entries[i][2]
        names = []
        j = i
        while j < len(entries) and entries[j][2] == val:
            names.append(entries[j][1])
            j += 1
        places.append((rank, val, names))
        rank += (j - i)
        i = j

    podium    = places[:3]
    rest      = places[3:]
    rest_rows = [(rg, nm, val) for (rg, val, names) in rest for nm in names]

    fig_h = 5.6 + 0.42 * len(rest_rows)
    fig = plt.figure(figsize=(8, fig_h), dpi=150)
    fig.patch.set_facecolor(BG)

    if rest_rows:
        gs = fig.add_gridspec(2, 1, height_ratios=[3.4, 0.8 + 0.42 * len(rest_rows)], hspace=0.18)
        ax  = fig.add_subplot(gs[0])
        axt = fig.add_subplot(gs[1])
    else:
        ax  = fig.add_subplot(111)
        axt = None

    ax.set_facecolor(BG)
    fig.suptitle(title, color=FG, fontsize=22, fontweight="bold", y=0.97)

    slot_for_step = {0: 1, 1: 0, 2: 2}        # Stufe -> x-Position (2. links, 1. Mitte, 3. rechts)
    step_heights  = {0: 3.0, 1: 2.0, 2: 1.0}  # gleichmäßiges Treppchen; echte Werte stehen als Label
    maxh = 3.0

    for step_idx, (rg, val, names) in enumerate(podium):
        xpos = slot_for_step.get(step_idx, step_idx)
        col  = STEP_COLORS[step_idx]
        h    = step_heights.get(step_idx, 1.0)
        ax.bar(xpos, h, width=0.72, color=col, edgecolor="white", linewidth=1.4, zorder=3)
        ax.text(xpos, h / 2, str(rg),
                ha="center", va="center", color=BG, fontsize=30, fontweight="bold", zorder=4)
        ax.text(xpos, h + maxh * 0.04, f"{val}", ha="center", va="bottom",
                color=FG, fontsize=14, fontweight="bold")
        shown = [_clip(n, 16) for n in names[:5]]
        if len(names) > 5:
            shown.append(more.format(n=len(names) - 5))
        ax.text(xpos, -maxh * 0.06, "\n".join(shown), ha="center", va="top",
                color=col, fontsize=11, fontweight="bold", linespacing=1.35)

    ax.set_xlim(-0.7, 2.7)
    ax.set_ylim(-maxh * 0.34, maxh * 1.22)
    ax.axis("off")

    if axt is not None:
        axt.axis("off")
        cell_text = [[str(rg), _clip(nm, 30), str(val)] for (rg, nm, val) in rest_rows]
        tbl = axt.table(
            cellText=cell_text,
            colLabels=col_labels,
            cellLoc="center", loc="center",
            colWidths=[0.18, 0.62, 0.20],
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(11)
        tbl.scale(1, 1.45)
        for (row, col), cell in tbl.get_celld().items():
            cell.set_edgecolor("#30363d")
            if row == 0:
                cell.set_facecolor("#1f6feb")
                cell.set_text_props(color="white", fontweight="bold")
            else:
                cell.set_facecolor("#161b22" if row % 2 else "#0d1117")
                cell.set_text_props(color=FG)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


class _CheckView(discord.ui.View):
    """Button „Jetzt prüfen“ unter der Verknüpfungs-Anleitung."""

    def __init__(self, cog, user_id: int, lang: str):
        super().__init__(timeout=3600)
        self.cog, self.user_id, self.lang = cog, user_id, lang
        self.children[0].label = l10n.get("inat_link_check_btn", lang)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(style=discord.ButtonStyle.success, emoji="✅")
    async def check(self, button, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = await self.cog._verify_text(interaction.user.id, self.lang)
        await interaction.followup.send(msg, ephemeral=True)


class InatTrackerCog(commands.Cog, name="InatTracker"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self._last_manual: datetime | None = None
        self._hinted: set[int] = set()
        self.hourly.start()
        self.daily.start()

    def cog_unload(self):
        self.hourly.cancel()
        self.daily.cancel()

    # ── Treppchen ─────────────────────────────────────────────────────────────
    async def _ranking_file(self, guild: discord.Guild, year: int, lang: str):
        rows = await I.ranking(self.bot, year)
        entries = []
        for r in rows:
            m = guild.get_member(int(r["discord_id"])) if guild else None
            if m is None or not r["species"]:
                continue
            entries.append((None, f"{m.display_name} ({r['obs']})", r["species"]))
        if not entries:
            return None
        png = await asyncio.to_thread(
            _render_ranking_png, entries, l10n.get("inat_png_title", lang, year=year),
            [l10n.get("inat_png_col_place", lang), l10n.get("inat_png_col_name", lang),
             l10n.get("inat_png_col_species", lang)], l10n.get("inat_png_more", lang))
        return discord.File(io.BytesIO(png), filename=f"inat_{year}.png")

    async def _post_ranking(self, channel, year: int, caption_key: str) -> bool:
        lang = await get_guild_lang(self.bot, channel.guild.id)
        f = await self._ranking_file(channel.guild, year, lang)
        if f is None:
            return False
        await channel.send(l10n.get(caption_key, lang, year=year), file=f)
        return True

    # ── Kanal ─────────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != INAT_CHANNEL_ID:
            return
        if message.content.strip().lower() in _TRIGGERS:
            now = datetime.now(BERLIN)
            if self._last_manual and (now - self._last_manual).total_seconds() < _MANUAL_COOLDOWN:
                await message.add_reaction("⏱️")
                return
            self._last_manual = now
            ok = await self._post_ranking(message.channel, now.year, "inat_ranking_caption")
            await message.add_reaction("📊" if ok else "🤷")
            return
        ids = [int(x) for x in I.OBS_RE.findall(message.content or "")]
        if not ids:
            return
        new = await I.store_posts(self.bot, message.author.id, message.id, ids)
        if not new:
            return
        await I.update_obs(self.bot, new)
        await self._react(message, new)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Gelöschter Post im iNat-Kanal → zugehörige Beobachtungen zählen nicht mehr."""
        if payload.channel_id == INAT_CHANNEL_ID:
            await I.execute_db(self.bot, "DELETE FROM inat_obs WHERE message_id=?",
                               (str(payload.message_id),), commit=True)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.channel_id == INAT_CHANNEL_ID:
            for mid in payload.message_ids:
                await I.execute_db(self.bot, "DELETE FROM inat_obs WHERE message_id=?",
                                   (str(mid),), commit=True)

    async def _react(self, message: discord.Message, obs_ids: list[int]):
        states = {o: await I.obs_state(self.bot, o, message.author.id) for o in obs_ids}
        marks: list[str] = []
        if "counted" in states.values():
            marks.append("✅")
            for o, st in states.items():
                if st == "counted":
                    mine, community = await I.novelty(self.bot, o, message.author.id)
                    if community and "🌟" not in marks:
                        marks.append("🌟")
                    elif mine and "🆕" not in marks:
                        marks.append("🆕")
        if "foreign" in states.values():
            marks.append("❌")
        if "unlinked" in states.values():
            marks.append("🔗")
        if "pending" in states.values():
            marks.append("⏳")
        for m in marks:
            try:
                await message.add_reaction(m)
            except discord.HTTPException:
                pass
        if "unlinked" in states.values() and message.author.id not in self._hinted:
            self._hinted.add(message.author.id)
            lang = await get_user_lang(self.bot, message.author.id, message.guild.id if message.guild else None)
            try:
                await message.reply(l10n.get("inat_hint_link", lang), delete_after=90,
                                    mention_author=False)
            except discord.HTTPException:
                pass
        if "counted" in states.values():
            try:
                from utils.achievements import check_and_grant
                lang = await get_user_lang(self.bot, message.author.id, message.guild.id if message.guild else None)
                await check_and_grant(self.bot, message.author, lang)
            except Exception:
                pass

    # ── Slash-Befehle ─────────────────────────────────────────────────────────
    async def _verify_text(self, user_id: int, lang: str) -> str:
        status, u = await I.verify_link(self.bot, user_id)
        if status == "ok":
            st = await I.user_stats(self.bot, user_id)
            return l10n.get("inat_link_ok", lang, login=u["login"], obs=st["obs"], species=st["species"])
        return l10n.get(f"inat_link_{status}", lang)

    @discord.slash_command(
        name="inat_verknuepfen",
        description="Link your iNaturalist account (proof via a code in your iNat profile)",
        description_localizations={"de": "Dein iNaturalist-Konto verknüpfen (Nachweis per Code im iNat-Profil)"},
    )
    @allowed_channel()
    async def inat_verknuepfen(
        self, ctx: discord.ApplicationContext,
        login: discord.Option(str, "Your iNaturalist username (empty = check pending code)",
                              description_localizations={"de": "Dein iNaturalist-Benutzername (leer = offenen Code prüfen)"},
                              required=False, default=None),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        if not login:
            await ctx.followup.send(await self._verify_text(ctx.author.id, lang), ephemeral=True)
            return
        login = login.strip().lstrip("@")
        if not I.LOGIN_RE.match(login):
            await ctx.followup.send(l10n.get("inat_link_bad_login", lang), ephemeral=True)
            return
        code = await I.start_link(self.bot, ctx.author.id, login)
        await ctx.followup.send(l10n.get("inat_link_howto", lang, login=login, code=code),
                                view=_CheckView(self, ctx.author.id, lang), ephemeral=True)

    @discord.slash_command(
        name="inat_trennen",
        description="Unlink your iNaturalist account (your posts stop counting)",
        description_localizations={"de": "iNaturalist-Verknüpfung lösen (deine Posts zählen dann nicht mehr)"},
    )
    @allowed_channel()
    async def inat_trennen(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        ok = await I.unlink(self.bot, ctx.author.id)
        await ctx.respond(l10n.get("inat_unlink_ok" if ok else "inat_unlink_none", lang), ephemeral=True)

    @discord.slash_command(
        name="inat_statistik",
        description="Your iNaturalist ant statistics: observations and species",
        description_localizations={"de": "Deine iNaturalist-Ameisenstatistik: Beobachtungen und Arten"},
    )
    @allowed_channel()
    async def inat_statistik(
        self, ctx: discord.ApplicationContext,
        jahr: discord.Option(int, "Year (default: current year)", required=False, default=None,
                             min_value=2000, max_value=2100,
                             description_localizations={"de": "Jahr (Standard: aktuelles Jahr)"}),
    ):
        await ctx.defer(ephemeral=True)
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        acc = await I.account_of(self.bot, ctx.author.id)
        if not acc:
            await ctx.followup.send(l10n.get("inat_stats_unlinked", lang), ephemeral=True)
            return
        year = jahr or datetime.now(BERLIN).year
        y = await I.user_stats(self.bot, ctx.author.id, year)
        a = await I.user_stats(self.bot, ctx.author.id)
        rank = next((i + 1 for i, r in enumerate(await I.ranking(self.bot, year))
                     if r["discord_id"] == str(ctx.author.id)), None)
        e = discord.Embed(title=l10n.get("inat_stats_title", lang, login=acc["inat_login"]), color=0x74AC00,
                          url=f"https://www.inaturalist.org/people/{acc['inat_login']}")
        e.add_field(name=l10n.get("inat_stats_year_h", lang, year=year), inline=True,
                    value=l10n.get("inat_stats_line", lang, obs=y["obs"], species=y["species"])
                    + (f"\n{l10n.get('inat_stats_rank', lang, rank=rank)}" if rank else ""))
        e.add_field(name=l10n.get("inat_stats_all_h", lang), inline=True,
                    value=l10n.get("inat_stats_line", lang, obs=a["obs"], species=a["species"]))
        if y["taxa"]:
            names = ", ".join(f"*{n}*" for n in y["taxa"][:40])
            if len(y["taxa"]) > 40:
                names += " " + l10n.get("dir_more", lang, n=len(y["taxa"]) - 40)
            e.add_field(name=l10n.get("inat_stats_taxa_h", lang, year=year), value=names[:1024], inline=False)
        e.set_footer(text=l10n.get("inat_stats_footer", lang))
        await ctx.followup.send(embed=e, ephemeral=True)

    # ── Hintergrund ───────────────────────────────────────────────────────────
    @tasks.loop(minutes=60)
    async def hourly(self):
        """Noch ungeprüfte Beobachtungen (z. B. API war down) nachholen und Reaktionen setzen."""
        rows = await I.execute_db(self.bot, "SELECT obs_id, message_id, discord_id FROM inat_obs "
                                            "WHERE checked_at IS NULL", fetch=True) or []
        if not rows:
            return
        if not await I.update_obs(self.bot, [r["obs_id"] for r in rows]):
            return
        ch = self.bot.get_channel(INAT_CHANNEL_ID)
        by_msg: dict = {}
        for r in rows:
            by_msg.setdefault(r["message_id"], []).append(r["obs_id"])
        for mid, ids in by_msg.items():
            try:
                msg = await ch.fetch_message(int(mid))
                await msg.remove_reaction("⏳", self.bot.user)
                await self._react(msg, ids)
            except Exception:
                pass

    @hourly.before_loop
    async def _before_hourly(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dtime(hour=4, minute=17, tzinfo=BERLIN))
    async def daily(self):
        """Bestimmungen der letzten 6 Monate aktualisieren; am 1. des Monats Treppchen posten."""
        n = await I.refresh(self.bot, full=True)
        logger.info("🐜 iNat: %s Beobachtungen aktualisiert", n)
        now = datetime.now(BERLIN)
        if now.day != 1:
            return
        month = now.strftime("%Y-%m")
        done = await I.execute_db(self.bot, "SELECT 1 FROM inat_posted_months WHERE month=?", (month,), fetch=True)
        ch = self.bot.get_channel(INAT_CHANNEL_ID)
        if done or ch is None:
            return
        year = (now - timedelta(days=1)).year          # am 1. Januar: Endstand des Vorjahres
        key = "inat_ranking_year_end" if now.month == 1 else "inat_ranking_monthly"
        try:
            await self._post_ranking(ch, year, key)
        finally:
            await I.execute_db(self.bot, "INSERT OR IGNORE INTO inat_posted_months(month) VALUES (?)",
                               (month,), commit=True)

    @daily.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()


def setup(bot: discord.Bot):
    bot.add_cog(InatTrackerCog(bot))
