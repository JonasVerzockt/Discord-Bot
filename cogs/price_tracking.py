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

Aufteilung seit 5.0.0:
  cogs/price_tracking.py         – dieses Cog: Slash-Befehle, Hintergrund-Jobs, DM-Versand
  utils/price_tracking.py        – Datenzugriff/Logik (Preis-Historie, Produktsuche, Speichern)
  utils/price_tracking_views.py  – Discord-Oberfläche (Auswahlmenüs, Buttons)
"""

import asyncio
import logging
from collections import defaultdict
from datetime import time as dtime
from zoneinfo import ZoneInfo
import discord
from discord.ext import commands, tasks
from utils.db import execute_db, execute_many
from utils.localization import get_user_lang, l10n
from cogs.server_settings import allowed_channel
from utils.availability import is_merch_product, load_shop_data, normalize_species_name, strip_html
from utils.embeds import send_embeds, send_embeds_to
from utils.currency import ensure_rates, format_price
from utils.achievements import check_and_grant, log_event
from utils import species_catalog
from utils.account import is_quiet

from utils.price_tracking import (
    _current_variant_map, _display_title, _find_products_for_tracking,
    _get_latest_prices_sync, _get_latest_variant_prices_sync, _get_price_reason_sync,
    _REASON_KEYS,
)
from utils.price_tracking_views import ShopSelectView, UntrackView

logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)

# Entfallene Varianten werden gesammelt und einmal täglich zu dieser Zeit gemeldet.
BERLIN = ZoneInfo("Europe/Berlin")
_REMOVED_DIGEST_TIME = dtime(hour=10, minute=0, tzinfo=BERLIN)


# ── Cog ───────────────────────────────────────────────────────────────────────

class PriceTrackingCog(commands.Cog, name="PriceTracking"):

    def __init__(self, bot: discord.Bot):
        self.bot = bot
        # check_price_changes & check_species_watches haben KEINEN eigenen Timer mehr –
        # sie werden stündlich von der Daten-Pipeline (Tasks-Cog, reload_shops_task)
        # nach dem Shop-Reload sequenziell aufgerufen (immer auf frischen Daten).
        self.flush_removed_variants.start()

    def cog_unload(self):
        self.flush_removed_variants.cancel()

    # ── /track_price ──────────────────────────────────────────────────────────

    @discord.slash_command(
        name="track_price",
        description="Track prices for an ant species and get notified on changes.",
        description_localizations={"de": "Preise für eine Art beobachten und bei Änderung per PN informiert werden."},
    )
    @commands.guild_only()
    @allowed_channel()
    async def track_price(
        self,
        ctx: discord.ApplicationContext,
        species: discord.Option(str, "Artname oder Gattung (z.B. 'Oecophylla smaragdina' oder 'Camponotus')", description_localizations={"de": "Artname oder Gattung (z.B. 'Oecophylla smaragdina' oder 'Camponotus')", "en-US": "Species or genus (e.g. 'Oecophylla smaragdina' or 'Camponotus')"}),  # type: ignore[valid-type]
        force: discord.Option(bool, "Skip name validation (allow unknown / variant spellings)", description_localizations={"de": "Namensprüfung überspringen (unbekannte/abweichende Schreibweisen zulassen)"}, required=False, default=False),  # type: ignore[valid-type]
    ):
        await ctx.defer(ephemeral=True)

        lang = await get_user_lang(self.bot, ctx.author.id, ctx.guild_id)

        # Namensprüfung gegen die Artenliste (außer bei force): bei Synonym/
        # Tippfehler/unbekannt Hinweis + korrekte Schreibweise, dann abbrechen.
        if not force:
            _msg = species_catalog.validation_message(species_catalog.check(species), lang)
            if _msg:
                await ctx.followup.send(_msg, ephemeral=True)
                return

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
    @allowed_channel()
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
        await send_embeds(ctx, msg, ephemeral=True)

    # ── /untrack_price ────────────────────────────────────────────────────────

    @discord.slash_command(
        name="untrack_price",
        description="Remove products or species watches from price tracking.",
        description_localizations={"de": "Produkte oder Arten-Beobachtungen aus dem Preis-Tracking entfernen."},
    )
    @commands.guild_only()
    @allowed_channel()
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

    async def check_price_changes(self):
        """Prüft Preisänderungen bei beobachteten Einzelprodukten.

        Kein eigener Timer mehr: wird stündlich von der Daten-Pipeline (Tasks-Cog,
        reload_shops_task) direkt nach dem Shop-Reload aufgerufen – so immer auf
        frischen Shop-Daten und in fester Reihenfolge vor den Arten-Beobachtungen."""
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
                if is_quiet(row["user_id"]):
                    # Ruhezeit: Vergleichsbasis NICHT anfassen, die Änderung wird nach dem
                    # Ende der Ruhezeit beim nächsten stündlichen Lauf gemeldet.
                    continue
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

    # ── Arten-Beobachtungen prüfen (Teil der Daten-Pipeline) ──────────────────

    async def check_species_watches(self):
        """
        Prüft alle Arten-Beobachtungen (kein eigener Timer – stündlich von der
        Daten-Pipeline nach check_price_changes aufgerufen):
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
                if is_quiet(watch["user_id"]):
                    continue        # Ruhezeit: Baseline bleibt, Meldung kommt danach
                try:
                    await self._process_species_watch(watch, shop_data)
                except Exception as e:
                    logger.error(
                        "❌ species watch error user=%s species=%s: %s",
                        watch["user_id"], watch["species"], e,
                    )
            # Sammel-PN nachholen, die wegen Ruhezeit um 10 Uhr ausgefallen ist
            await self._flush_removed(overdue_only=True)

        except Exception as e:
            logger.error("❌ check_species_watches error: %s", e, exc_info=True)

    async def _process_species_watch(self, watch: dict, shop_data: dict):
        """Verarbeitet eine einzelne Arten-Beobachtung."""
        user_id         = watch["user_id"]
        watched_species = watch["species"]
        is_genus        = bool(watch["is_genus"])
        # Auch die kanonische Form der (evtl. veraltet gespeicherten) Beobachtung
        # berücksichtigen → alte DB-Einträge greifen ohne Migration.
        watch_terms = {watched_species}
        _cw = species_catalog.canonical(watched_species)
        if _cw:
            watch_terms.add(normalize_species_name(_cw))

        # Alle aktuell passenden Produkte sammeln
        current_products: dict[int, dict] = {}
        for shop_id, shop_info in shop_data.items():
            for product in shop_info.get("products", []):
                if is_merch_product(product):     # Merch (auch Poster o.Ä. im Titel) ignorieren
                    continue
                species = (product.get("species") or "").strip()
                cand_names = [normalize_species_name(species)]
                _cs = (product.get("canonical_species") or "").strip()
                if _cs:
                    cand_names.append(normalize_species_name(_cs))
                if is_genus:
                    match = any(c.startswith(t + " ") for c in cand_names for t in watch_terms)
                else:
                    match = any(c in watch_terms for c in cand_names)
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
        #    Sowohl die Produkt-Spanne als auch die Pro-Variante-Baseline seeden.
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
            await self._save_variant_baseline(
                user_id, watched_species, new_pid, _current_variant_map(p)
            )

        # 2) Preisänderungen bei bereits bekannten Produkten
        #    2a) Produkte MIT Varianten -> Pro-Variante-Vergleich (listet ALLE
        #        geänderten/neuen/entfallenen Varianten einzeln auf).
        #    2b) Produkte OHNE Varianten -> aggregierter Min/Max-Vergleich (wie bisher).
        overlap_ids = list(curr_ids & seen_ids)
        plain_ids: list[int] = []
        for pid in overlap_ids:
            p = current_products.get(pid, {})
            cvars = _current_variant_map(p)
            if cvars:
                await self._process_variant_watch(user_id, watched_species, pid, p, cvars)
            else:
                plain_ids.append(pid)

        if plain_ids:
            hist = await asyncio.to_thread(_get_latest_prices_sync, plain_ids)
            for pid in plain_ids:
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

    # ── Pro-Variante-Baseline (Arten-Beobachtung) ─────────────────────────────

    async def _load_variant_baseline(self, user_id: str, species: str, pid: int) -> dict:
        """{variant_id: (title, last_price, currency)} der gespeicherten Baseline."""
        rows = await execute_db(
            self.bot,
            "SELECT variant_id, variant_title, last_price, currency "
            "FROM user_species_watch_variant_seen "
            "WHERE user_id=? AND watched_species=? AND product_id=?",
            (user_id, species, int(pid)), fetch=True,
        )
        return {r["variant_id"]: (r["variant_title"], r["last_price"], r["currency"]) for r in rows}

    async def _save_variant_baseline(self, user_id: str, species: str, pid: int, cvars: dict) -> None:
        """Ersetzt die Varianten-Baseline eines Produkts durch den aktuellen Satz."""
        await execute_db(
            self.bot,
            "DELETE FROM user_species_watch_variant_seen "
            "WHERE user_id=? AND watched_species=? AND product_id=?",
            (user_id, species, int(pid)), commit=True,
        )
        if not cvars:
            return
        await execute_many(
            self.bot,
            "INSERT OR REPLACE INTO user_species_watch_variant_seen "
            "(user_id, watched_species, product_id, variant_id, variant_title, last_price, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(user_id, species, int(pid), int(vid), t, pr, cur)
             for vid, (t, pr, cur) in cvars.items()],
        )

    async def _process_variant_watch(self, user_id: str, species: str, pid: int,
                                     product: dict, cvars: dict) -> None:
        """Vergleicht den aktuellen Varianten-Satz eines Produkts gegen die Baseline
        und meldet ALLE geänderten/neuen/entfallenen Varianten in einer DM."""
        baseline = await self._load_variant_baseline(user_id, species, pid)
        if not baseline:
            # Erstmalig gesehen (z.B. vor diesem Feature angelegte Beobachtung) -> still seeden
            await self._save_variant_baseline(user_id, species, pid, cvars)
            return

        changed, added, removed = [], [], []
        for vid, (title, price, cur) in cvars.items():
            if vid in baseline:
                old = baseline[vid][1]
                if old is not None and abs(price - old) > 1e-6:
                    changed.append((title, old, price, cur))
            else:
                added.append((vid, title, price, cur))
        for vid, (title, old, cur) in baseline.items():
            if vid not in cvars:
                removed.append((vid, title, old, cur))

        # Neu aufgetauchte Varianten heben eine ggf. hängende „entfallen"-Meldung auf.
        if added:
            await execute_many(
                self.bot,
                "DELETE FROM pending_variant_removed "
                "WHERE user_id=? AND watched_species=? AND product_id=? AND variant_id=?",
                [(user_id, species, int(pid), int(vid)) for vid, *_ in added],
            )

        # Entfallene Varianten NICHT sofort melden, sondern für den täglichen
        # Sammel-Alert einreihen.
        if removed:
            await self._enqueue_removed(user_id, species, product, removed)

        # Geänderte + neue Varianten weiterhin sofort melden.
        added_simple = [(t, p, c) for _vid, t, p, c in added]
        if changed or added_simple:
            await self._notify_species_variant_changes(
                user_id, product, species, changed, added_simple
            )

        if changed or added or removed:
            await self._save_variant_baseline(user_id, species, pid, cvars)

    async def _enqueue_removed(self, user_id: str, species: str, product: dict, removed: list) -> None:
        """Reiht entfallene Varianten für den täglichen Sammel-Alert ein."""
        pid = product.get("id")
        if pid is None or not removed:
            return
        await execute_many(
            self.bot,
            "INSERT OR REPLACE INTO pending_variant_removed "
            "(user_id, watched_species, product_id, variant_id, variant_title, last_price, "
            " currency, product_title, shop_name, antcheck_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(user_id, species, int(pid), int(vid), title, old, cur,
              strip_html(product.get("title") or product.get("species") or "?"),
              product.get("_shop_name") or "?",
              product.get("antcheck_url") or "")
             for vid, title, old, cur in removed],
        )

    async def _notify_species_variant_changes(self, user_id: str, product: dict,
                                              watched_species: str,
                                              changed: list, added: list) -> None:
        """Sofort-DM: geänderte (alt→neu) und neu hinzugekommene Varianten.
        Entfallene Varianten laufen separat über den täglichen Sammel-Alert."""
        try:
            user = await self.bot.fetch_user(int(user_id))
        except Exception as e:
            logger.warning("⚠️ User %s nicht abrufbar: %s", user_id, e)
            return
        lang = await get_user_lang(self.bot, user_id, None)

        lines = [l10n.get(
            "pt_var_alert_header", lang,
            shop=product.get("_shop_name") or "?",
            title=strip_html(product.get("title") or product.get("species") or "?"),
            species=watched_species,
            url=product.get("antcheck_url") or "",
        )]
        for title, old, new, cur in changed:
            key = "pt_var_line_down" if new < old else "pt_var_line_up"
            lines.append(l10n.get(
                key, lang, variant=title,
                old=format_price(old, old, cur), new=format_price(new, new, cur),
            ))
        for title, price, cur in added:
            lines.append(l10n.get("pt_var_line_new", lang, variant=title,
                                  price=format_price(price, price, cur)))

        msg = "\n".join(lines)
        try:
            await send_embeds_to(user, msg)
            logger.info("📩 Varianten-Preis-DM: user=%s changed=%d added=%d",
                        user_id, len(changed), len(added))
        except discord.Forbidden:
            await self._fallback_server_message(user_id, msg)
        except Exception as e:
            logger.error("❌ _notify_species_variant_changes DM-Fehler: %s", e)

    # ── Täglicher Sammel-Alert für entfallene Varianten ───────────────────────

    @tasks.loop(time=_REMOVED_DIGEST_TIME)
    async def flush_removed_variants(self):
        """Verschickt einmal täglich (feste Zeit) die gesammelten entfallenen Varianten."""
        await self._flush_removed()

    async def _flush_removed(self, overdue_only: bool = False):
        """Sammel-PN je Person. Wer gerade Ruhezeit hat, wird übersprungen; das holt der
        stündliche Lauf nach (overdue_only=True: nur Einträge von vor der letzten 10-Uhr-Runde)."""
        try:
            sql = ("SELECT user_id, watched_species, product_id, variant_id, variant_title, "
                   "       last_price, currency, product_title, shop_name, antcheck_url "
                   "FROM pending_variant_removed ")
            params: tuple = ()
            if overdue_only:
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                now = _dt.now(BERLIN)
                last = now.replace(hour=_REMOVED_DIGEST_TIME.hour, minute=_REMOVED_DIGEST_TIME.minute,
                                   second=0, microsecond=0)
                if last > now:
                    last -= _td(days=1)
                sql += "WHERE detected_at < ? "
                params = (last.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S"),)
            rows = await execute_db(self.bot, sql + "ORDER BY user_id, shop_name, product_title, variant_title",
                                    params, fetch=True)
            if not rows:
                return
            await ensure_rates()
            by_user: dict[str, list] = defaultdict(list)
            for r in rows:
                if not is_quiet(r["user_id"]):
                    by_user[r["user_id"]].append(r)

            for uid, items in by_user.items():
                try:
                    await self._send_removed_digest(uid, items)
                except Exception as e:
                    logger.error("❌ removed-digest an %s fehlgeschlagen: %s", uid, e)
                # Nur die verschickten Zeilen löschen (nicht später Hinzugekommenes).
                await execute_many(
                    self.bot,
                    "DELETE FROM pending_variant_removed "
                    "WHERE user_id=? AND watched_species=? AND product_id=? AND variant_id=?",
                    [(r["user_id"], r["watched_species"], r["product_id"], r["variant_id"])
                     for r in items],
                )
            if by_user:
                logger.info("🗓️ Entfallene-Varianten-Digest%s: %d User, %d Einträge",
                            " (nachgeholt)" if overdue_only else "",
                            len(by_user), sum(len(v) for v in by_user.values()))
        except Exception as e:
            logger.error("❌ flush_removed_variants error: %s", e, exc_info=True)

    @flush_removed_variants.before_loop
    async def before_flush_removed_variants(self):
        await self.bot.wait_until_ready()

    async def _send_removed_digest(self, user_id: str, items: list) -> None:
        """Baut die Tages-Übersicht entfallener Varianten und schickt sie als DM."""
        try:
            user = await self.bot.fetch_user(int(user_id))
        except Exception as e:
            logger.warning("⚠️ User %s nicht abrufbar: %s", user_id, e)
            return
        lang = await get_user_lang(self.bot, user_id, None)

        # Nach Produkt (Titel+Shop+Art+URL) gruppieren.
        groups: dict[tuple, list] = defaultdict(list)
        for r in items:
            groups[(r["product_title"] or "?", r["shop_name"] or "?",
                    r["watched_species"] or "?", r["antcheck_url"] or "")].append(r)

        lines = [l10n.get("pt_var_removed_digest_header", lang)]
        for (title, shop, species, url), rs in groups.items():
            lines.append("")
            lines.append(l10n.get("pt_var_removed_product", lang,
                                  title=title, shop=shop, species=species, url=url))
            for r in rs:
                cur = r["currency"] or "EUR"
                price = format_price(r["last_price"], r["last_price"], cur) if r["last_price"] is not None else "?"
                lines.append(l10n.get("pt_var_line_gone", lang,
                                      variant=r["variant_title"] or "?", price=price))

        msg = "\n".join(lines)
        try:
            await send_embeds_to(user, msg)
        except discord.Forbidden:
            await self._fallback_server_message(user_id, msg)
        except Exception as e:
            logger.error("❌ _send_removed_digest DM-Fehler: %s", e)

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

        # Variantengrund anhängen: WELCHE Variante sich geändert hat (alt→neu) bzw.
        # welche neu hinzukam – aus der vom Grabber gepflegten product_price_reason
        # (gleiche Logik wie beim Einzelprodukt-Alert), damit die Spannen-Änderung
        # konkret nachvollziehbar ist.
        pid = product.get("id")
        if pid is not None:
            reason = await asyncio.to_thread(_get_price_reason_sync, int(pid))
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
            await send_embeds_to(user, msg)
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
            await send_embeds_to(user, msg)
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
            await send_embeds_to(user, msg)
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
