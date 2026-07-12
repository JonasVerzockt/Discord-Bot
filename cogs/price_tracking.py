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
                        Neu: "Alle Shops beobachten" Option für Art-/Gattungsweites Tracking
  /my_price_tracking  – Alle beobachteten Produkte + Arten-Beobachtungen (mit Preisen)
  /untrack_price      – Produkte und Arten-Beobachtungen entfernen

Background Tasks (gestaffelt, damit nicht beide gleichzeitig laufen):
  check_price_changes (alle 65 Min)  – Preisänderungen bei beobachteten Einzelprodukten
  check_species_watches (alle 67 Min) – Neue Produkte + Preisänderungen bei Arten-Beobachtungen
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
from utils.availability import load_shop_data, normalize_species_name, format_rating, available_variants
from utils.currency import ensure_rates, format_price
from utils.achievements import log_event, check_and_grant

logger = logging.getLogger(__name__)

PRICE_HISTORY_DB = Path(DATA_DIRECTORY) / "price_history.db"

# Sentinel-Wert für "Alle Shops beobachten"-Option
_ALL_SHOPS_SENTINEL = "__all__"
# Präfix für Arten-Beobachtungen im Untrack-Select
_SW_PREFIX = "__sw__"

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


def _get_latest_variant_price_sync(variant_id: int):
    """Gibt (price, currency_iso) des letzten Varianten-Eintrags zurück, oder None."""
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
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def _get_latest_variant_prices_sync(variant_ids: list[int]) -> dict:
    """Batch: {variant_id: (price, currency)} fuer alle IDs."""
    if not variant_ids or not PRICE_HISTORY_DB.exists():
        return {}
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        result: dict = {}
        for vid in variant_ids:
            cur.execute(
                "SELECT price, currency_iso FROM variant_price_history "
                "WHERE variant_id=? ORDER BY recorded_at DESC LIMIT 1",
                (vid,),
            )
            row = cur.fetchone()
            if row:
                result[vid] = (row[0], row[1])
        return result
    finally:
        conn.close()


def _get_price_reason_sync(product_id: int):
    """Letzter erkannter Grund einer Spannen-Aenderung (product_price_reason), oder None."""
    if not PRICE_HISTORY_DB.exists():
        return None
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT direction, code, variant_title, old_price, new_price, currency_iso "
            "FROM product_price_reason WHERE product_id=?",
            (product_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"direction": row[0], "code": row[1], "variant": row[2] or "?",
                "old": row[3], "new": row[4], "currency": row[5] or "EUR"}
    except Exception:
        return None  # Tabelle existiert evtl. noch nicht (alter Grabber-Stand)
    finally:
        conn.close()


_REASON_KEYS = {
    "price_up":       "pt_reason_price_up",
    "price_down":     "pt_reason_price_down",
    "cheapest_gone":  "pt_reason_cheapest_gone",
    "new_expensive":  "pt_reason_new_expensive",
    "new_cheaper":    "pt_reason_new_cheaper",
    "expensive_gone": "pt_reason_expensive_gone",
}


def _display_title(row) -> str:
    """Anzeigetitel inkl. Variantenname (falls variant_id>0)."""
    base = row["product_title"] or row["species"] or "?"
    try:
        vt = row["variant_title"]
    except (IndexError, KeyError):
        vt = ""
    return f"{base} – {vt}" if vt else base


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

    candidates: dict[str, list] = {}
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

    hist_prices: dict[int, tuple[float, float, str]] = {}
    if zero_price_ids:
        hist_prices = await asyncio.to_thread(
            _get_latest_prices_sync, zero_price_ids
        )

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


def _make_product_label(p: dict) -> str:
    """
    Eindeutiges Label für ein Produkt im Select-Menü.
    Reihenfolge: title (wenn != species) > species + description > species + #ID.
    Die AntCheck API liefert aktuell kein separates Varianten-Feld;
    description/comment werden genutzt falls ein Shop sie befüllt.
    """
    raw_title   = (p.get("title") or p.get("species") or "?").strip()
    species     = (p.get("species") or "").strip()
    description = (p.get("description") or "").strip()
    pid_str     = str(p.get("id", ""))

    if raw_title.lower() != species.lower():
        # title enthält Varianteninfo → direkt nutzen
        label = raw_title
    elif description:
        # description enthält Varianteninfo (z.B. "1Q + 10W")
        label = f"{raw_title} – {description}"
    elif pid_str:
        # Fallback: Produkt-ID als Disambiguator
        label = f"{raw_title} (#{pid_str})"
    else:
        label = raw_title

    return label[:97]


# ── Discord-UI Views ──────────────────────────────────────────────────────────

class _BaseView(discord.ui.View):
    """Basisklasse mit Owner-Check und Timeout-Handling."""

    def __init__(self, owner_id: int, lang: str = "en", timeout: int = 180):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.lang = lang

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                l10n.get("pt_not_your_menu", self.lang), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Shop-Auswahl ──────────────────────────────────────────────────────────────

class _ShopSelectItem(discord.ui.Select):
    def __init__(self, shops_with_products: dict, species: str, lang: str):
        options = [
            discord.SelectOption(
                label=l10n.get("pt_watch_all_shops_label", lang),
                value=_ALL_SHOPS_SENTINEL,
                description=l10n.get("pt_watch_all_shops_desc", lang),
                emoji="🔭",
            )
        ]
        for shop_id, data in list(shops_with_products.items())[:24]:
            name  = data["shop_info"].get("name", shop_id)
            count = len(data["products"])
            options.append(discord.SelectOption(
                label=name[:100],
                value=shop_id,
                description=l10n.get("pt_products_found", lang, count=count)[:100],
            ))
        super().__init__(
            placeholder=l10n.get("pt_shop_placeholder", lang),
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
        super().__init__(owner_id, lang)
        self.shops_with_products = shops_with_products
        self.species = species
        self.bot = bot
        self.lang = lang
        self.add_item(_ShopSelectItem(shops_with_products, species, lang))

    async def on_shop_selected(self, shop_id: str, interaction: discord.Interaction):
        # "Alle Shops beobachten" gewählt
        if shop_id == _ALL_SHOPS_SENTINEL:
            normalized = normalize_species_name(self.species)
            view = SpeciesWatchConfirmView(
                self.species, normalized, self.bot, self.owner_id, self.lang
            )
            await interaction.response.edit_message(
                content=l10n.get(
                    "pt_species_watch_confirm", self.lang,
                    species=self.species,
                ),
                view=view,
            )
            return

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


# ── Arten-Beobachtung (alle Shops) ────────────────────────────────────────────

class SpeciesWatchConfirmView(_BaseView):
    """Bestätigung für Arten-weite Beobachtung (alle Shops)."""

    def __init__(self, species_raw: str, species_normalized: str, bot, owner_id: int, lang: str):
        super().__init__(owner_id, lang)
        self.species_raw        = species_raw
        self.species_normalized = species_normalized
        self.bot  = bot
        self.lang = lang
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "sw_watch":
                    child.label = l10n.get("pt_button_watch", lang)
                elif child.custom_id == "sw_cancel":
                    child.label = l10n.get("pt_button_cancel", lang)

    @discord.ui.button(label="🔭 Beobachten", style=discord.ButtonStyle.success, custom_id="sw_watch")
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        normalized = self.species_normalized
        is_genus   = 1 if " " not in normalized.strip() else 0

        rc = await execute_db(
            self.bot,
            """INSERT INTO user_species_watch (user_id, species, is_genus)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, species) DO NOTHING""",
            (str(self.owner_id), normalized, is_genus),
            commit=True,
        )

        # Alle aktuell bekannten Produkte als "gesehen" markieren (keine Spam-DMs beim Start)
        await self._init_seen_products(normalized, is_genus)

        self.disable_all_items()
        try:
            await check_and_grant(self.bot, interaction.user, self.lang)
        except Exception:
            pass
        await interaction.response.edit_message(
            content=l10n.get(
                "pt_species_watch_saved" if rc else "pt_species_watch_already",
                self.lang, species=self.species_raw),
            view=self,
        )
        if rc and interaction.channel:
            await interaction.channel.send(
                l10n.get(
                    "pt_species_watch_announced", self.lang,
                    user=interaction.user.display_name,
                    species=self.species_raw,
                )
            )
        self.stop()

    async def _init_seen_products(self, normalized: str, is_genus: int):
        """Lädt alle aktuellen Produkte und speichert sie als Baseline."""
        try:
            shop_data = await load_shop_data(self.bot)
            for shop_id, shop_info in shop_data.items():
                for product in shop_info.get("products", []):
                    species = (product.get("species") or "").strip()
                    norm    = normalize_species_name(species)
                    if is_genus:
                        match = norm.startswith(normalized + " ")
                    else:
                        match = norm == normalized
                    if not match:
                        continue
                    pid = product.get("id")
                    if pid is None:
                        continue
                    try:
                        min_p = float(product.get("min_price") or 0)
                        max_p = float(product.get("max_price") or 0)
                    except (ValueError, TypeError):
                        min_p = max_p = 0.0
                    await execute_db(
                        self.bot,
                        """INSERT OR IGNORE INTO user_species_watch_seen
                           (user_id, watched_species, product_id, last_min, last_max, currency)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            str(self.owner_id), normalized, int(pid),
                            min_p or None, max_p or None,
                            product.get("currency_iso") or "EUR",
                        ),
                        commit=True,
                    )
        except Exception as e:
            logger.error("❌ _init_seen_products error: %s", e)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.danger, custom_id="sw_cancel")
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content=l10n.get("pt_cancelled", self.lang), view=self)
        self.stop()

    def disable_all_items(self):
        for item in self.children:
            item.disabled = True


# ── Produkt-Auswahl (Multi-Select) ────────────────────────────────────────────

class _ProductSelectItem(discord.ui.Select):
    def __init__(self, products: list, lang: str):
        options = []
        for p in products[:25]:
            label    = _make_product_label(p)
            currency = p.get("currency_iso", "EUR")

            if p.get("_no_price"):
                stock_icon = "❓"
                price_str  = l10n.get("pt_no_price", lang)
            elif p.get("_from_history"):
                min_p      = p.get("min_price", "0")
                max_p      = p.get("max_price", "0")
                price_str  = l10n.get("pt_last_price_prefix", lang) + " " + format_price(min_p, max_p, currency)
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
                label=label,
                value=str(p.get("id", "")),
                description=price_str[:100],
                emoji=stock_icon,
            ))
        super().__init__(
            placeholder=l10n.get("pt_product_placeholder", lang),
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
        super().__init__(owner_id, lang)
        self.products_by_id = {str(p.get("id", "")): p for p in products}
        self.shop_id   = shop_id
        self.shop_info = shop_info
        self.species   = species
        self.bot       = bot
        self.lang      = lang
        self.add_item(_ProductSelectItem(products, lang))

    async def on_products_selected(self, product_ids: list[str], interaction: discord.Interaction):
        selected = [self.products_by_id[pid] for pid in product_ids if pid in self.products_by_id]

        # Genau EIN Produkt mit Varianten -> optionaler Varianten-Auswahlschritt
        if len(selected) == 1 and available_variants(selected[0]):
            vview = VariantSelectView(
                selected[0], self.shop_id, self.shop_info, self.species,
                self.bot, self.owner_id, self.lang,
            )
            await interaction.response.edit_message(
                content=l10n.get("pt_select_variant", self.lang), view=vview,
            )
            return

        lines = []
        for p in selected:
            if p.get("_no_price"):
                price_str = l10n.get("pt_no_price", self.lang)
                stock = "❓"
            elif p.get("_from_history"):
                price_str = l10n.get("pt_last_price_prefix", self.lang) + " " + format_price(
                    p.get("min_price", 0), p.get("max_price", 0), p.get("currency_iso", "EUR")
                )
                stock = "⏸️"
            else:
                price_str = format_price(
                    p.get("min_price", 0), p.get("max_price", 0), p.get("currency_iso", "EUR")
                )
                stock = "✅" if p.get("in_stock") else "❌"
            title = _make_product_label(p)
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


# ── Varianten-Auswahl (optional, bei genau einem Produkt mit Varianten) ───────

class _VariantSelectItem(discord.ui.Select):
    def __init__(self, product: dict, lang: str):
        self.lang = lang
        opts = [discord.SelectOption(
            label=l10n.get("pt_whole_product", lang)[:97],
            value="0", emoji="🔭",
        )]
        for v in available_variants(product)[:24]:
            vid   = v.get("id")
            if vid is None:
                continue
            label = (v.get("title") or v.get("description") or f"Variante {vid}")[:97]
            cur   = v.get("currency_iso") or product.get("currency_iso") or "EUR"
            price = format_price(v.get("price"), v.get("price"), cur)
            opts.append(discord.SelectOption(
                label=label, value=str(vid), description=price[:100], emoji="🔖",
            ))
        super().__init__(
            placeholder=l10n.get("pt_variant_placeholder", lang),
            min_values=1, max_values=len(opts), options=opts,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_variant_selected(self.values, interaction)


class VariantSelectView(_BaseView):
    def __init__(self, product, shop_id, shop_info, species, bot, owner_id, lang):
        super().__init__(owner_id, lang)
        self.product   = product
        self.shop_id   = shop_id
        self.shop_info = shop_info
        self.species   = species
        self.bot       = bot
        self.lang      = lang
        self.add_item(_VariantSelectItem(product, lang))

    async def on_variant_selected(self, values, interaction):
        vmap = {str(v.get("id")): v for v in available_variants(self.product)}
        entries = []
        for val in values:
            e = dict(self.product)
            if val == "0":
                e["_variant_id"] = 0
                e["_variant_title"] = ""
            else:
                v = vmap.get(val)
                if not v:
                    continue
                e["_variant_id"]       = int(val)
                e["_variant_title"]    = v.get("title") or v.get("description") or f"Variante {val}"
                e["_variant_price"]    = v.get("price")
                e["_variant_currency"] = v.get("currency_iso") or self.product.get("currency_iso") or "EUR"
            entries.append(e)

        lines = []
        for e in entries:
            vt = e.get("_variant_title")
            title = _make_product_label(e) + (f" – {vt}" if vt else "")
            if e.get("_variant_id"):
                price_str = format_price(e.get("_variant_price"), e.get("_variant_price"), e.get("_variant_currency"))
            else:
                price_str = format_price(e.get("min_price", 0), e.get("max_price", 0), e.get("currency_iso", "EUR"))
            lines.append(f"• {title} – {price_str}")

        content = l10n.get("pt_confirm_header", self.lang) + "\n" + "\n".join(lines)
        view = ConfirmView(entries, self.shop_id, self.shop_info, self.species,
                           self.bot, self.owner_id, self.lang)
        await interaction.response.edit_message(content=content, view=view)


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
        super().__init__(owner_id, lang)
        self.selected   = selected_products
        self.shop_id    = shop_id
        self.shop_info  = shop_info
        self.species    = species
        self.bot        = bot
        self.lang       = lang
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "cv_confirm":
                    child.label = l10n.get("pt_button_confirm", lang)
                elif child.custom_id == "cv_cancel":
                    child.label = l10n.get("pt_button_cancel", lang)

    @discord.ui.button(label="✅ Bestätigen", style=discord.ButtonStyle.success, custom_id="cv_confirm")
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        user_id    = str(self.owner_id)
        shop_name  = self.shop_info.get("name", self.shop_id)
        saved      = 0
        already    = 0

        for p in self.selected:
            pid = p.get("id")
            if pid is None:
                continue
            vid    = int(p.get("_variant_id", 0) or 0)
            vtitle = p.get("_variant_title", "") or ""
            try:
                min_p = float(p.get("min_price") or 0)
                max_p = float(p.get("max_price") or 0)
            except (ValueError, TypeError):
                min_p = max_p = 0.0

            if vid > 0:
                hist = await asyncio.to_thread(_get_latest_variant_price_sync, vid)
                if hist:
                    baseline_min = baseline_max = hist[0]
                    currency = hist[1]
                else:
                    try:
                        vp = float(p.get("_variant_price") or 0)
                    except (ValueError, TypeError):
                        vp = 0.0
                    baseline_min = baseline_max = vp
                    currency = p.get("_variant_currency") or p.get("currency_iso") or "EUR"
            else:
                current = await asyncio.to_thread(_get_latest_price_sync, int(pid))
                if current:
                    baseline_min, baseline_max, currency = current
                else:
                    baseline_min = min_p
                    baseline_max = max_p
                    currency = p.get("currency_iso") or "EUR"

            rc = await execute_db(
                self.bot,
                """INSERT INTO user_price_tracking
                   (user_id, product_id, variant_id, variant_title, species,
                    product_title, product_url, shop_name, shop_id, currency_iso,
                    last_notified_min, last_notified_max)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, product_id, variant_id) DO NOTHING""",
                (
                    user_id, pid, vid, vtitle,
                    p.get("species") or "",
                    p.get("title") or p.get("species") or "",
                    p.get("antcheck_url") or "",
                    shop_name, self.shop_id,
                    currency,
                    baseline_min if (baseline_min or baseline_max) else None,
                    baseline_max if (baseline_min or baseline_max) else None,
                ),
                commit=True,
            )
            if rc:
                saved += 1
            else:
                already += 1

        self.disable_all_items()
        if saved and already:
            content = (l10n.get("pt_saved", self.lang, count=saved) + "\n"
                       + l10n.get("pt_already_tracked", self.lang, count=already))
        elif already:
            content = l10n.get("pt_already_tracked", self.lang, count=already)
        else:
            content = l10n.get("pt_saved", self.lang, count=saved)
        await interaction.response.edit_message(content=content, view=self)
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
        try:
            await check_and_grant(self.bot, interaction.user, self.lang)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.danger, custom_id="cv_cancel")
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content=l10n.get("pt_cancelled", self.lang), view=self)
        self.stop()

    def disable_all_items(self):
        for item in self.children:
            item.disabled = True


# ── Untrack-Auswahl ───────────────────────────────────────────────────────────

class _UntrackSelectItem(discord.ui.Select):
    def __init__(
        self,
        tracked_rows: list,
        current_prices: dict,
        variant_prices: dict,
        species_watches: list,
        lang: str,
    ):
        options = []
        # Einzelne Produkte / Varianten
        for row in tracked_rows[:20]:
            pid      = row["product_id"]
            vid      = row["variant_id"] or 0
            title    = _display_title(row)[:80]
            shop     = (row["shop_name"] or "?")[:15]
            if vid > 0:
                vc = variant_prices.get(vid)
                price_str = format_price(vc[0], vc[0], vc[1]) if vc else l10n.get("pt_no_price_short", lang)
            else:
                current  = current_prices.get(pid)
                price_str = format_price(current[0], current[1], current[2]) if current else l10n.get("pt_no_price_short", lang)
            options.append(discord.SelectOption(
                label=f"{title}"[:97],
                value=f"{pid}:{vid}",
                description=f"{shop} – {price_str}"[:100],
                emoji="🏷️",
            ))
        # Arten-Beobachtungen
        for sw in species_watches[:5]:
            species = sw["species"]
            label   = species[:90]
            options.append(discord.SelectOption(
                label=label,
                value=f"{_SW_PREFIX}{species}",
                description=l10n.get("pt_species_watch_type", lang),
                emoji="🔭",
            ))
        super().__init__(
            placeholder=l10n.get("pt_untrack_placeholder", lang),
            min_values=1,
            max_values=min(len(options), 25),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_products_selected(self.values, interaction)


class UntrackView(_BaseView):
    def __init__(
        self,
        tracked_rows: list,
        current_prices: dict,
        variant_prices: dict,
        species_watches: list,
        bot,
        owner_id: int,
        lang: str,
    ):
        super().__init__(owner_id, lang)
        self.bot       = bot
        self.lang      = lang
        self.add_item(_UntrackSelectItem(tracked_rows, current_prices, variant_prices, species_watches, lang))

    async def on_products_selected(self, values: list[str], interaction: discord.Interaction):
        user_id  = str(self.owner_id)
        removed  = 0
        sw_removed = 0

        for val in values:
            if val.startswith(_SW_PREFIX):
                # Arten-Beobachtung entfernen
                species = val[len(_SW_PREFIX):]
                await execute_db(
                    self.bot,
                    "DELETE FROM user_species_watch WHERE user_id=? AND species=?",
                    (user_id, species),
                    commit=True,
                )
                await execute_db(
                    self.bot,
                    "DELETE FROM user_species_watch_seen WHERE user_id=? AND watched_species=?",
                    (user_id, species),
                    commit=True,
                )
                sw_removed += 1
            else:
                # Einzelprodukt/Variante entfernen (Wert: "pid:vid")
                pid_str, _, vid_str = val.partition(":")
                await execute_db(
                    self.bot,
                    "DELETE FROM user_price_tracking WHERE user_id=? AND product_id=? AND variant_id=?",
                    (user_id, int(pid_str), int(vid_str or 0)),
                    commit=True,
                )
                removed += 1

        self.disable_all_items()
        parts = []
        if removed:
            parts.append(l10n.get("pt_untrack_done", self.lang, count=removed))
        if sw_removed:
            parts.append(l10n.get("pt_unwatch_done", self.lang, count=sw_removed))
        await interaction.response.edit_message(
            content="\n".join(parts) or l10n.get("pt_removed_generic", self.lang),
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
        self.check_species_watches.start()

    def cog_unload(self):
        self.check_price_changes.cancel()
        self.check_species_watches.cancel()

    # ── /track_price ──────────────────────────────────────────────────────────

    @discord.slash_command(
        name="track_price",
        description="Track prices for an ant species and get notified on changes.",
        description_localizations={"de": "Preise für eine Art beobachten und bei Änderung per PN informiert werden."},
    )
    @commands.guild_only()
    async def track_price(
        self,
        ctx: discord.ApplicationContext,
        species: discord.Option(str, "Artname oder Gattung (z.B. 'Oecophylla smaragdina' oder 'Camponotus')", description_localizations={"de": "Artname oder Gattung (z.B. 'Oecophylla smaragdina' oder 'Camponotus')", "en-US": "Species or genus (e.g. 'Oecophylla smaragdina' or 'Camponotus')"}),  # type: ignore[valid-type]
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
    @commands.guild_only()
    async def my_price_tracking(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        lang    = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        user_id = str(ctx.author.id)

        rows = await execute_db(
            self.bot,
            "SELECT product_id, variant_id, variant_title, species, product_title, product_url, "
            "shop_name, currency_iso, last_notified_min, last_notified_max "
            "FROM user_price_tracking WHERE user_id=? ORDER BY shop_name, species",
            (user_id,),
            fetch=True,
        )

        sw_rows = await execute_db(
            self.bot,
            "SELECT species, is_genus, created_at FROM user_species_watch WHERE user_id=? ORDER BY species",
            (user_id,),
            fetch=True,
        )

        if not rows and not sw_rows:
            await ctx.followup.send(
                l10n.get("pt_list_empty", lang), ephemeral=True
            )
            return

        await ensure_rates()
        msg_parts = []

        # Arten-Beobachtungen
        if sw_rows:
            sw_lines = [l10n.get("pt_watch_list_header", lang)]
            for sw in sw_rows:
                date_str = (sw["created_at"] or "")[:10]
                sw_lines.append(
                    l10n.get("pt_watch_list_entry", lang,
                             species=sw["species"], date=date_str)
                )
            msg_parts.append("\n".join(sw_lines))

        # Einzelprodukte
        if rows:
            pids     = [r["product_id"] for r in rows if (r["variant_id"] or 0) == 0]
            vids     = [r["variant_id"] for r in rows if (r["variant_id"] or 0) > 0]
            current  = await asyncio.to_thread(_get_latest_prices_sync, pids)
            vcurrent = await asyncio.to_thread(_get_latest_variant_prices_sync, vids)

            lines = [l10n.get("pt_list_header", lang)]
            for row in rows:
                pid   = row["product_id"]
                vid   = row["variant_id"] or 0
                title = _display_title(row)
                url   = row["product_url"] or ""
                shop  = row["shop_name"] or "?"

                if vid > 0:
                    vc = vcurrent.get(vid)
                    price_str = format_price(vc[0], vc[0], vc[1]) if vc else format_price(
                        row["last_notified_min"] or 0, row["last_notified_max"] or 0,
                        row["currency_iso"] or "EUR")
                else:
                    curr = current.get(pid)
                    price_str = format_price(curr[0], curr[1], curr[2]) if curr else format_price(
                        row["last_notified_min"] or 0, row["last_notified_max"] or 0,
                        row["currency_iso"] or "EUR")

                lines.append(l10n.get(
                    "pt_list_entry", lang,
                    title=title, url=url, shop=shop, price=price_str,
                    status="",
                ))
            msg_parts.append("\n".join(lines))

        msg = "\n\n".join(msg_parts)
        if len(msg) > 2000:
            msg = msg[:1990] + "…"

        await ctx.followup.send(msg, ephemeral=True)

    # ── /untrack_price ────────────────────────────────────────────────────────

    @discord.slash_command(
        name="untrack_price",
        description="Remove products or species watches from price tracking.",
        description_localizations={"de": "Produkte oder Arten-Beobachtungen aus dem Preis-Tracking entfernen."},
    )
    @commands.guild_only()
    async def untrack_price(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)

        lang    = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)
        user_id = str(ctx.author.id)

        rows = await execute_db(
            self.bot,
            "SELECT product_id, variant_id, variant_title, species, product_title, product_url, "
            "shop_name, currency_iso, last_notified_min, last_notified_max "
            "FROM user_price_tracking WHERE user_id=? ORDER BY shop_name, species",
            (user_id,),
            fetch=True,
        )

        sw_rows = await execute_db(
            self.bot,
            "SELECT species, is_genus FROM user_species_watch WHERE user_id=?",
            (user_id,),
            fetch=True,
        )

        if not rows and not sw_rows:
            await ctx.followup.send(
                l10n.get("pt_untrack_none", lang), ephemeral=True
            )
            return

        await ensure_rates()
        pids     = [r["product_id"] for r in rows if (r["variant_id"] or 0) == 0]
        vids     = [r["variant_id"] for r in rows if (r["variant_id"] or 0) > 0]
        current  = await asyncio.to_thread(_get_latest_prices_sync, pids) if pids else {}
        vcurrent = await asyncio.to_thread(_get_latest_variant_prices_sync, vids) if vids else {}

        view = UntrackView(list(rows), current, vcurrent, list(sw_rows), self.bot, ctx.author.id, lang)
        await ctx.followup.send(
            l10n.get("pt_untrack_select", lang),
            view=view,
            ephemeral=True,
        )

    # ── Hintergrundtask: Einzelprodukte prüfen ────────────────────────────────

    @tasks.loop(minutes=65)
    async def check_price_changes(self):
        """Prüft ~stündlich (alle 65 Min) Preisänderungen bei beobachteten Einzelprodukten."""
        try:
            rows = await execute_db(
                self.bot,
                """SELECT user_id, product_id, variant_id, variant_title, species,
                          product_title, product_url, shop_name, currency_iso,
                          last_notified_min, last_notified_max, target_price, target_mode
                   FROM user_price_tracking""",
                fetch=True,
            )
            if not rows:
                return

            prod_ids = list({r["product_id"] for r in rows if (r["variant_id"] or 0) == 0})
            var_ids  = list({r["variant_id"] for r in rows if (r["variant_id"] or 0) > 0})
            current  = await asyncio.to_thread(_get_latest_prices_sync, prod_ids)
            vcurrent = await asyncio.to_thread(_get_latest_variant_prices_sync, var_ids)
            await ensure_rates()

            for row in rows:
                pid = row["product_id"]
                vid = row["variant_id"] or 0
                if vid > 0:
                    vc = vcurrent.get(vid)
                    if not vc:
                        continue
                    curr_min = curr_max = vc[0]
                    curr_currency = vc[1]
                else:
                    curr_row = current.get(pid)
                    if not curr_row:
                        continue
                    curr_min, curr_max, curr_currency = curr_row

                last_min = row["last_notified_min"]
                last_max = row["last_notified_max"]

                if last_min is None or last_max is None:
                    await execute_db(
                        self.bot,
                        "UPDATE user_price_tracking SET last_notified_min=?, last_notified_max=?, currency_iso=? "
                        "WHERE user_id=? AND product_id=? AND variant_id=?",
                        (curr_min, curr_max, curr_currency, row["user_id"], pid, vid),
                        commit=True,
                    )
                    continue

                changed = not (curr_min == last_min and curr_max == last_max)

                target = row["target_price"]
                mode   = (row["target_mode"] or "").lower()
                reached_now    = target is not None and curr_min <= target
                reached_before = target is not None and last_min <= target
                newly_reached  = reached_now and not reached_before

                if target is not None and mode == "replace":
                    # 'ersetzt': keine Änderungs-DMs, nur die Zielpreis-DM beim Erreichen
                    if newly_reached:
                        await self._notify_target(row, curr_min, curr_max, curr_currency, target)
                else:
                    if changed:
                        await self._notify_user(row, curr_min, curr_max, curr_currency, last_min, last_max)
                    if newly_reached:
                        await self._notify_target(row, curr_min, curr_max, curr_currency, target)

                if changed:
                    await execute_db(
                        self.bot,
                        "UPDATE user_price_tracking SET last_notified_min=?, last_notified_max=?, currency_iso=? "
                        "WHERE user_id=? AND product_id=? AND variant_id=?",
                        (curr_min, curr_max, curr_currency, row["user_id"], pid, vid),
                        commit=True,
                    )

        except Exception as e:
            logger.error("❌ check_price_changes error: %s", e, exc_info=True)

    @check_price_changes.before_loop
    async def before_check_price_changes(self):
        await self.bot.wait_until_ready()

    # ── Hintergrundtask: Arten-Beobachtungen prüfen ───────────────────────────

    @tasks.loop(minutes=67)
    async def check_species_watches(self):
        """
        Prüft ~stündlich (alle 67 Min) alle Arten-Beobachtungen:
        - Neue Produkte → DM
        - Preisänderungen bei bekannten Produkten → DM
        """
        try:
            watches = await execute_db(
                self.bot,
                "SELECT user_id, species, is_genus FROM user_species_watch",
                fetch=True,
            )
            if not watches:
                return

            shop_data = await load_shop_data(self.bot)
            await ensure_rates()

            for watch in watches:
                try:
                    await self._process_species_watch(watch, shop_data)
                except Exception as e:
                    logger.error(
                        "❌ species watch error user=%s species=%s: %s",
                        watch["user_id"], watch["species"], e,
                    )

        except Exception as e:
            logger.error("❌ check_species_watches error: %s", e, exc_info=True)

    @check_species_watches.before_loop
    async def before_check_species_watches(self):
        await self.bot.wait_until_ready()
        # Tabellen user_species_watch / user_species_watch_seen werden
        # zentral in utils/db.py:init_db() angelegt.

    async def _process_species_watch(self, watch: dict, shop_data: dict):
        """Verarbeitet eine einzelne Arten-Beobachtung."""
        user_id         = watch["user_id"]
        watched_species = watch["species"]
        is_genus        = bool(watch["is_genus"])

        # Alle aktuell passenden Produkte sammeln
        current_products: dict[int, dict] = {}
        for shop_id, shop_info in shop_data.items():
            for product in shop_info.get("products", []):
                species = (product.get("species") or "").strip()
                norm    = normalize_species_name(species)
                if is_genus:
                    match = norm.startswith(watched_species + " ")
                else:
                    match = norm == watched_species
                if not match:
                    continue
                pid = product.get("id")
                if pid is None:
                    continue
                current_products[int(pid)] = {
                    **product,
                    "_shop_name": shop_info.get("name", shop_id),
                }

        # Bekannte Produkte laden
        seen_rows = await execute_db(
            self.bot,
            "SELECT product_id, last_min, last_max, currency "
            "FROM user_species_watch_seen WHERE user_id=? AND watched_species=?",
            (user_id, watched_species),
            fetch=True,
        )
        seen     = {r["product_id"]: r for r in seen_rows}
        seen_ids = set(seen.keys())
        curr_ids = set(current_products.keys())

        # 1) Neue Produkte still zur Baseline hinzufügen (kein DM – dafür gibt es /notification)
        for new_pid in curr_ids - seen_ids:
            p = current_products[new_pid]
            try:
                min_p = float(p.get("min_price") or 0)
                max_p = float(p.get("max_price") or 0)
            except (ValueError, TypeError):
                min_p = max_p = 0.0
            await execute_db(
                self.bot,
                """INSERT OR REPLACE INTO user_species_watch_seen
                   (user_id, watched_species, product_id, last_min, last_max, currency)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, watched_species, new_pid,
                 min_p or None, max_p or None, p.get("currency_iso") or "EUR"),
                commit=True,
            )

        # 2) Preisänderungen bei bereits bekannten Produkten
        overlap_ids = list(curr_ids & seen_ids)
        if overlap_ids:
            hist = await asyncio.to_thread(_get_latest_prices_sync, overlap_ids)
            for pid in overlap_ids:
                curr_price = hist.get(pid)
                if not curr_price:
                    continue
                curr_min, curr_max, curr_currency = curr_price
                seen_row = seen[pid]
                last_min = seen_row["last_min"]
                last_max = seen_row["last_max"]

                if last_min is None or last_max is None:
                    # Baseline setzen, keine DM
                    await execute_db(
                        self.bot,
                        "UPDATE user_species_watch_seen "
                        "SET last_min=?, last_max=?, currency=? "
                        "WHERE user_id=? AND watched_species=? AND product_id=?",
                        (curr_min, curr_max, curr_currency,
                         user_id, watched_species, pid),
                        commit=True,
                    )
                    continue

                if curr_min == last_min and curr_max == last_max:
                    continue

                p = current_products.get(pid, {})
                await self._notify_species_price_change(
                    user_id, p, watched_species,
                    curr_min, curr_max, curr_currency,
                    last_min, last_max,
                )
                await execute_db(
                    self.bot,
                    "UPDATE user_species_watch_seen "
                    "SET last_min=?, last_max=?, currency=? "
                    "WHERE user_id=? AND watched_species=? AND product_id=?",
                    (curr_min, curr_max, curr_currency,
                     user_id, watched_species, pid),
                    commit=True,
                )

    async def _notify_species_price_change(
        self,
        user_id: str,
        product: dict,
        watched_species: str,
        curr_min: float,
        curr_max: float,
        currency: str,
        last_min: float,
        last_max: float,
    ):
        """DM: Preisänderung bei Arten-Beobachtung."""
        try:
            uid  = int(user_id)
            user = await self.bot.fetch_user(uid)
        except Exception as e:
            logger.warning("⚠️ User %s nicht abrufbar: %s", user_id, e)
            return

        lang = await get_user_lang(self.bot, user_id, None)

        old_avg = (last_min + last_max) / 2
        new_avg = (curr_min + curr_max) / 2
        key = "pt_dm_cheaper" if new_avg < old_avg else "pt_dm_dearer"

        msg = l10n.get(
            key, lang,
            shop=product.get("_shop_name") or "?",
            species=watched_species,
            title=product.get("title") or product.get("species") or "?",
            old_price=format_price(last_min, last_max, currency),
            new_price=format_price(curr_min, curr_max, currency),
            url=product.get("antcheck_url") or "",
        )

        try:
            await user.send(msg)
        except discord.Forbidden:
            await self._fallback_server_message(user_id, msg)
        except Exception as e:
            logger.error("❌ _notify_species_price_change DM-Fehler: %s", e)

    async def _notify_user(
        self,
        row,
        curr_min: float,
        curr_max: float,
        currency: str,
        last_min: float,
        last_max: float,
    ):
        """Sendet eine DM an den User über die Preisänderung (Einzelprodukt)."""
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

        old_avg = (last_min + last_max) / 2
        new_avg = (curr_min + curr_max) / 2
        key     = "pt_dm_cheaper" if new_avg < old_avg else "pt_dm_dearer"

        msg = l10n.get(
            key, lang,
            shop=row["shop_name"] or "?",
            species=row["species"] or "?",
            title=_display_title(row),
            old_price=format_price(last_min, last_max, currency),
            new_price=format_price(curr_min, curr_max, currency),
            url=row["product_url"] or "",
        )

        # Grund-Erkennung nur bei Produkt-Tracking (variant_id=0)
        if (row["variant_id"] or 0) == 0:
            reason = await asyncio.to_thread(_get_price_reason_sync, row["product_id"])
            if reason and (
                (key == "pt_dm_dearer" and reason["direction"] == "up")
                or (key == "pt_dm_cheaper" and reason["direction"] == "down")
            ):
                rkey = _REASON_KEYS.get(reason["code"])
                if rkey:
                    rcur = reason["currency"]
                    op = f"{reason['old']:.2f} {rcur}" if reason["old"] is not None else ""
                    np = f"{reason['new']:.2f} {rcur}" if reason["new"] is not None else ""
                    msg += "\n" + l10n.get(rkey, lang, variant=reason["variant"], old=op, new=np)

        try:
            await user.send(msg)
            logger.info(
                "📩 Preis-Benachrichtigung: user=%s product=%s %s→%s",
                user_id_str, row["product_id"],
                format_price(last_min, last_max, currency),
                format_price(curr_min, curr_max, currency),
            )
        except discord.Forbidden:
            await self._fallback_server_message(user_id_str, msg)
        except Exception as e:
            logger.error("❌ Fehler beim Senden der Preis-DM an %s: %s", user_id_str, e)

    async def _notify_target(
        self,
        row,
        curr_min: float,
        curr_max: float,
        currency: str,
        target: float,
    ):
        """Sendet eine DM, wenn der gesetzte Zielpreis erreicht/unterschritten wurde."""
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

        # Erfolg-Event: Zielpreis erreicht ("Schnäppchen!")
        await log_event(self.bot, user_id_str, "target_hit")
        try:
            await check_and_grant(self.bot, user, lang)
        except Exception:
            pass

        msg = l10n.get(
            "pt_dm_target", lang,
            shop=row["shop_name"] or "?",
            species=row["species"] or "?",
            title=_display_title(row),
            new_price=format_price(curr_min, curr_max, currency),
            target=f"{target:.2f} {currency}",
            url=row["product_url"] or "",
        )
        try:
            await user.send(msg)
            logger.info(
                "🎯 Zielpreis-DM: user=%s product=%s ziel=%s",
                user_id_str, row["product_id"], target,
            )
        except discord.Forbidden:
            await self._fallback_server_message(user_id_str, msg)
        except Exception as e:
            logger.error("❌ Fehler beim Senden der Zielpreis-DM an %s: %s", user_id_str, e)

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
