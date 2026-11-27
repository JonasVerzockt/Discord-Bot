# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stichprobe Discord-Menüs (utils/price_tracking_views.py):
  • fremde Person kann ein Menü nicht bedienen
  • Auswahl-Optionen: Lager-Symbol, Preis, „ganzes Produkt“ + nur lagernde Varianten
  • Bestätigen legt Tracking an (zweites Mal: „schon beobachtet“), Abbrechen sperrt das Menü
  • Entfernen löscht Produkt und Arten-Beobachtung samt Zusatzdaten
"""
import asyncio
from types import SimpleNamespace


import board_snapshot as BS
import utils.price_tracking_views as V

UID = 820000000000000001


class _Resp:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send_message(self, content=None, **kw):
        self.sent.append((content, kw.get("ephemeral")))

    async def edit_message(self, content=None, view=None, **kw):
        self.edits.append((content, view))


class _Chan:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def _inter(uid=UID):
    return SimpleNamespace(user=SimpleNamespace(id=uid, display_name="Tester"), response=_Resp(), channel=_Chan())


def _prod(pid=301, stock=True, variants=None, **kw):
    return {"id": pid, "species": "Lasius niger", "title": "Lasius niger Kolonie", "min_price": 20, "max_price": 30,
            "currency_iso": "EUR", "in_stock": stock, "is_active": True,
            "antcheck_url": f"https://antcheck.example/{pid}", "variants": variants or [], **kw}


def _run(coro_fn):
    async def go():
        from utils.db import init_db, execute_db
        bot = BS.FakeBot(asyncio.get_running_loop())
        await init_db(bot)
        for t in ("user_price_tracking", "user_species_watch", "user_species_watch_seen",
                  "user_species_watch_variant_seen", "pending_variant_removed"):
            await execute_db(bot, f"DELETE FROM {t} WHERE user_id=?", (str(UID),), commit=True)
        return await coro_fn(bot, execute_db)
    return asyncio.run(go())


def test_foreign_user_is_rejected():
    async def c(bot, db):
        v = V.ConfirmView([_prod()], "1", {"name": "ShopDE"}, "Lasius niger", bot, UID, "en")
        other, me = _inter(uid=42), _inter()
        return await v.interaction_check(other), other, await v.interaction_check(me)
    ok_other, other, ok_me = _run(c)
    assert ok_other is False and ok_me is True
    assert other.response.sent == [("❌ This is not your menu.", True)]


def test_product_options_show_stock_and_price():
    async def c(bot, db):
        item = V._ProductSelectItem([_prod(301), _prod(302, stock=False), _prod(303, _no_price=True),
                                     _prod(304, _from_history=True)], "en")
        return [(o.value, str(o.emoji), o.description) for o in item.options], item.max_values
    opts, mx = _run(c)
    assert [o[1] for o in opts] == ["✅", "❌", "❓", "⏸️"]
    assert "20.00-30.00EUR" in opts[0][2] and mx == 4


def test_variant_options_only_in_stock():
    async def c(bot, db):
        p = _prod(variants=[{"id": 1, "title": "Königin", "price": 20, "in_stock": True, "is_active": True},
                            {"id": 2, "title": "Königin + 10", "price": 30, "in_stock": False, "is_active": True},
                            {"id": 3, "title": "<b>Königin + 50</b>", "price": 45, "in_stock": True, "is_active": True}])
        item = V._VariantSelectItem(p, "en")
        return [(o.value, o.label) for o in item.options]
    opts = _run(c)
    assert [v for v, _ in opts] == ["0", "1", "3"] and opts[2][1] == "Königin + 50"     # HTML entfernt


def test_confirm_saves_and_second_time_already():
    async def c(bot, db):
        res = []
        for _ in range(2):
            v = V.ConfirmView([_prod()], "1", {"name": "ShopDE"}, "Lasius niger", bot, UID, "en")
            i = _inter()
            await v.confirm.callback(i)
            res.append((i.response.edits[0][0], i.channel.sent, all(x.disabled for x in v.children)))
        rows = await db(bot, "SELECT product_id, shop_name, last_notified_min, last_notified_max FROM user_price_tracking "
                             "WHERE user_id=?", (str(UID),), fetch=True)
        return res, [dict(r) for r in rows]
    res, rows = _run(c)
    assert "1** product(s) are now being tracked" in res[0][0] and len(res[0][1]) == 1 and res[0][2]
    assert "1 already tracked" in res[1][0] and res[1][1] == []                 # keine zweite Ansage
    assert rows == [{"product_id": 301, "shop_name": "ShopDE", "last_notified_min": 20.0, "last_notified_max": 30.0}]


def test_cancel_disables_menu():
    async def c(bot, db):
        v = V.ConfirmView([_prod()], "1", {"name": "ShopDE"}, "Lasius niger", bot, UID, "en")
        i = _inter()
        await v.cancel.callback(i)
        n = await db(bot, "SELECT COUNT(*) AS n FROM user_price_tracking WHERE user_id=?", (str(UID),), fetch=True)
        return i.response.edits[0][0], all(x.disabled for x in v.children), n[0]["n"]
    assert _run(c) == ("❌ Cancelled.", True, 0)


def test_untrack_removes_product_and_species_watch():
    async def c(bot, db):
        u = str(UID)
        await db(bot, "INSERT INTO user_price_tracking (user_id, product_id, variant_id, shop_name) VALUES (?,301,0,'S')",
                 (u,), commit=True)
        await db(bot, "INSERT INTO user_price_tracking (user_id, product_id, variant_id, shop_name) VALUES (?,302,7,'S')",
                 (u,), commit=True)
        await db(bot, "INSERT INTO user_species_watch (user_id, species) VALUES (?, 'lasius niger')", (u,), commit=True)
        await db(bot, "INSERT INTO user_species_watch_seen (user_id, watched_species, product_id) VALUES (?, 'lasius niger', 1)",
                 (u,), commit=True)
        rows = [dict(r) for r in await db(bot, "SELECT * FROM user_price_tracking WHERE user_id=?", (u,), fetch=True)]
        v = V.UntrackView(rows, {301: (20, 30, "EUR")}, {}, [{"species": "lasius niger"}], bot, UID, "en")
        values = [o.value for o in v.children[0].options]
        i = _inter()
        await v.on_products_selected(["301:0", "__sw__lasius niger"], i)
        left = {t: (await db(bot, f"SELECT COUNT(*) AS n FROM {t} WHERE user_id=?", (u,), fetch=True))[0]["n"]
                for t in ("user_price_tracking", "user_species_watch", "user_species_watch_seen")}
        return values, i.response.edits[0][0], left
    values, msg, left = _run(c)
    assert values == ["301:0", "302:7", "__sw__lasius niger"]
    assert "1** product(s) removed" in msg
    assert left == {"user_price_tracking": 1, "user_species_watch": 0, "user_species_watch_seen": 0}


# ── Stichprobe Karten-Menüs (utils/map_views.py) ──────────────────────────────

def test_map_tag_select_saves_only_valid_tags():
    import utils.map_views as MV
    from utils import map_tags

    async def c(bot, db):
        sel = MV.TagSelect("de", preselected=[map_tags.TAG_GROUPS[0]["tags"][0][0]])
        first = map_tags.TAG_GROUPS[0]["tags"][0][0]
        assert [o.default for o in sel.options][0] is True and len(sel.options) <= 25
        await db(bot, "DELETE FROM map_entry_tags WHERE user_id=?", (str(UID),), commit=True)
        i = _inter()
        i.client, i.data = bot, {}
        sel._interaction, sel._selected_values = i, [first, "gibtsnicht"]   # wie nach einer Auswahl in Discord
        await sel.callback(i)
        rows = await db(bot, "SELECT tag_code FROM map_entry_tags WHERE user_id=?", (str(UID),), fetch=True)
        saved = [r["tag_code"] for r in rows]
        i2 = _inter()
        i2.client, i2.data = bot, {}
        sel._interaction, sel._selected_values = i2, []
        await sel.callback(i2)
        left = await db(bot, "SELECT 1 FROM map_entry_tags WHERE user_id=?", (str(UID),), fetch=True)
        return first, saved, i.response.edits[0], left, i2.response.edits[0][0]
    first, saved, edit, left, cleared = _run(c)
    assert saved == [first]                                                    # Unbekanntes verworfen
    assert edit[1] is None and edit[0]                                         # Menü geschlossen, Bestätigung
    assert left == [] and cleared


def test_map_display_preview():
    import utils.map_views as MV
    assert MV._display_preview("  Jonas ", "jonas_aam", True, "de") == "Jonas (jonas_aam)"
    assert MV._display_preview("", "jonas_aam", True, "de") == "jonas_aam"
    assert MV._display_preview("Jonas", "jonas_aam", False, "de") != "Jonas (jonas_aam)"   # anonym


def test_event_actions_owner_only(monkeypatch):
    import utils.map_views as MV
    monkeypatch.setattr(MV, "BOARD_OWNER_ID", 7)
    owner, other = _inter(uid=7), _inter(uid=8)
    assert asyncio.run(MV._owner_only(owner)) is True and owner.response.sent == []
    assert asyncio.run(MV._owner_only(other)) is False and other.response.sent[0][1] is True
