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
from cogs.server_settings import allowed_channel
from utils.achievements import check_and_grant

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


def _latest_variant_min_sync(variant_id: int):
    """Letzter bekannter Einzelpreis + Währung einer Variante, oder None."""
    if not PRICE_HISTORY_DB.exists():
        return None
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT price, currency_iso FROM variant_price_history "
            "WHERE variant_id=? ORDER BY recorded_at DESC LIMIT 1",
            (variant_id,),
        )
        row = cur.fetchone()
        return (float(row[0]), row[1] or "EUR") if row else None
    finally:
        conn.close()


def _row_title(row) -> str:
    """Anzeigetitel inkl. Variantenname."""
    base = row["product_title"] or row["species"] or f"#{row['product_id']}"
    try:
        vt = row["variant_title"]
    except (IndexError, KeyError):
        vt = ""
    return f"{base} – {vt}" if vt else base


class _TargetProductSelect(discord.ui.Select):
    def __init__(self, rows: list, lang: str, mode: str | None, target_price):
        self.lang    = lang
        self.mode    = mode          # 'additional' | 'replace' | None (aus)
        self.target  = target_price
        self._rows   = {f"{r['product_id']}:{r['variant_id'] or 0}": r for r in rows}
        options = []
        for r in rows[:25]:
            title = _row_title(r).strip()
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
        title = _row_title(row)

        if self.mode is None:
            await execute_db(
                interaction.client,
                "UPDATE user_price_tracking SET target_price=NULL, target_mode=NULL "
                "WHERE user_id=? AND product_id=? AND variant_id=?",
                (str(interaction.user.id), pid, vid),
                commit=True,
            )
            await interaction.followup.send(
                l10n.get("st_cleared", lang, title=title), ephemeral=True
            )
            return

        await execute_db(
            interaction.client,
            "UPDATE user_price_tracking SET target_price=?, target_mode=? "
            "WHERE user_id=? AND product_id=? AND variant_id=?",
            (self.target, self.mode, str(interaction.user.id), pid, vid),
            commit=True,
        )
        try:
            await check_and_grant(interaction.client, interaction.user, lang)
        except Exception:
            pass

        latest   = await asyncio.to_thread(
            _latest_variant_min_sync if vid > 0 else _latest_min_sync,
            vid if vid > 0 else pid,
        )
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
    @allowed_channel()
    async def set_target(
        self,
        ctx: discord.ApplicationContext,
        mode: discord.Option(  # type: ignore[valid-type]
            str,
            "zusätzlich = extra DM, ersetzt = nur Ziel-DM, aus = entfernen", description_localizations={"de": 'zusätzlich = extra DM, ersetzt = nur Ziel-DM, aus = entfernen', "en-US": 'zusätzlich = extra DM, ersetzt = only target DM, aus = remove'},
            choices=[
                discord.OptionChoice(name="zusätzlich", value="zusätzlich", name_localizations={"de": "zusätzlich", "en-US": "additional"}),
                discord.OptionChoice(name="ersetzt", value="ersetzt", name_localizations={"de": "ersetzt", "en-US": "replace"}),
                discord.OptionChoice(name="aus", value="aus", name_localizations={"de": "aus", "en-US": "off"}),
            ],
        ),
        target_price: discord.Option(  # type: ignore[valid-type]
            float,
            "Zielpreis in Shop-Währung (nicht nötig bei 'aus')", description_localizations={"de": "Zielpreis in Shop-Währung (nicht nötig bei 'aus')", "en-US": "Target price in shop currency (not needed for 'aus')"},
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
            "SELECT product_id, variant_id, variant_title, product_title, species, shop_name, currency_iso "
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
