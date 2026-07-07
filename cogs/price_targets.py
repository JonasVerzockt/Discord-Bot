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
cogs/price_targets.py – /set_target: Zielpreis pro beobachtetem Produkt setzen.

Modi (pro Tracking wählbar):
  • zusätzlich – weiter DM bei jeder Änderung + extra 🎯-DM beim Erreichen
  • ersetzt    – nur noch die 🎯-DM beim Erreichen (keine Zwischenänderungen)
  • aus        – Zielpreis wieder entfernen

Die Auswertung passiert im Preis-Check-Loop in cogs/price_tracking.py.
Der Zielpreis gilt in der Shop-Währung des Produkts.
"""
import asyncio
import logging
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

from config import DATA_DIRECTORY
from utils.db import execute_db
from utils.localization import l10n, get_user_lang

logger = logging.getLogger(__name__)

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"

_MODE_MAP = {"zusätzlich": "additional", "ersetzt": "replace", "aus": None}


def _latest_min_sync(product_id: int):
    """Letzter bekannter min_price + Währung eines Produkts, oder None."""
    if not PRICE_HISTORY_DB.exists():
        return None
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT min_price, currency_iso FROM product_price_history "
            "WHERE product_id=? ORDER BY recorded_at DESC LIMIT 1",
            (product_id,),
        )
        row = cur.fetchone()
        return (float(row[0]), row[1] or "EUR") if row else None
    finally:
        conn.close()


class _TargetProductSelect(discord.ui.Select):
    def __init__(self, rows: list, lang: str, mode: str | None, target_price):
        self.lang    = lang
        self.mode    = mode          # 'additional' | 'replace' | None (aus)
        self.target  = target_price
        self._rows   = {str(r["product_id"]): r for r in rows}
        options = []
        for r in rows[:25]:
            title = (r["product_title"] or r["species"] or f"#{r['product_id']}").strip()
            desc  = (r["shop_name"] or "").strip()
            options.append(discord.SelectOption(
                label=(title[:95] or f"#{r['product_id']}"),
                value=str(r["product_id"]),
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
        pid  = int(self.values[0])
        title = row["product_title"] or row["species"] or f"#{pid}"

        if self.mode is None:
            await execute_db(
                interaction.client,
                "UPDATE user_price_tracking SET target_price=NULL, target_mode=NULL "
                "WHERE user_id=? AND product_id=?",
                (str(interaction.user.id), pid),
                commit=True,
            )
            await interaction.followup.send(
                l10n.get("st_cleared", lang, title=title), ephemeral=True
            )
            return

        await execute_db(
            interaction.client,
            "UPDATE user_price_tracking SET target_price=?, target_mode=? "
            "WHERE user_id=? AND product_id=?",
            (self.target, self.mode, str(interaction.user.id), pid),
            commit=True,
        )

        latest   = await asyncio.to_thread(_latest_min_sync, pid)
        currency = latest[1] if latest else (row["currency_iso"] or "EUR")
        mode_lbl = l10n.get(
            "st_mode_additional" if self.mode == "additional" else "st_mode_replace", lang
        )
        text = l10n.get(
            "st_set", lang,
            title=title, price=f"{self.target:.2f} {currency}", mode=mode_lbl,
        )
        if latest and latest[0] <= self.target:
            text += "\n" + l10n.get("st_already", lang, price=f"{latest[0]:.2f} {currency}")

        await interaction.followup.send(text, ephemeral=True)


class SetTargetView(discord.ui.View):
    def __init__(self, owner_id: int, rows: list, lang: str, mode: str | None,
                 target_price, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.lang     = lang
        self.add_item(_TargetProductSelect(rows, lang, mode, target_price))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                l10n.get("ph_not_your_menu", self.lang), ephemeral=True
            )
            return False
        return True


class PriceTargetsCog(commands.Cog, name="PriceTargets"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot

    @discord.slash_command(
        name="set_target",
        description="Set a target price for a tracked product (alert when reached).",
        description_localizations={"de": "Zielpreis für ein beobachtetes Produkt setzen (Alarm bei Erreichen)."},
    )
    @commands.guild_only()
    async def set_target(
        self,
        ctx: discord.ApplicationContext,
        mode: discord.Option(  # type: ignore[valid-type]
            str,
            "zusätzlich = extra DM, ersetzt = nur Ziel-DM, aus = entfernen",
            choices=["zusätzlich", "ersetzt", "aus"],
        ),
        target_price: discord.Option(  # type: ignore[valid-type]
            float,
            "Zielpreis in Shop-Währung (nicht nötig bei 'aus')",
            required=False,
            default=None,
        ),
    ):
        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        internal = _MODE_MAP[mode]

        if internal is not None and (target_price is None or target_price <= 0):
            await ctx.respond(l10n.get("st_invalid_price", lang), ephemeral=True)
            return

        rows = await execute_db(
            self.bot,
            "SELECT product_id, product_title, species, shop_name, currency_iso "
            "FROM user_price_tracking WHERE user_id=? ORDER BY added_at DESC",
            (str(ctx.author.id),),
            fetch=True,
        )
        if not rows:
            await ctx.respond(l10n.get("ph_no_tracking", lang), ephemeral=True)
            return

        view = SetTargetView(ctx.author.id, rows, lang, internal, target_price)
        await ctx.respond(l10n.get("st_select", lang), view=view, ephemeral=True)


def setup(bot: discord.Bot):
    bot.add_cog(PriceTargetsCog(bot))
