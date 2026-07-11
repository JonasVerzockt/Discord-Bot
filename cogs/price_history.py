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
cogs/price_history.py – /price_history: Preisverlauf eines beobachteten Produkts
als lokal gerendertes Liniendiagramm (matplotlib) inkl. Bestpreis-Marker.

Datenquelle: price_history.db (Tabelle product_price_history), vom Grabber befüllt.
Der Grabber schreibt nur bei Preisänderung eine Zeile → die Reihe ist treppenförmig
(steps-post).
"""
import io
import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from config import DATA_DIRECTORY
from utils.db import execute_db
from utils.localization import l10n, get_user_lang

logger = logging.getLogger(__name__)

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"


# ── Datenzugriff ────────────────────────────────────────────────────────────────

def _get_history_sync(product_id: int):
    """
    Liest den gesamten Preisverlauf eines Produkts aus price_history.db.
    Rückgabe: (points, currency) mit points = [(datetime, min, max), …] aufsteigend,
    oder None wenn keine Daten vorliegen.
    """
    if not PRICE_HISTORY_DB.exists():
        return None
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT recorded_at, min_price, max_price, currency_iso "
            "FROM product_price_history WHERE product_id=? "
            "ORDER BY recorded_at ASC",
            (product_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    points = []
    currency = "EUR"
    for recorded_at, mn, mx, cur_iso in rows:
        try:
            ts = datetime.fromisoformat(str(recorded_at))
        except ValueError:
            continue
        points.append((ts, float(mn), float(mx)))
        currency = cur_iso or currency
    return (points, currency) if points else None


def _get_variant_history_sync(variant_id: int):
    """Preisverlauf einer Variante aus variant_price_history (price als min=max)."""
    if not PRICE_HISTORY_DB.exists():
        return None
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT recorded_at, price, currency_iso FROM variant_price_history "
            "WHERE variant_id=? ORDER BY recorded_at ASC",
            (variant_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    points = []
    currency = "EUR"
    for recorded_at, pr, cur_iso in rows:
        try:
            ts = datetime.fromisoformat(str(recorded_at))
        except ValueError:
            continue
        points.append((ts, float(pr), float(pr)))
        currency = cur_iso or currency
    return (points, currency) if points else None


def _ph_title(row) -> str:
    base = row["product_title"] or row["species"] or f"#{row['product_id']}"
    try:
        vt = row["variant_title"]
    except (IndexError, KeyError):
        vt = ""
    return f"{base} – {vt}" if vt else base


# ── Rendering ───────────────────────────────────────────────────────────────────

def _render_history_png(title: str, points: list, currency: str, labels: dict):
    """
    Rendert den Preisverlauf als Step-Chart und markiert das historische Tief.
    Rückgabe: (png_bytes, best_price, best_date).
    """
    # An "jetzt" verlängern, damit die letzte (flache) Stufe bis heute reicht.
    pts = list(points)
    now = datetime.now()
    if pts[-1][0] < now:
        pts.append((now, pts[-1][1], pts[-1][2]))

    xs   = [p[0] for p in pts]
    mins = [p[1] for p in pts]
    maxs = [p[2] for p in pts]

    # Bestpreis nur aus echten Datenpunkten (ohne künstlichen jetzt-Punkt).
    best_idx   = min(range(len(points)), key=lambda i: points[i][1])
    best_price = points[best_idx][1]
    best_date  = points[best_idx][0]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    has_band = any(mx > mn for _, mn, mx in points)
    if has_band:
        ax.fill_between(xs, mins, maxs, step="post", alpha=0.15, color="#3b82f6")
        ax.step(xs, maxs, where="post", color="#93c5fd", linewidth=1.2, label=labels["max"])
    ax.step(xs, mins, where="post", color="#2563eb", linewidth=2.0, label=labels["min"])

    ax.scatter([best_date], [best_price], color="#16a34a", zorder=5, s=60)
    ax.annotate(
        f"▼ {best_price:.2f} {currency}",
        xy=(best_date, best_price),
        xytext=(0, -18), textcoords="offset points",
        ha="center", color="#16a34a", fontweight="bold", fontsize=9,
    )

    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_ylabel(f"{labels['price']} ({currency})")
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m."))
    fig.autofmt_xdate(rotation=30)
    if has_band:
        ax.legend(loc="best", fontsize=8)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), best_price, best_date


# ── UI ───────────────────────────────────────────────────────────────────────────

class _HistoryProductSelect(discord.ui.Select):
    def __init__(self, rows: list, lang: str):
        self.lang  = lang
        self._rows = {f"{r['product_id']}:{r['variant_id'] or 0}": r for r in rows}
        options = []
        for r in rows[:25]:
            title = _ph_title(r).strip()
            desc  = (r["shop_name"] or "").strip()
            options.append(discord.SelectOption(
                label=(title[:95] or f"#{r['product_id']}"),
                value=f"{r['product_id']}:{r['variant_id'] or 0}",
                description=(desc[:95] or None),
            ))
        super().__init__(
            placeholder=l10n.get("ph_select_placeholder", lang),
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = self.lang
        row  = self._rows[self.values[0]]
        pid_str, _, vid_str = self.values[0].partition(":")
        pid  = int(pid_str)
        vid  = int(vid_str or 0)

        if vid > 0:
            data = await asyncio.to_thread(_get_variant_history_sync, vid)
        else:
            data = await asyncio.to_thread(_get_history_sync, pid)
        if not data:
            await interaction.followup.send(l10n.get("ph_no_data", lang), ephemeral=True)
            return
        points, currency = data

        title = _ph_title(row)
        if row["shop_name"]:
            title = f"{title} · {row['shop_name']}"

        labels = {
            "price": l10n.get("ph_axis_price", lang),
            "min":   l10n.get("ph_legend_min", lang),
            "max":   l10n.get("ph_legend_max", lang),
        }
        try:
            png, best_price, best_date = await asyncio.to_thread(
                _render_history_png, title[:80], points, currency, labels
            )
        except Exception as e:
            logger.error(f"❌ price_history Render-Fehler (pid={pid}): {e}", exc_info=True)
            await interaction.followup.send(l10n.get("ph_no_data", lang), ephemeral=True)
            return

        caption = l10n.get("ph_caption", lang, title=title)
        caption += "\n" + l10n.get(
            "ph_best", lang,
            price=f"{best_price:.2f} {currency}",
            date=best_date.strftime("%d.%m.%Y"),
        )
        if points[-1][1] <= best_price:
            caption += "\n" + l10n.get("ph_current_best", lang)

        buf = io.BytesIO(png)
        buf.seek(0)
        await interaction.followup.send(
            caption,
            file=discord.File(buf, filename="price_history.png"),
            ephemeral=True,
        )


class PriceHistoryView(discord.ui.View):
    def __init__(self, owner_id: int, rows: list, lang: str, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.lang     = lang
        self.add_item(_HistoryProductSelect(rows, lang))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                l10n.get("ph_not_your_menu", self.lang), ephemeral=True
            )
            return False
        return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class PriceHistoryCog(commands.Cog, name="PriceHistory"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="price_history",
        description="Show the price history of a tracked product as a chart.",
        description_localizations={"de": "Preisverlauf eines beobachteten Produkts als Diagramm anzeigen."},
    )
    @commands.guild_only()
    async def price_history(self, ctx: discord.ApplicationContext):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        rows = await execute_db(
            self.bot,
            "SELECT product_id, variant_id, variant_title, product_title, species, shop_name, currency_iso "
            "FROM user_price_tracking WHERE user_id=? ORDER BY added_at DESC",
            (str(ctx.author.id),),
            fetch=True,
        )
        if not rows:
            await ctx.respond(l10n.get("ph_no_tracking", lang), ephemeral=True)
            return

        view = PriceHistoryView(ctx.author.id, rows, lang)
        await ctx.respond(l10n.get("ph_select", lang), view=view, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(PriceHistoryCog(bot))
