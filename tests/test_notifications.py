# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Logik hinter den Benachrichtigungen:
  • Verfügbarkeit (/notification, „Mein Konto“): Regionen, Gattung, Ausschlüsse, Blacklist,
    CH-Modus, Merch, Synonyme (canonical_species), lagernd/ausverkauft
  • Grund einer Preisänderung (Grabber → Preis-DMs) und Bestandswechsel
  • Preis-Ampel (12-Monats-Spanne)
"""
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import board_snapshot as BS


def _prod(pid, species, stock=True, canonical=None, title=None, price=20):
    return {"id": pid, "species": species, "canonical_species": canonical, "title": title or species,
            "min_price": price, "max_price": price, "currency_iso": "EUR",
            "in_stock": stock, "is_active": True, "variants": []}


SHOPS = {
    "_meta": {"fetched_at": "2026-11-23T10:00:00+00:00"},
    "1": {"id": 1, "name": "ShopDE", "country": "de", "products": [
        _prod(11, "Lasius niger"), _prod(12, "Lasius flavus"), _prod(13, "Messor barbarus", stock=False),
        _prod(14, "Lasius niger", title="Poster Lasius niger 70×50"),                      # Merch
        _prod(15, "Pachycondyla apicalis", canonical="Neoponera apicalis")]},             # Synonym
    "2": {"id": 2, "name": "ShopCH", "country": "ch", "products": [_prod(21, "Lasius niger")]},
    "3": {"id": 3, "name": "ShopAT", "country": "at", "products": [_prod(31, "Camponotus ligniperda")]},
}


@pytest.fixture()
def avail():
    """Eigene shops_data.json + Bot mit DB; danach Originaldaten zurück."""
    f = Path(os.environ["SHOPS_DATA_FILE"])
    original = f.read_text(encoding="utf-8")
    f.write_text(json.dumps(SHOPS), encoding="utf-8")
    from utils.availability import check_availability_for_species, species_exists
    from utils.db import execute_db

    def run(coro_fn):
        async def go():
            from utils.db import init_db
            bot = BS.FakeBot(asyncio.get_running_loop())
            await init_db(bot)                                # nur Tabellen (idempotent)
            await execute_db(bot, "DELETE FROM user_shop_blacklist WHERE user_id='u1'", commit=True)
            return await coro_fn(bot)
        return asyncio.run(go())

    def ids(species, regions, **kw):
        async def c(bot):
            if kw.pop("blacklist", None):
                await execute_db(bot, "INSERT INTO user_shop_blacklist (user_id, shop_id) VALUES ('u1', 1)", commit=True)
                kw["user_id"] = "u1"
            res = await check_availability_for_species(bot, species, regions, **kw)
            return sorted(r["id"] for r in res)
        return run(c)

    def exists(term):
        return run(lambda bot: species_exists(bot, term))
    yield ids, exists
    f.write_text(original, encoding="utf-8")


def test_species_by_region(avail):
    ids, _ = avail
    assert ids("Lasius niger", ["de"]) == [11]                 # Poster (14) ist Merch → nie
    assert ids("Lasius niger", ["de", "ch"]) == [11, 21]
    assert ids("Lasius niger", ["at"]) == []


def test_genus_and_exclusions(avail):
    ids, _ = avail
    assert ids("Lasius", ["de", "ch"]) == [11, 12, 21]
    assert ids("Lasius", ["de", "ch"], excluded_species_list={"niger"}) == [12]


def test_stock_requirement(avail):
    ids, _ = avail
    assert ids("Messor barbarus", ["de"]) == []                  # ausverkauft
    assert ids("Messor barbarus", ["de"], require_stock=False) == [13]


def test_blacklist_and_ch_mode(avail):
    ids, _ = avail
    assert ids("Lasius niger", ["de", "ch"], blacklist=True) == [21]
    assert ids("Lasius niger", [], ch_mode=True, ch_shops={"2"}) == [21]


def test_synonym_via_canonical_species(avail):
    ids, exists = avail
    assert ids("Neoponera apicalis", ["de"]) == [15]
    assert exists("Neoponera apicalis") and exists("Lasius") and not exists("Formica rufa")


# ── Grund einer Preisänderung (grabber._classify_reason) ───────────────────────
def _variants(*items):
    return {"variants": [{"id": vid, "title": t, "price": p, "in_stock": True, "is_active": True}
                         for vid, t, p in items]}


@pytest.fixture()
def db():
    """(grabber-Modul, Cursor auf eine leere Preis-Historie im Speicher)"""
    import grabber as G
    con = sqlite3.connect(":memory:")
    con.executescript(G._PRICE_HISTORY_SCHEMA)
    yield G, con.cursor()
    con.close()


@pytest.mark.parametrize("old, new, expected", [
    ([(1, "Königin", 20), (2, "Kolonie", 40)], [(1, "Königin", 15), (2, "Kolonie", 40)], ("price_down", "Königin", 20, 15)),
    ([(1, "Königin", 20), (2, "Kolonie", 40)], [(1, "Königin", 25), (2, "Kolonie", 40)], ("price_up", "Königin", 20, 25)),
    ([(1, "Königin", 20), (2, "Kolonie", 40)], [(2, "Kolonie", 40)], ("cheapest_gone", "Königin", None, None)),
    ([(1, "Königin", 20), (2, "Kolonie", 40)], [(1, "Königin", 20)], ("expensive_gone", "Kolonie", None, None)),
    ([(1, "Königin", 20), (2, "Kolonie", 40)], [(1, "Königin", 20), (2, "Kolonie", 40), (3, "Mini", 10)],
     ("new_cheaper", "Mini", None, None)),
    ([(1, "Königin", 20), (2, "Kolonie", 40)], [(1, "Königin", 20), (2, "Kolonie", 40), (3, "Groß", 80)],
     ("new_expensive", "Groß", None, None)),
])
def test_price_change_reason(db, old, new, expected):
    G, cur = db
    G._write_snapshot(cur, 7, _variants(*old))
    o = [p for _, _, p in old]; n = [p for _, _, p in new]
    assert G._classify_reason(cur, 7, _variants(*new), min(o), max(o), min(n), max(n)) == expected


def test_price_reason_needs_snapshot(db):
    G, cur = db
    assert G._classify_reason(cur, 8, _variants((1, "a", 10)), 10, 10, 9, 9) is None


def test_stock_history_only_on_change(db):
    G, cur = db
    p = {"in_stock": True, "is_active": True}
    assert G._track_stock(cur, 5, p) == 1          # erster Eintrag
    assert G._track_stock(cur, 5, p) == 0          # unverändert → nichts
    assert G._track_stock(cur, 5, {"in_stock": False, "is_active": True}) == 1
    assert G._track_stock(cur, 5, {"in_stock": True, "is_active": False}) == 0   # inaktiv = nicht lagernd
    cur.execute("SELECT in_stock FROM product_stock_history WHERE product_id=5 ORDER BY id")
    assert [r[0] for r in cur.fetchall()] == [1, 0]


# ── Preis-Ampel (utils/price_signal.classify) ──────────────────────────────────
def _hist(now, prices_days):
    return [(now - timedelta(days=d), p, "EUR") for d, p in prices_days]


@pytest.mark.parametrize("hist, expected", [
    ([(200, 20), (100, 10), (1, 10)], "low"),
    ([(200, 10), (100, 20), (1, 20)], "high"),
    ([(200, 10), (100, 20), (1, 15)], "mid"),
    ([(30, 10), (1, 20)], None),                     # Historie kürzer als 90 Tage
    ([(200, 20.0), (1, 20.2)], None),                # Spanne zu klein
])
def test_price_signal(hist, expected):
    from utils.price_signal import classify
    now = datetime(2026, 11, 23)
    assert classify(_hist(now, hist), now, now - timedelta(days=365)) == expected
