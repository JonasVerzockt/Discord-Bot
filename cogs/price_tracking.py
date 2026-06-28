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
cogs/price_tracking.py – Preis-Tracking für Ameisenprodukte.

Slash Commands:
  /track_price        – Interaktiv Produkt(e) zum Preis-Tracking hinzufügen
  /my_price_tracking  – Alle beobachteten Produkte anzeigen (mit aktuellen Preisen)
  /untrack_price      – Produkte aus dem Tracking entfernen

Background Task:
  check_price_changes (stündlich) – liest price_history.db, sendet DM bei Preisänderung
"""
import asyncio
import logging
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands, tasks

from config import DATA_DIRECTORY
from utils.db import execute_db
from utils.localization import l10n, get_user_lang
from utils.availability import load_shop_data, normalize_species_name, format_rating
from utils.currency import ensure_rates, format_price

logger = logging.getLogger(__name__)

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"

# ── SQLite-Helfer (laufen im ThreadPool) ──────────────────────────────────────

def _get_latest_price_sync(product_id: int) -> tuple[float, float, str] | None:
    """Gibt (min_price, max_price, currency_iso) des letzten Eintrags zurück, oder None."""
    if not PRICE_HISTORY_DB.exists():
        return None
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT min_price, max_price, currency_iso "
            "FROM product_price_history WHERE product_id=? "
            "ORDER BY recorded_at DESC LIMIT 1",
            (product_id,),
        )
        row = cur.fetchone()
        return (row[0], row[1], row[2]) if row else None
    finally:
        conn.close()


def _get_latest_prices_sync(product_ids: list[int]) -> dict[int, tuple[float, float, str]]:
    """Batch-Variante: {product_id: (min, max, currency)} für alle IDs."""
    if not product_ids or not PRICE_HISTORY_DB.exists():
        return {}
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        result: dict[int, tuple[float, float, str]] = {}
        for pid in product_ids:
            cur.execute(
                "SELECT min_price, max_price, currency_iso "
                "FROM product_price_history WHERE product_id=? "
                "ORDER BY recorded_at DESC LIMIT 1",
                (pid,),
            )
            row = cur.fetchone()
            if row:
                result[pid] = (row[0], row[1], row[2])
        return result
    finally:
        conn.close()


# ── Produktsuche ──────────────────────────────────────────────────────────────

async def _find_products_for_tracking(bot, species_query: str) -> dict:
    """
    Sucht Produkte für eine Art/Gattung über alle Shops.

    Alle passenden Produkte werden zurückgegeben, unabhängig vom Preis:
    - Aktueller Preis > 0  → Produkt mit aktuellem Preis (✅ / ❌ je in_stock)
    - Preis = 0 + History  → Letzter bekannter Preis injiziert (⏸️ "zul. X")
    - Preis = 0, kein Hist.→ Trotzdem wählbar, mit ❓ "kein Preis bekannt"

    Returns:
        {shop_id: {"shop_info": {...}, "products": [...]}}
        Nur Shops mit mindestens einem Produkt.
    """
    shop_data = await load_shop_data(bot)
    normalized = normalize_species_name(species_query)
    is_genus = " " not in normalized.strip()

    # Alle passenden Produkte sammeln
    candidates: dict[str, list] = {}   # shop_id → [product, ...]
    zero_price_ids: list[int] = []

    for shop_id, shop_info in shop_data.items():
        for product in shop_info.get("products", []):
            species = (product.get("species") or "").strip()
            norm = normalize_species_name(species)

            if is_genus:
                match = norm.startswith(normalized + " ")
            else:
                match = norm == normalized

            if not match:
                continue

            try:
                min_p = float(product.get("min_price") or 0)
                max_p = float(product.get("max_price") or 0)
            except (ValueError, TypeError):
                min_p = max_p = 0.0

            if min_p == 0.0 and max_p == 0.0:
                pid = product.get("id")
                if pid is not None:
                    zero_price_ids.append(pid)
                candidates.setdefault(shop_id, []).append(
                    {**product, "_price_zero": True}
                )
            else:
                candidates.setdefault(shop_id, []).append(product)

    # Batch-Lookup historischer Preise für Produkte ohne aktuellen Preis
    hist_prices: dict[int, tuple[float, float, str]] = {}
    if zero_price_ids:
        hist_prices = await asyncio.to_thread(
            _get_latest_prices_sync, zero_price_ids
        )

    # Ergebnis zusammenbauen
    result: dict = {}
    for shop_id, products in candidates.items():
        shop_info = shop_data[shop_id]
        matches = []
        for p in products:
            if p.get("_price_zero"):
                pid = p.get("id")
                hist = hist_prices.get(pid)
                if hist is not None:
                    min_h, max_h, cur_h = hist
                    p = {
                        **p,
                        "min_price":     str(min_h),
                        "max_price":     str(max_h),
                        "currency_iso":  cur_h,
                        "_from_history": True,
                    }
                else:
                    p = {**p, "_no_price": True}
                p = {k: v for k, v in p.items() if k != "_price_zero"}
            matches.append(p)

        if matches:
            result[shop_id] = {"shop_info": shop_info, "products": matches}

    return result


# ── Discord-UI Views ──────────────────────────────────────────────────────────

class _BaseView(discord.ui.View):
    """Basisklasse mit Owner-Check und Timeout-Handling."""

    def __init__(self, owner_id: int, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Das ist nicht dein Menü.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Shop-Auswahl ──────────────────────────────────────────────────────────────

class _ShopSelectItem(discord.ui.Select):
    def __init__(self, shops_with_products: dict):
        options = []
        for shop_id, data in list(shops_with_products.items())[:25]:
            name  = data["shop_info"].get("name", shop_id)
            count = len(data["products"])
            options.append(discord.SelectOption(
                label=name[:100],
                value=shop_id,
                description=f"{count} Produkt(e) gefunden"[:100],
            ))
        super().__init__(
            placeholder="Shop auswählen …",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_shop_selected(self.values[0], interaction)


class ShopSelectView(_BaseView):
    def __init__(
        self,
        shops_with_products: dict,
        species: str,
        bot,
        owner_id: int,
        lang: str,
    ):
        super().__init__(owner_id)
        self.shops_with_products = shops_with_products
        self.species = species
        self.bot = bot
        self.lang = lang
        self.add_item(_ShopSelectItem(shops_with_products))

    async def on_shop_selected(self, shop_id: str, interaction: discord.Interaction):
        data      = self.shops_with_products[shop_id]
        products  = data["products"]
        shop_info = data["shop_info"]

        view = ProductSelectView(
            products, shop_id, shop_info, self.species,
            self.bot, self.owner_id, self.lang,
        )
        await interaction.response.edit_message(
            content=l10n.get("pt_select_products", self.lang),
            view=view,
        )


# ── Produkt-Auswahl (Multi-Select) ────────────────────────────────────────────

class _ProductSelectItem(discord.ui.Select):
    def __init__(self, products: list, lang: str):
        options = []
        for p in products[:25]:
            title    = (p.get("species") or p.get("title") or "?")[:97]
            currency = p.get("currency_iso", "EUR")

            if p.get("_no_price"):
                stock_icon = "❓"
                price_str  = "kein Preis bekannt"
            elif p.get("_from_history"):
                min_p      = p.get("min_price", "0")
                max_p      = p.get("max_price", "0")
                price_str  = "zul. " + format_price(min_p, max_p, currency)
                stock_icon = "⏸️"
            elif p.get("in_stock"):
                min_p      = p.get("min_price", "0")
                max_p      = p.get("max_price", "0")
                price_str  = format_price(min_p, max_p, currency)
                stock_icon = "✅"
            else:
                min_p      = p.get("min_price", "0")
                max_p      = p.get("max_price", "0")
                price_str  = format_price(min_p, max_p, currency)
                stock_icon = "❌"

            options.append(discord.SelectOption(
                label=title,
                value=str(p.get("id", "")),
                description=f"{stock_icon} {price_str}"[:100],
            ))
        super().__init__(
            placeholder="Produkt(e) auswählen …",
            min_values=1,
            max_values=min(len(options), 25),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_products_selected(self.values, interaction)


class ProductSelectView(_BaseView):
    def __init__(
        self,
        products: list,
        shop_id: str,
        shop_info: dict,
        species: str,
        bot,
        owner_id: int,
        lang: str,
    ):
        super().__init__(owner_id)
        self.products_by_id = {str(p.get("id", "")): p for p in products}
        self.shop_id   = shop_id
        self.shop_info = shop_info
        self.species   = species
        self.bot       = bot
        self.lang      = lang
        self.add_item(_ProductSelectItem(products, lang))

    async def on_products_selected(self, product_ids: list[str], interaction: discord.Interaction):
        selected = [self.products_by_id[pid] for pid in product_ids if pid in self.products_by_id]

        lines = []
        for p in selected:
            if p.get("_no_price"):
                price_str = "kein Preis bekannt"
                stock = "❓"
            elif p.get("_from_history"):
                price_str = "zul. " + format_price(
                    p.get("min_price", 0), p.get("max_price", 0), p.get("currency_iso", "EUR")
                )
                stock = "⏸️"
            else:
                price_str = format_price(
                    p.get("min_price", 0), p.get("max_price", 0), p.get("currency_iso", "EUR")
                )
                stock = "✅" if p.get("in_stock") else "❌"
            title = p.get("species") or p.get("title") or "?"
            url   = p.get("antcheck_url") or ""
            lines.append(f"• {stock} [{title}](<{url}>) – {price_str}")

        content = (
            l10n.get("pt_confirm_header", self.lang)
            + "\n"
            + "\n".join(lines)
        )
        view = ConfirmView(
            selected, self.shop_id, self.shop_info, self.species,
            self.bot, self.owner_id, self.lang,
        )
        await interaction.response.edit_message(content=content, view=view)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Bestätigung ───────────────────────────────────────────────────────────────

class ConfirmView(_BaseView):
    def __init__(
        self,
        selected_products: list,
        shop_id: str,
        shop_info: dict,
        species: str,
        bot,
        owner_id: int,
        lang: str,
    ):
        super().__init__(owner_id)
        self.selected   = selected_products
        self.shop_id    = shop_id
        self.shop_info  = shop_info
        self.species    = species
        self.bot        = bot
        self.lang       = lang

    @discord.ui.button(label="✅ Bestätigen", style=discord.ButtonStyle.success)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        user_id    = str(self.owner_id)
        shop_name  = self.shop_info.get("name", self.shop_id)
        saved      = 0

        for p in self.selected:
            pid = p.get("id")
            if pid is None:
                continue
            try:
                min_p = float(p.get("min_price") or 0)
                max_p = float(p.get("max_price") or 0)
            except (ValueError, TypeError):
                min_p = max_p = 0.0

            # Aktuellsten Preis als Baseline setzen (oder API-Preis wenn kein History-Eintrag)
            current = await asyncio.to_thread(_get_latest_price_sync, int(pid))
            if current:
                baseline_min, baseline_max, currency = current
            else:
                baseline_min = min_p
                baseline_max = max_p
                currency = p.get("currency_iso") or "EUR"

            await execute_db(
                self.bot,
                """INSERT INTO user_price_tracking
                   (user_id, product_id, species, product_title, product_url,
                    shop_name, shop_id, currency_iso, last_notified_min, last_notified_max)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, product_id) DO NOTHING""",
                (
                    user_id, pid,
                    p.get("species") or "",
                    p.get("species") or p.get("title") or "",
                    p.get("antcheck_url") or "",
                    shop_name, self.shop_id,
                    currency,
                    baseline_min if (baseline_min or baseline_max) else None,
                    baseline_max if (baseline_min or baseline_max) else None,
                ),
                commit=True,
            )
            saved += 1

        self.disable_all_items()
        await interaction.response.edit_message(
            content=l10n.get("pt_saved", self.lang, count=saved),
            view=self,
        )
        # Öffentliche Meldung im Kanal (nur wenn Tracking erfolgreich gespeichert)
        if saved > 0 and interaction.channel:
            await interaction.channel.send(
                l10n.get(
                    "pt_tracking_announced",
                    self.lang,
                    user=interaction.user.display_name,
                    species=self.species,
                    shop=self.shop_info.get("name", self.shop_id),
                    count=saved,
                )
            )
        self.stop()

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.danger)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content="❌ Abgebrochen.", view=self)
        self.stop()

    def disable_all_items(self):
        for item in self.children:
            item.disabled = True


# ── Untrack-Auswahl ───────────────────────────────────────────────────────────

class _UntrackSelectItem(discord.ui.Select):
    def __init__(self, tracked_rows: list, current_prices: dict, lang: str):
        options = []
        for row in tracked_rows[:25]:
            pid      = row["product_id"]
            title    = (row["product_title"] or row["species"] or f"Produkt {pid}")[:97]
            shop     = row["shop_name"] or "?"
            current  = current_prices.get(pid)
            if current:
                price_str = format_price(current[0], current[1], current[2])
            else:
                price_str = "kein Preis"
            options.append(discord.SelectOption(
                label=title,
                value=str(pid),
                description=f"{shop} – {price_str}"[:100],
            ))
        super().__init__(
            placeholder="Produkt(e) auswählen …",
            min_values=1,
            max_values=min(len(options), 25),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_products_selected(self.values, interaction)


class UntrackView(_BaseView):
    def __init__(self, tracked_rows: list, current_prices: dict, bot, owner_id: int, lang: str):
        super().__init__(owner_id)
        self.bot       = bot
        self.lang      = lang
        self.add_item(_UntrackSelectItem(tracked_rows, current_prices, lang))

    async def on_products_selected(self, product_ids: list[str], interaction: discord.Interaction):
        user_id = str(self.owner_id)
        removed = 0
        for pid_str in product_ids:
            await execute_db(
                self.bot,
                "DELETE FROM user_price_tracking WHERE user_id=? AND product_id=?",
                (user_id, int(pid_str)),
                commit=True,
            )
            removed += 1

        self.disable_all_items()
        await interaction.response.edit_message(
            content=l10n.get("pt_untrack_done", self.lang, count=removed),
            view=self,
        )
        self.stop()

    def disable_all_items(self):
        for item in self.children:
            item.disabled = True


# ── Cog ───────────────────────────────────────────────────────────────────────

class PriceTrackingCog(commands.Cog, name="PriceTracking"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.check_price_changes.start()

    def cog_unload(self):
        self.check_price_changes.cancel()

    # ── /track_price ──────────────────────────────────────────────────────────

    @discord.slash_command(
        name="track_price",
        description="Track prices for an ant species and get notified on changes.",
        description_localizations={"de": "Preise für eine Art beobachten und bei Änderung per PN informiert werden."},
    )
    async def track_price(
        self,
        ctx: discord.ApplicationContext,
        species: discord.Option(str, "Artname oder Gattung (z.B. 'Oecophylla smaragdina' oder 'Camponotus')"),  # type: ignore[valid-type]
    ):
        await ctx.defer(ephemeral=True)

        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        await ensure_rates()

        shops_data = await _find_products_for_tracking(self.bot, species)

        if not shops_data:
            await ctx.followup.send(
                l10n.get("pt_no_products", lang, species=species),
                ephemeral=True,
            )
            return

        view = ShopSelectView(shops_data, species, self.bot, ctx.author.id, lang)
        await ctx.followup.send(
            l10n.get("pt_select_shop", lang),
            view=view,
            ephemeral=True,
        )

    # ── /my_price_tracking ────────────────────────────────────────────────────

    @discord.slash_command(
        name="my_price_tracking",
        description="Show all tracked products with current prices.",
        description_localizations={"de": "Alle beobachteten Produkte mit aktuellen Preisen anzeigen."},
    )
    async def my_price_tracking(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        lang    = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        user_id = str(ctx.author.id)

        rows = await execute_db(
            self.bot,
            "SELECT product_id, species, product_title, product_url, "
            "shop_name, currency_iso, last_notified_min, last_notified_max "
            "FROM user_price_tracking WHERE user_id=? ORDER BY shop_name, species",
            (user_id,),
            fetch=True,
        )

        if not rows:
            await ctx.followup.send(
                l10n.get("pt_list_empty", lang), ephemeral=True
            )
            return

        await ensure_rates()
        pids    = [r["product_id"] for r in rows]
        current = await asyncio.to_thread(_get_latest_prices_sync, pids)

        lines = []
        for row in rows:
            pid   = row["product_id"]
            title = row["product_title"] or row["species"] or f"ID {pid}"
            url   = row["product_url"] or ""
            shop  = row["shop_name"] or "?"

            curr = current.get(pid)
            if curr:
                price_str = format_price(curr[0], curr[1], curr[2])
            else:
                price_str = format_price(
                    row["last_notified_min"] or 0,
                    row["last_notified_max"] or 0,
                    row["currency_iso"] or "EUR",
                )

            lines.append(l10n.get(
                "pt_list_entry", lang,
                title=title, url=url, shop=shop, price=price_str,
                status="",
            ))

        header = l10n.get("pt_list_header", lang)
        msg    = header + "\n" + "\n".join(lines)

        if len(msg) > 2000:
            msg = msg[:1990] + "…"

        await ctx.followup.send(msg, ephemeral=True)

    # ── /untrack_price ────────────────────────────────────────────────────────

    @discord.slash_command(
        name="untrack_price",
        description="Remove products from price tracking.",
        description_localizations={"de": "Produkte aus dem Preis-Tracking entfernen."},
    )
    async def untrack_price(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        lang    = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        user_id = str(ctx.author.id)

        rows = await execute_db(
            self.bot,
            "SELECT product_id, species, product_title, product_url, "
            "shop_name, currency_iso, last_notified_min, last_notified_max "
            "FROM user_price_tracking WHERE user_id=? ORDER BY shop_name, species",
            (user_id,),
            fetch=True,
        )

        if not rows:
            await ctx.followup.send(
                l10n.get("pt_untrack_none", lang), ephemeral=True
            )
            return

        await ensure_rates()
        pids    = [r["product_id"] for r in rows]
        current = await asyncio.to_thread(_get_latest_prices_sync, pids)

        view = UntrackView(list(rows), current, self.bot, ctx.author.id, lang)
        await ctx.followup.send(
            l10n.get("pt_untrack_select", lang),
            view=view,
            ephemeral=True,
        )

    # ── Hintergrundtask: Preisänderungen prüfen ───────────────────────────────

    @tasks.loop(minutes=65)
    async def check_price_changes(self):
        """
        Prüft stündlich ob sich Preise für beobachtete Produkte geändert haben
        und benachrichtigt betroffene User per DM.
        """
        try:
            rows = await execute_db(
                self.bot,
                """SELECT user_id, product_id, species, product_title, product_url,
                          shop_name, currency_iso, last_notified_min, last_notified_max
                   FROM user_price_tracking""",
                fetch=True,
            )
            if not rows:
                return

            pids    = list({r["product_id"] for r in rows})
            current = await asyncio.to_thread(_get_latest_prices_sync, pids)

            await ensure_rates()

            for row in rows:
                pid      = row["product_id"]
                curr_row = current.get(pid)
                if not curr_row:
                    continue  # Noch kein Preis in History

                curr_min, curr_max, curr_currency = curr_row
                last_min = row["last_notified_min"]
                last_max = row["last_notified_max"]

                if last_min is None or last_max is None:
                    # Erster Lauf: Baseline setzen ohne Nachricht
                    await execute_db(
                        self.bot,
                        "UPDATE user_price_tracking SET last_notified_min=?, last_notified_max=?, currency_iso=? "
                        "WHERE user_id=? AND product_id=?",
                        (curr_min, curr_max, curr_currency, row["user_id"], pid),
                        commit=True,
                    )
                    continue

                if curr_min == last_min and curr_max == last_max:
                    continue  # Keine Änderung

                # Preisänderung erkannt → User benachrichtigen
                await self._notify_user(row, curr_min, curr_max, curr_currency, last_min, last_max)

                # Baseline aktualisieren
                await execute_db(
                    self.bot,
                    "UPDATE user_price_tracking SET last_notified_min=?, last_notified_max=?, currency_iso=? "
                    "WHERE user_id=? AND product_id=?",
                    (curr_min, curr_max, curr_currency, row["user_id"], pid),
                    commit=True,
                )

        except Exception as e:
            logger.error("❌ check_price_changes error: %s", e, exc_info=True)

    @check_price_changes.before_loop
    async def before_check_price_changes(self):
        await self.bot.wait_until_ready()

    async def _notify_user(
        self,
        row,
        curr_min: float,
        curr_max: float,
        currency: str,
        last_min: float,
        last_max: float,
    ):
        """Sendet eine DM an den User über die Preisänderung."""
        user_id_str = row["user_id"]
        try:
            user_id = int(user_id_str)
        except (ValueError, TypeError):
            return

        try:
            user = await self.bot.fetch_user(user_id)
        except Exception as e:
            logger.warning("⚠️ User %s nicht abrufbar: %s", user_id_str, e)
            return

        lang = await get_user_lang(self.bot, user_id_str, None)

        old_price_str = format_price(last_min, last_max, currency)
        new_price_str = format_price(curr_min, curr_max, currency)

        # Günstiger wenn Mittelwert gesunken
        old_avg = (last_min + last_max) / 2
        new_avg = (curr_min + curr_max) / 2
        key     = "pt_dm_cheaper" if new_avg < old_avg else "pt_dm_dearer"

        msg = l10n.get(
            key, lang,
            shop=row["shop_name"] or "?",
            species=row["species"] or "?",
            title=row["product_title"] or row["species"] or "?",
            old_price=old_price_str,
            new_price=new_price_str,
            url=row["product_url"] or "",
        )

        try:
            await user.send(msg)
            logger.info(
                "📩 Preis-Benachrichtigung gesendet: user=%s product=%s %s→%s",
                user_id_str, row["product_id"], old_price_str, new_price_str,
            )
        except discord.Forbidden:
            # DM blockiert – versuche Server-Kanal als Fallback
            await self._fallback_server_message(user_id_str, msg)
        except Exception as e:
            logger.error("❌ Fehler beim Senden der Preis-DM an %s: %s", user_id_str, e)

    async def _fallback_server_message(self, user_id: str, msg: str):
        """Postet Preis-Benachrichtigung im Server-Kanal wenn DM blockiert."""
        try:
            servers = await execute_db(
                self.bot,
                "SELECT DISTINCT server_id FROM server_user_mappings WHERE user_id=?",
                (user_id,),
                fetch=True,
            )
            for srv_row in servers:
                server_id = srv_row["server_id"]
                ch_row    = await execute_db(
                    self.bot,
                    "SELECT channel_id FROM server_settings WHERE server_id=?",
                    (server_id,),
                    fetch=True,
                )
                if not ch_row:
                    continue
                channel = self.bot.get_channel(ch_row[0]["channel_id"])
                if channel:
                    await channel.send(msg)
        except Exception as e:
            logger.error("❌ _fallback_server_message error: %s", e)


def setup(bot):
    bot.add_cog(PriceTrackingCog(bot))
