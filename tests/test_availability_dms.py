# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verfügbarkeits-DMs (cogs/notifications.py, trigger_availability_check):
  • lagernde, noch nicht gesehene Produkte → eine DM mit Shop, Preis, Varianten
  • bereits gesehene → keine zweite DM; ausverkauft → „gesehen“ nur für DIESE Art zurück
  • Rückfrage danach: 👍 schließt, 🔄 sucht weiter, keine Antwort → abgelaufen
  • DMs gesperrt → Hinweis im Server-Kanal

Discord ist durch Attrappen ersetzt, Shopdaten kommen aus einer Test-Datei.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import board_snapshot as BS

UID = "860000000000000001"
SERVER = 434343
CHANNEL = 525252


def _p(pid, species, stock=True, price=20, variants=None):
    return {"id": pid, "species": species, "title": f"{species} Kolonie", "min_price": price, "max_price": price,
            "currency_iso": "EUR", "in_stock": stock, "is_active": True,
            "antcheck_url": f"https://antcheck.example/p/{pid}", "variants": variants or []}


def _shops(lasius_stock=True):
    return {"_meta": {"fetched_at": "2026-11-27T08:00:00+00:00"},
            "1": {"id": 1, "name": "ShopDE", "country": "de", "url": "https://shop-de.example", "products": [
                _p(11, "Lasius niger", stock=lasius_stock,
                   variants=[{"id": 1, "title": "Königin + 5", "price": 18, "in_stock": True, "is_active": True},
                             {"id": 2, "title": "Königin + 20", "price": 30, "in_stock": False, "is_active": True}]),
                _p(12, "Messor barbarus")]},
            "2": {"id": 2, "name": "ShopAT", "country": "at", "url": "https://shop-at.example", "products": [
                _p(21, "Lasius niger", price=25)]}}


class FakeQ:
    def __init__(self, text):
        self.id = 7700
        self.text = text
        self.reactions = []

    async def add_reaction(self, e):
        self.reactions.append(e)


class FakeUser:
    def __init__(self, blocked=False):
        self.id = int(UID)
        self.blocked = blocked
        self.embeds, self.texts, self.views = [], [], []

    async def send(self, content=None, embed=None, view=None):
        if self.blocked:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "DMs gesperrt")
        if embed is not None:
            if embed.description.startswith(("🏆", "🎉")):          # Erfolge/Rang (check_and_grant)
                return None
            self.embeds.append(embed.description)
            self.views.append(view)
            return None
        self.texts.append(content)
        return FakeQ(content)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


class NBot(BS.FakeBot):
    def __init__(self, loop, user, answer):
        super().__init__(loop)
        self.u = user
        self.answer = answer            # "👍" | "🔄" | None (Zeitüberschreitung)
        self.channel = FakeChannel()

    async def fetch_user(self, uid):
        return self.u

    def get_channel(self, cid):
        return self.channel if int(cid) == CHANNEL else None

    async def wait_for(self, event, timeout=None, check=None):
        if self.answer is None:
            raise asyncio.TimeoutError
        reaction = SimpleNamespace(emoji=self.answer, message=SimpleNamespace(id=7700))
        assert check(reaction, SimpleNamespace(id=int(UID)))
        assert not check(reaction, SimpleNamespace(id=1))            # fremde Reaktion zählt nicht
        return reaction, None


@pytest.fixture()
def nt():
    f = Path(os.environ["SHOPS_DATA_FILE"])
    original = f.read_text(encoding="utf-8")

    def run(*, regions="de", species="Lasius niger", answer="👍", blocked=False, seen=(), stock=True,
            fallback=False, rounds=1):
        f.write_text(json.dumps(_shops(lasius_stock=stock)), encoding="utf-8")

        async def go():
            from utils.db import init_db, execute_db
            from cogs.notifications import NotificationsCog
            user = FakeUser(blocked)
            bot = NBot(asyncio.get_running_loop(), user, answer)
            await init_db(bot)
            for t in ("notifications", "user_seen_products", "server_user_mappings", "user_shop_blacklist"):
                await execute_db(bot, f"DELETE FROM {t} WHERE user_id=?", (UID,), commit=True)
            await execute_db(bot, "DELETE FROM server_settings WHERE server_id=?", (SERVER,), commit=True)
            await execute_db(bot, "INSERT INTO notifications (user_id, species, regions, server_id) VALUES (?,?,?,?)",
                             (UID, species, regions, SERVER), commit=True)
            for pid in seen:
                await execute_db(bot, "INSERT INTO user_seen_products (user_id, product_id) VALUES (?,?)",
                                 (UID, pid), commit=True)
            if fallback:
                await execute_db(bot, "INSERT INTO server_user_mappings (user_id, server_id) VALUES (?,?)",
                                 (UID, SERVER), commit=True)
                await execute_db(bot, "INSERT INTO server_settings (server_id, channel_id) VALUES (?,?)",
                                 (SERVER, CHANNEL), commit=True)
            cog = NotificationsCog(bot)
            results = [await cog.trigger_availability_check(UID, species, regions) for _ in range(rounds)]
            st = await execute_db(bot, "SELECT status FROM notifications WHERE user_id=?", (UID,), fetch=True)
            sn = await execute_db(bot, "SELECT product_id FROM user_seen_products WHERE user_id=? ORDER BY product_id",
                                  (UID,), fetch=True)
            return SimpleNamespace(res=results, user=user, status=st[0]["status"],
                                   seen=[r["product_id"] for r in sn], channel=bot.channel.sent)
        return asyncio.run(go())
    yield run
    f.write_text(original, encoding="utf-8")


def test_dm_lists_new_products_with_variants(nt):
    r = nt(regions="de,at", answer="🔄")
    assert r.res == [True]
    assert len(r.user.embeds) == 1
    dm = r.user.embeds[0]
    assert "Availability Alert" in dm and "ShopDE" in dm and "ShopAT" in dm
    assert "antcheck.example/p/11" in dm and "Königin + 5" in dm and "Königin + 20" not in dm   # nur lagernde Varianten
    assert "Messor" not in dm
    assert r.user.views[-1] is not None                                        # „Preis beobachten“-Knopf
    assert r.seen == ["11", "21"]


def test_region_filter(nt):
    r = nt(regions="at")
    assert "ShopAT" in r.user.embeds[0] and "ShopDE" not in r.user.embeds[0]


def test_seen_products_not_sent_again(nt):
    r = nt(seen=("11",), regions="de")
    assert r.res == [False] and r.user.embeds == []


def test_second_round_no_duplicate(nt):
    r = nt(answer="🔄", rounds=2)
    assert r.res == [True, False] and len(r.user.embeds) == 1


def test_sold_out_returns_false_and_keeps_other_species_seen(nt):
    r = nt(stock=False, seen=("11", "12"))
    assert r.res == [False] and r.user.embeds == []
    assert "12" in r.seen                                                       # andere Art unangetastet


def test_back_in_stock_after_sold_out_alerts_again(nt):
    # „gesehen“ wird nur bei einem erfolgreichen Lauf für diese Art bereinigt:
    # 11 gesehen, 21 neu → DM nur mit 21; 11 bleibt gesehen.
    r = nt(regions="de,at", seen=("11",), answer="🔄")
    assert r.res == [True] and "ShopAT" in r.user.embeds[0] and "antcheck.example/p/11" not in r.user.embeds[0]


def test_feedback_thumbs_up_closes(nt):
    r = nt(answer="👍")
    assert r.status == "completed"
    assert any("satisfied" in t for t in r.user.texts) and any("close your search" in t for t in r.user.texts)
    assert r.seen == []                                                         # Art zurückgesetzt


def test_feedback_continue_keeps_active(nt):
    r = nt(answer="🔄")
    assert r.status == "active" and any("keep notifying" in t for t in r.user.texts)
    assert r.seen == ["11"]


def test_feedback_timeout_expires(nt):
    r = nt(answer=None)
    assert r.status == "expired" and any("no feedback received" in t for t in r.user.texts)


def test_blocked_dm_posts_hint_in_server_channel(nt):
    r = nt(blocked=True, fallback=True)
    assert r.res == [None]
    assert len(r.channel) == 1 and f"<@{UID}>" in r.channel[0] and "Lasius niger" in r.channel[0]
    assert r.seen == []                                                         # nichts als gesehen markiert
