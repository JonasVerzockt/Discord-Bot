# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Preis-DMs (cogs/price_tracking.py, check_price_changes):
  • erster Lauf setzt nur die Vergleichsbasis, keine DM
  • günstiger / teurer → passende DM, Basis wird nachgezogen
  • Grund der Änderung (product_price_reason) wird angehängt, wenn die Richtung passt
  • Zielpreis: DM nur beim ersten Erreichen, Modus „replace“ unterdrückt Änderungs-DMs
  • Ruhezeit: keine DM, Basis bleibt unverändert
  • Varianten-Tracking liest variant_price_history
  • DM gesperrt (Forbidden) → Hinweis im Server-Kanal

Discord ist durch FakeUser/FakeChannel ersetzt, es geht nichts nach außen.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import discord
import pytest

import board_snapshot as BS

UID = "900000000000000001"
SERVER = 424242
CHANNEL = 515151


class FakeUser:
    def __init__(self, uid, blocked=False):
        self.id = int(uid)
        self.blocked = blocked
        self.sent: list[str] = []

    async def send(self, content=None, embed=None, **kw):
        if self.blocked:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "DMs gesperrt")
        self.sent.append(embed.description if embed is not None else content)


class FakeChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


class DMBot(BS.FakeBot):
    def __init__(self, loop, blocked=False):
        super().__init__(loop)
        self.user_obj = FakeUser(UID, blocked)
        self.channel = FakeChannel()

    async def fetch_user(self, uid):
        return self.user_obj

    def get_channel(self, cid):
        return self.channel if int(cid) == CHANNEL else None


def _history(products=(), variants=(), reason=None):
    """price_history.db neu anlegen: products=[(pid,min,max)], variants=[(vid,pid,price)]."""
    import grabber
    from utils.price_tracking import PRICE_HISTORY_DB
    PRICE_HISTORY_DB.unlink(missing_ok=True)
    con = sqlite3.connect(PRICE_HISTORY_DB)
    con.executescript(grabber._PRICE_HISTORY_SCHEMA)
    con.executemany("INSERT INTO product_price_history (product_id, min_price, max_price) VALUES (?,?,?)",
                    list(products))
    con.executemany("INSERT INTO variant_price_history (variant_id, product_id, price) VALUES (?,?,?)",
                    list(variants))
    if reason:
        con.execute("INSERT INTO product_price_reason (product_id, recorded_at, direction, code, "
                    "variant_title, old_price, new_price, currency_iso) VALUES (?,datetime('now'),?,?,?,?,?,'EUR')",
                    reason)
    con.commit()
    con.close()


def _run(rows, *, blocked=False, quiet=False, fallback=False, monkeypatch=None):
    """Tracking-Zeilen anlegen, check_price_changes laufen lassen.
    rows: dicts mit product_id, variant_id, last_min, last_max, target, mode.
    Rückgabe: (DM-Texte, Kanal-Texte, Tracking-Zeilen danach)."""
    import cogs.price_tracking as PT
    from utils.db import execute_db, init_db

    if monkeypatch is not None:
        monkeypatch.setattr(PT, "is_quiet", lambda uid: quiet)

    async def go():
        bot = DMBot(asyncio.get_running_loop(), blocked=blocked)
        await init_db(bot)
        for t in ("user_price_tracking", "server_user_mappings"):
            await execute_db(bot, f"DELETE FROM {t} WHERE user_id=?", (UID,), commit=True)
        await execute_db(bot, "DELETE FROM server_settings WHERE server_id=?", (SERVER,), commit=True)
        if fallback:
            await execute_db(bot, "INSERT INTO server_user_mappings (user_id, server_id) VALUES (?,?)",
                             (UID, SERVER), commit=True)
            await execute_db(bot, "INSERT INTO server_settings (server_id, channel_id) VALUES (?,?)",
                             (SERVER, CHANNEL), commit=True)
        for r in rows:
            await execute_db(
                bot,
                "INSERT INTO user_price_tracking (user_id, product_id, variant_id, variant_title, species, "
                "product_title, product_url, shop_name, currency_iso, last_notified_min, last_notified_max, "
                "target_price, target_mode) VALUES (?,?,?,?,?,?,?,?,'EUR',?,?,?,?)",
                (UID, r["product_id"], r.get("variant_id", 0), r.get("variant_title", ""),
                 "Lasius niger", "Lasius niger Kolonie", "https://shop.example/ln", "ShopDE",
                 r.get("last_min"), r.get("last_max"), r.get("target"), r.get("mode")),
                commit=True,
            )
        cog = PT.PriceTrackingCog.__new__(PT.PriceTrackingCog)   # ohne Timer-Start
        cog.bot = bot
        await cog.check_price_changes()
        after = await execute_db(
            bot,
            "SELECT product_id, variant_id, last_notified_min AS mn, last_notified_max AS mx "
            "FROM user_price_tracking WHERE user_id=? ORDER BY product_id, variant_id",
            (UID,), fetch=True)
        # Erfolgs-/Rang-DMs (🏆/🎉) kommen aus check_and_grant und nur einmal je Person
        price = [m for m in bot.user_obj.sent if not m.startswith(("🏆", "🎉"))]
        _run.extra = [m for m in bot.user_obj.sent if m.startswith(("🏆", "🎉"))]
        return price, bot.channel.sent, [dict(a) for a in after]

    return asyncio.run(go())


@pytest.fixture()
def pt(monkeypatch):
    def run(rows, **kw):
        return _run(rows, monkeypatch=monkeypatch, **kw)
    yield run
    from utils.price_tracking import PRICE_HISTORY_DB
    PRICE_HISTORY_DB.unlink(missing_ok=True)


def test_first_run_sets_baseline_without_dm(pt):
    _history(products=[(501, 20.0, 30.0)])
    dms, _, after = pt([{"product_id": 501}])
    assert dms == []
    assert (after[0]["mn"], after[0]["mx"]) == (20.0, 30.0)


def test_unchanged_price_no_dm(pt):
    _history(products=[(501, 20.0, 30.0)])
    dms, _, _ = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}])
    assert dms == []


def test_cheaper_dm_and_baseline_update(pt):
    _history(products=[(501, 15.0, 25.0)])
    dms, _, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}])
    assert len(dms) == 1
    assert "Price drop" in dms[0] and "ShopDE" in dms[0] and "shop.example/ln" in dms[0]
    assert (after[0]["mn"], after[0]["mx"]) == (15.0, 25.0)


def test_dearer_dm(pt):
    _history(products=[(501, 25.0, 35.0)])
    dms, _, _ = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}])
    assert len(dms) == 1 and "Price increase" in dms[0]


def test_latest_history_row_wins(pt):
    import grabber
    from utils.price_tracking import PRICE_HISTORY_DB
    _history()
    con = sqlite3.connect(PRICE_HISTORY_DB)
    con.executescript(grabber._PRICE_HISTORY_SCHEMA)
    con.execute("INSERT INTO product_price_history (product_id, min_price, max_price, recorded_at) "
                "VALUES (501, 99, 99, '2026-01-01 00:00:00')")
    con.execute("INSERT INTO product_price_history (product_id, min_price, max_price, recorded_at) "
                "VALUES (501, 10, 10, '2026-06-01 00:00:00')")
    con.commit()
    con.close()
    dms, _, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 20.0}])
    assert "Price drop" in dms[0] and after[0]["mn"] == 10.0


def test_reason_appended_when_direction_matches(pt):
    _history(products=[(501, 25.0, 35.0)],
             reason=(501, "up", "cheapest_gone", "Königin + 5", 20.0, 25.0))
    dms, _, _ = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}])
    assert "Price increase" in dms[0]
    assert "cheapest variant" in dms[0] and "Königin + 5" in dms[0]


def test_reason_skipped_when_direction_differs(pt):
    _history(products=[(501, 15.0, 25.0)],
             reason=(501, "up", "cheapest_gone", "Königin + 5", 20.0, 25.0))
    dms, _, _ = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}])
    assert "Price drop" in dms[0] and "Reason" not in dms[0]


def test_target_reached_first_time(pt):
    _history(products=[(501, 18.0, 30.0)])
    dms, _, _ = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0, "target": 19.0}])
    assert len(dms) == 2
    assert "Price drop" in dms[0]
    assert "Target price reached" in dms[1] and "19.00 EUR" in dms[1]


def test_target_hit_counts_for_achievement(pt):
    from utils.db import execute_db
    _history(products=[(501, 18.0, 30.0)])
    pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0, "target": 19.0}])

    async def events():
        bot = BS.FakeBot(asyncio.get_running_loop())
        return await execute_db(bot, "SELECT COUNT(*) AS n FROM user_events WHERE user_id=? AND event='target_hit'",
                                (UID,), fetch=True)
    assert asyncio.run(events())[0]["n"] >= 1


def test_target_already_reached_no_second_target_dm(pt):
    _history(products=[(501, 17.0, 30.0)])
    dms, _, _ = pt([{"product_id": 501, "last_min": 18.0, "last_max": 30.0, "target": 19.0}])
    assert len(dms) == 1 and "Target price" not in dms[0]


def test_replace_mode_only_target_dm(pt):
    _history(products=[(501, 18.0, 30.0)])
    dms, _, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0,
                         "target": 19.0, "mode": "replace"}])
    assert len(dms) == 1 and "Target price reached" in dms[0]
    assert after[0]["mn"] == 18.0


def test_replace_mode_change_without_target_silent(pt):
    _history(products=[(501, 25.0, 30.0)])
    dms, _, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0,
                         "target": 19.0, "mode": "replace"}])
    assert dms == []
    assert after[0]["mn"] == 25.0                  # Basis trotzdem nachgezogen


def test_quiet_hours_skip_and_keep_baseline(pt):
    _history(products=[(501, 15.0, 25.0)])
    dms, _, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}], quiet=True)
    assert dms == []
    assert (after[0]["mn"], after[0]["mx"]) == (20.0, 30.0)


def test_variant_tracking(pt):
    _history(products=[(501, 1.0, 1.0)], variants=[(7001, 501, 12.5)])
    dms, _, after = pt([{"product_id": 501, "variant_id": 7001, "variant_title": "Königin + 10",
                         "last_min": 14.0, "last_max": 14.0}])
    assert len(dms) == 1 and "Price drop" in dms[0] and "Königin + 10" in dms[0]
    assert after[0]["mn"] == 12.5 and after[0]["mx"] == 12.5


def test_missing_history_is_ignored(pt):
    _history()
    dms, _, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}])
    assert dms == [] and after[0]["mn"] == 20.0


def test_forbidden_falls_back_to_server_channel(pt):
    _history(products=[(501, 15.0, 25.0)])
    dms, channel, _ = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}],
                         blocked=True, fallback=True)
    assert dms == []
    assert len(channel) == 1 and "Price drop" in channel[0]


def test_forbidden_without_server_mapping_is_silent(pt):
    _history(products=[(501, 15.0, 25.0)])
    dms, channel, after = pt([{"product_id": 501, "last_min": 20.0, "last_max": 30.0}], blocked=True)
    assert dms == [] and channel == []
    assert after[0]["mn"] == 15.0                  # kein Endlos-Wiederholen


# ── Arten-Beobachtung (check_species_watches / _process_species_watch) ────────

def _shop(products):
    return {"1": {"id": 1, "name": "ShopDE", "country": "de", "products": products}}


def _sprod(pid, species="Lasius niger", lo=20.0, hi=30.0, variants=None, title=None):
    return {"id": pid, "species": species, "title": title or f"{species} Kolonie",
            "min_price": lo, "max_price": hi, "currency_iso": "EUR", "in_stock": True,
            "is_active": True, "antcheck_url": f"https://antcheck.example/{pid}",
            "variants": variants or []}


def _var(vid, title, price, stock=True):
    return {"id": vid, "title": title, "price": price, "in_stock": stock, "is_active": True}


def _species_rounds(rounds, species="lasius niger", genus=False):
    """Mehrere Läufe nacheinander: rounds=[(shop_data, history_products), …].
    Rückgabe: Liste der Preis-DMs je Lauf + Bot (für Folgeabfragen)."""
    import cogs.price_tracking as PT
    from utils.db import execute_db, init_db

    async def go():
        bot = DMBot(asyncio.get_running_loop())
        await init_db(bot)
        for t, col in (("user_species_watch", "user_id"), ("user_species_watch_seen", "user_id"),
                       ("user_species_watch_variant_seen", "user_id"), ("pending_variant_removed", "user_id")):
            await execute_db(bot, f"DELETE FROM {t} WHERE {col}=?", (UID,), commit=True)
        await execute_db(bot, "INSERT INTO user_species_watch (user_id, species, is_genus) VALUES (?,?,?)",
                         (UID, species, int(genus)), commit=True)
        cog = PT.PriceTrackingCog.__new__(PT.PriceTrackingCog)
        cog.bot = bot
        out = []
        watch = {"user_id": UID, "species": species, "is_genus": int(genus)}
        for shop_data, hist in rounds:
            _history(products=hist)
            before = len(bot.user_obj.sent)
            await cog._process_species_watch(watch, shop_data)
            out.append([m for m in bot.user_obj.sent[before:] if not m.startswith(("🏆", "🎉"))])
        pending = await execute_db(bot, "SELECT variant_title FROM pending_variant_removed WHERE user_id=?",
                                   (UID,), fetch=True)
        digest_before = len(bot.user_obj.sent)
        await cog._flush_removed()
        digest = bot.user_obj.sent[digest_before:]
        return out, [p["variant_title"] for p in pending], digest

    return asyncio.run(go())


def test_species_new_product_silent_then_range_change():
    s = _shop([_sprod(601)])
    out, _, _ = _species_rounds([(s, [(601, 20.0, 30.0)]),
                                 (s, [(601, 20.0, 30.0)]),
                                 (s, [(601, 15.0, 30.0)])])
    assert out[0] == [] and out[1] == []                 # neu → nur Basis, unverändert → nichts
    assert len(out[2]) == 1 and "Price drop" in out[2][0] and "antcheck.example/601" in out[2][0]


def test_species_ignores_other_species_and_merch():
    s = _shop([_sprod(602, species="Lasius flavus"),
               _sprod(603, title="Poster Lasius niger 70×50")])
    out, _, _ = _species_rounds([(s, [(602, 10, 10), (603, 10, 10)]),
                                 (s, [(602, 5, 5), (603, 5, 5)])])
    assert out == [[], []]


def test_genus_watch_matches_species_of_genus():
    s = _shop([_sprod(604, species="Lasius flavus"), _sprod(605, species="Messor barbarus")])
    out, _, _ = _species_rounds([(s, [(604, 10, 10), (605, 10, 10)]),
                                 (s, [(604, 12, 12), (605, 12, 12)])], species="lasius", genus=True)
    assert len(out[1]) == 1 and "Lasius flavus" in out[1][0]


def test_species_variants_changed_new_and_removed_digest():
    v1 = [_var(1, "Königin", 20.0), _var(2, "Königin + 10", 30.0)]
    v2 = [_var(1, "Königin", 18.0), _var(3, "Königin + 50", 45.0)]       # 1 billiger, 3 neu, 2 weg
    out, pending, digest = _species_rounds([(_shop([_sprod(606, variants=v1)]), []),
                                            (_shop([_sprod(606, variants=v1)]), []),
                                            (_shop([_sprod(606, variants=v2)]), [])])
    assert out[0] == [] and out[1] == []
    msg = out[2][0]
    assert "Price change" in msg and "20.00" in msg and "18.00" in msg
    assert "📉 Königin" in msg and "new variant: Königin + 50" in msg
    assert "Königin + 10" not in msg                     # entfallen → nicht sofort
    assert pending == ["Königin + 10"]
    assert len(digest) == 1 and "Variants no longer available" in digest[0] and "Königin + 10" in digest[0]


def test_species_variant_reappears_cancels_pending_removed():
    v1 = [_var(1, "Königin", 20.0), _var(2, "Königin + 10", 30.0)]
    v2 = [_var(1, "Königin", 20.0)]
    out, pending, digest = _species_rounds([(_shop([_sprod(607, variants=v1)]), []),
                                            (_shop([_sprod(607, variants=v2)]), []),
                                            (_shop([_sprod(607, variants=v1)]), [])])
    assert out[1] == []                                  # nur entfallen → keine Sofort-DM
    assert pending == [] and digest == []                # wieder da → Meldung verworfen
    assert "new variant: Königin + 10" in out[2][0]


def test_species_quiet_hours_skip(monkeypatch):
    import cogs.price_tracking as PT
    from utils.db import execute_db, init_db
    monkeypatch.setattr(PT, "is_quiet", lambda uid: True)
    called = []
    monkeypatch.setattr(PT.PriceTrackingCog, "_process_species_watch",
                        lambda self, w, s: called.append(w))

    async def go():
        bot = DMBot(asyncio.get_running_loop())
        await init_db(bot)
        await execute_db(bot, "INSERT OR REPLACE INTO user_species_watch (user_id, species) VALUES (?,?)",
                         (UID, "lasius niger"), commit=True)
        cog = PT.PriceTrackingCog.__new__(PT.PriceTrackingCog)
        cog.bot = bot
        await cog.check_species_watches()
        await execute_db(bot, "DELETE FROM user_species_watch WHERE user_id=?", (UID,), commit=True)
    asyncio.run(go())
    assert called == []
