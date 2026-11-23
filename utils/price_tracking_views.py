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
utils/price_tracking_views.py – Discord-Oberfläche des Preis-Trackings (Auswahlmenüs, Buttons).

Shop → Produkt → Variante → Bestätigen, Arten-Beobachtung, Entfernen.
Aus cogs/price_tracking.py herausgelöst (5.0.0); Verhalten unverändert.
"""

import discord
from utils.db import execute_db
from utils.localization import l10n
from utils.availability import available_variants, normalize_species_name, strip_html
from utils.currency import format_price
from utils.achievements import check_and_grant
from utils import species_catalog

from utils.price_tracking import (
    _ALL_SHOPS_SENTINEL, _display_title, _make_product_label, _SW_PREFIX,
    _variant_size_summary, _variant_step_header, init_species_watch_seen, save_tracking,
)


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
        # Beobachtung auf den akzeptierten (AntCat-)Namen normalisieren, damit die
        # Baseline sauber ist und Synonyme shopübergreifend vereinheitlicht werden.
        _canon = species_catalog.canonical(self.species_raw)
        normalized = normalize_species_name(_canon) if _canon else self.species_normalized
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
        await init_species_watch_seen(self.bot, str(self.owner_id), normalized, is_genus)

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

            vsum  = _variant_size_summary(p)
            n_var = len(available_variants(p))
            if vsum:
                desc = f"{price_str} · {vsum}"
            elif n_var:
                desc = f"{price_str} · " + l10n.get("pt_variant_count", lang, count=n_var)
            else:
                desc = price_str

            options.append(discord.SelectOption(
                label=label,
                value=str(p.get("id", "")),
                description=desc[:100],
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

        # Produkte mit Varianten -> sequentiell pro Produkt die Variante(n) wählen;
        # Produkte ohne Varianten kommen direkt als ganzes Produkt in die Sammlung.
        with_variants = [p for p in selected if available_variants(p)]
        without       = [p for p in selected if not available_variants(p)]
        if with_variants:
            vview = VariantSelectView(
                with_variants[0], self.shop_id, self.shop_info, self.species,
                self.bot, self.owner_id, self.lang,
                queue=with_variants[1:], acc=list(without),
                step=1, total=len(with_variants),
            )
            await interaction.response.edit_message(
                content=_variant_step_header(self.lang, 1, len(with_variants), with_variants[0]),
                view=vview,
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
            label = strip_html(v.get("title") or v.get("description") or f"Variante {vid}")[:97]
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
    def __init__(self, product, shop_id, shop_info, species, bot, owner_id, lang,
                 queue=None, acc=None, step=1, total=1):
        super().__init__(owner_id, lang)
        self.product   = product
        self.shop_id   = shop_id
        self.shop_info = shop_info
        self.species   = species
        self.bot       = bot
        self.lang      = lang
        self.queue     = queue or []   # noch zu bearbeitende Produkte mit Varianten
        self.acc       = acc or []     # bereits gewählte Einträge (inkl. Ganzprodukte)
        self.step      = step
        self.total     = total
        self.add_item(_VariantSelectItem(product, lang))

    async def on_variant_selected(self, values, interaction):
        vmap = {str(v.get("id")): v for v in available_variants(self.product)}
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
                e["_variant_title"]    = strip_html(v.get("title") or v.get("description") or f"Variante {val}")
                e["_variant_price"]    = v.get("price")
                e["_variant_currency"] = v.get("currency_iso") or self.product.get("currency_iso") or "EUR"
            self.acc.append(e)

        # Nächstes Produkt mit Varianten? -> weiterer Auswahlschritt
        if self.queue:
            nxt = self.queue[0]
            vview = VariantSelectView(
                nxt, self.shop_id, self.shop_info, self.species,
                self.bot, self.owner_id, self.lang,
                queue=self.queue[1:], acc=self.acc,
                step=self.step + 1, total=self.total,
            )
            await interaction.response.edit_message(
                content=_variant_step_header(self.lang, self.step + 1, self.total, nxt),
                view=vview,
            )
            return

        # Alle Produkte abgearbeitet -> Bestätigung über alle gesammelten Einträge
        lines = []
        for e in self.acc:
            vt = e.get("_variant_title")
            title = _make_product_label(e) + (f" – {vt}" if vt else "")
            if e.get("_variant_id"):
                price_str = format_price(e.get("_variant_price"), e.get("_variant_price"), e.get("_variant_currency"))
            else:
                price_str = format_price(e.get("min_price", 0), e.get("max_price", 0), e.get("currency_iso", "EUR"))
            lines.append(f"• {title} – {price_str}")

        content = l10n.get("pt_confirm_header", self.lang) + "\n" + "\n".join(lines)
        view = ConfirmView(self.acc, self.shop_id, self.shop_info, self.species,
                           self.bot, self.owner_id, self.lang)
        await interaction.response.edit_message(content=content, view=view)


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
            if p.get("id") is None:
                continue
            if await save_tracking(self.bot, user_id, self.shop_id, shop_name, p):
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
                await execute_db(
                    self.bot,
                    "DELETE FROM user_species_watch_variant_seen WHERE user_id=? AND watched_species=?",
                    (user_id, species),
                    commit=True,
                )
                await execute_db(
                    self.bot,
                    "DELETE FROM pending_variant_removed WHERE user_id=? AND watched_species=?",
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
