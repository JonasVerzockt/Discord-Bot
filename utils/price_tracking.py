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
utils/price_tracking.py – Preis-Tracking: Datenzugriff & Logik ohne Discord-Oberfläche.

Preis-Historie (price_history.db), Produktsuche für /track_price und „Mein Konto“,
Speichern von Trackings und Arten-Beobachtungen. Aus cogs/price_tracking.py herausgelöst (5.0.0).
"""

import logging
import asyncio
import re
import sqlite3
from pathlib import Path
from config import DATA_DIRECTORY
from utils.db import execute_db
from utils.localization import l10n
from utils.availability import (
    available_variants, is_merch_product, load_shop_data, normalize_species_name, strip_html,
)
from utils import species_catalog

logger = logging.getLogger("cogs.price_tracking")   # Log-Name wie vor dem Umbau


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
    # Suchbegriffe inkl. AntCat-kanonischer Form (Synonym→akzeptiert).
    search_terms = {normalized}
    _cq = species_catalog.canonical(species_query)
    if _cq:
        search_terms.add(normalize_species_name(_cq))

    candidates: dict[str, list] = {}
    zero_price_ids: list[int] = []

    for shop_id, shop_info in shop_data.items():
        for product in shop_info.get("products", []):
            species = (product.get("species") or "").strip()
            if is_merch_product(product):     # Merch (auch Poster o.Ä. im Titel/Variante) nicht trackbar
                continue
            # Rohfeld + canonical_species (falls Grabber es gesetzt hat) matchen.
            cand_names = [normalize_species_name(species)]
            _cs = (product.get("canonical_species") or "").strip()
            if _cs:
                cand_names.append(normalize_species_name(_cs))

            if is_genus:
                match = any(c.startswith(t + " ") for c in cand_names for t in search_terms)
            else:
                match = any(c in search_terms for c in cand_names)

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
    Eindeutiges Label für ein Produkt im Select-Menü: der bereinigte Produkttitel.
    Entspricht der Titel dem Artnamen, wird die Produkt-ID als Disambiguator
    angehängt. Die (lange, HTML-lastige) Produkt-„description" wird bewusst NICHT
    verwendet – die konkreten Varianten wählt man im nachfolgenden Varianten-Schritt
    aus dem `variants`-Feld.
    """
    raw_title = strip_html(p.get("title") or p.get("species") or "?")
    species   = strip_html(p.get("species") or "")
    pid_str   = str(p.get("id", ""))

    if raw_title.lower() != species.lower():
        label = raw_title
    elif pid_str:
        label = f"{raw_title} (#{pid_str})"
    else:
        label = raw_title

    return label[:97]


_VARIANT_SIZE_RE = re.compile(r"\d+\s*[-~–—]\s*\d+")


def _variant_size_summary(product) -> str:
    """Kompakte Zusammenfassung der lagernden Varianten-Größen (z.B. '3-6, 10-20, 25-50').
    Sprachneutral: zieht Zahlenspannen aus den Variantentiteln, sonst den Kurztitel."""
    tokens: list[str] = []
    for v in available_variants(product):
        t = strip_html(v.get("title") or v.get("description") or "")
        m = _VARIANT_SIZE_RE.search(t)
        tok = (m.group(0).replace(" ", "") if m else t).strip()
        if tok and tok not in tokens:
            tokens.append(tok)
    return ", ".join(tokens)


def _current_variant_map(product: dict) -> dict:
    """{variant_id: (title, price_float, currency)} der lagernden, aktiven Varianten
    mit echtem Preis (>0). Basis für den Pro-Variante-Vergleich der Arten-Beobachtung."""
    out: dict[int, tuple] = {}
    for v in available_variants(product):
        vid = v.get("id")
        if vid is None:
            continue
        try:
            price = float(str(v.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        title = strip_html(v.get("title") or v.get("description") or f"Variante {vid}")
        cur   = v.get("currency_iso") or product.get("currency_iso") or "EUR"
        out[int(vid)] = (title, price, cur)
    return out


def _variant_step_header(lang, current, total, product) -> str:
    """Kopfzeile für den (ggf. mehrstufigen) Varianten-Auswahlschritt."""
    if total <= 1:
        return l10n.get("pt_select_variant", lang)
    return l10n.get(
        "pt_variant_step", lang,
        product=_make_product_label(product), current=current, total=total,
    )


# ── Arten-Beobachtung (alle Shops) ────────────────────────────────────────────

async def init_species_watch_seen(bot, user_id: str, normalized: str, is_genus: int):
    """Lädt alle aktuellen Produkte und speichert sie als Baseline (auch vom Board genutzt)."""
    try:
        # normalized ist bereits der akzeptierte Name; zusätzlich Roh+canonical
        # der Produkte prüfen (gleiche Logik wie im Check-Loop).
        shop_data = await load_shop_data(bot)
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
                    match = any(c.startswith(normalized + " ") for c in cand_names)
                else:
                    match = normalized in cand_names
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
                    bot,
                    """INSERT OR IGNORE INTO user_species_watch_seen
                       (user_id, watched_species, product_id, last_min, last_max, currency)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(user_id), normalized, int(pid),
                        min_p or None, max_p or None,
                        product.get("currency_iso") or "EUR",
                    ),
                    commit=True,
                )
    except Exception as e:
        logger.error("❌ _init_seen_products error: %s", e)


# ── Bestätigung ───────────────────────────────────────────────────────────────

async def save_tracking(bot, user_id: str, shop_id: str, shop_name: str, p: dict) -> bool:
    """Legt EIN Preis-Tracking an (Produkt oder Variante); False = gab es schon.
    Baseline = letzter bekannter Preis aus der Historie (sonst aktueller Shop-Preis).
    Wird von der Discord-Auswahl und vom Board (/konto) genutzt."""
    pid = p.get("id")
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
        bot,
        """INSERT INTO user_price_tracking
           (user_id, product_id, variant_id, variant_title, species,
            product_title, product_url, shop_name, shop_id, currency_iso,
            last_notified_min, last_notified_max)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, product_id, variant_id) DO NOTHING""",
        (
            user_id, pid, vid, vtitle,
            p.get("species") or "",
            strip_html(p.get("title") or p.get("species") or ""),
            p.get("antcheck_url") or "",
            shop_name, shop_id,
            currency,
            baseline_min if (baseline_min or baseline_max) else None,
            baseline_max if (baseline_min or baseline_max) else None,
        ),
        commit=True,
    )
    return bool(rc)
