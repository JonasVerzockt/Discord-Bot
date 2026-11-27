# SPDX-License-Identifier: AGPL-3.0-or-later
"""
iNaturalist-Abgleich (utils/inat.py, cogs/inat_tracker.py), iNat-API durch feste Antworten ersetzt:
  • Konto verknüpfen: Code im Profil, unbekannt, Code fehlt, schon vergeben, abgelaufen, trennen
  • Beobachtungs-Links im Kanal: zählt nur eigene Ameisen-Beobachtungen (✅), 🆕/🌟 für neue Art,
    ❌ fremd, 🔗 nicht verknüpft (+ einmaliger Hinweis), ⏳ API nicht erreichbar, Doppel-Post
  • Auswertung: Ausstech-Regel (Gattung zählt nur ohne Art darunter), Rangliste, Jahr,
    gelöschter Post zählt nicht mehr
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import board_snapshot as BS
import cogs.inat_tracker as T
from utils import inat as I

ME = 830000000000000001
OTHER = 830000000000000002
ANT, LASIUS, NIGER, FLAVUS, BEE = I.FORMICOIDEA, 67, 6701, 6702, 999

OBS = {   # obs_id → (inat_user_id, taxon_id, name, rank, ancestors, observed_on)
    101: (5001, NIGER, "Lasius niger", "species", [ANT, LASIUS], "2026-06-01"),
    102: (5001, LASIUS, "Lasius", "genus", [ANT], "2026-06-02"),
    103: (5001, FLAVUS, "Lasius flavus", "species", [ANT, LASIUS], "2025-07-01"),
    104: (5002, NIGER, "Lasius niger", "species", [ANT, LASIUS], "2026-06-03"),      # fremdes Konto
    105: (5001, BEE, "Apis mellifera", "species", [1, 2], "2026-06-04"),             # keine Ameise
    201: (5002, NIGER, "Lasius niger", "species", [ANT, LASIUS], "2026-06-05"),
}


def _link(*ids):
    return " ".join(f"https://www.inaturalist.org/observations/{i}" for i in ids)


class FakeAPI:
    def __init__(self):
        self.down = False
        self.users = {"jonas": {"id": 5001, "login": "Jonas", "bio": ""},
                      "fremd": {"id": 5002, "login": "fremd", "bio": ""}}

    async def get(self, path, params, base=I.API):
        if self.down:
            return None
        if path == "/observations":
            ids = [int(x) for x in params["id"].split(",")]
            res = []
            for i in ids:
                if i in OBS:
                    u, tid, nm, rk, anc, day = OBS[i]
                    res.append({"id": i, "observed_on": day, "user": {"id": u, "login": "x"},
                                "taxon": {"id": tid, "name": nm, "rank": rk, "ancestor_ids": anc}})
            return {"results": res}
        if path == "/users/autocomplete":
            u = self.users.get(params["q"].lower())
            return {"results": [{"id": u["id"], "login": u["login"]}] if u else []}
        if path.startswith("/users/"):
            uid = int(path.rsplit("/", 1)[1])
            u = next(v for v in self.users.values() if v["id"] == uid)
            return {"results": [{"description": u["bio"]}]}
        raise AssertionError(path)


class FakeMsg:
    _n = 9400000000000000

    def __init__(self, content, author=ME):
        FakeMsg._n += 1
        self.id = FakeMsg._n
        self.content = content
        self.author = SimpleNamespace(id=author, bot=False)
        self.channel = SimpleNamespace(id=T.INAT_CHANNEL_ID)
        self.guild = None
        self.reactions, self.replies = [], []

    async def add_reaction(self, e):
        self.reactions.append(e)

    async def reply(self, text, **kw):
        self.replies.append(text)


@pytest.fixture()
def it(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(I, "_get", api.get)

    def run(coro_fn):
        async def go():
            from utils.db import init_db, execute_db
            bot = BS.FakeBot(asyncio.get_running_loop())
            await init_db(bot)
            for t in ("inat_accounts", "inat_link_codes", "inat_obs"):
                await execute_db(bot, f"DELETE FROM {t}", commit=True)
            cog = T.InatTrackerCog.__new__(T.InatTrackerCog)                    # ohne Timer-Start
            cog.bot, cog._hinted, cog._last_manual = bot, set(), None
            return await coro_fn(cog, bot, execute_db)
        return asyncio.run(go())
    return SimpleNamespace(run=run, api=api)


async def _linked(bot, discord_id, inat_id, login):
    from utils.db import execute_db
    await execute_db(bot, "INSERT INTO inat_accounts (discord_id, inat_user_id, inat_login, verified_at) "
                          "VALUES (?,?,?,datetime('now'))", (str(discord_id), inat_id, login), commit=True)


def test_link_flow(it):
    async def c(cog, bot, db):
        res = [await I.verify_link(bot, ME)]                                   # ohne Code
        code = await I.start_link(bot, ME, "Jonas")
        res.append(await I.verify_link(bot, ME))                               # Code fehlt im Profil
        it.api.users["jonas"]["bio"] = f"Ameisenfreund · {code.lower()}"
        await _linked(bot, OTHER, 5001, "jonas")                               # schon von jemand anderem
        res.append(await I.verify_link(bot, ME))
        await I.unlink(bot, OTHER)
        res.append(await I.verify_link(bot, ME))
        acc = await I.account_of(bot, ME)
        pend = await I.pending_link(bot, ME)
        await I.start_link(bot, ME, "gibtsnicht")
        res.append(await I.verify_link(bot, ME))
        return [r[0] for r in res], acc, pend, code
    statuses, acc, pend, code = it.run(c)
    assert statuses == ["no_pending", "code_missing", "taken", "ok", "unknown_user"]
    assert acc == {"inat_user_id": 5001, "inat_login": "jonas"} and pend is None
    assert code.startswith("AAM-") and len(code) == 10


def test_link_code_expires_and_api_down(it):
    async def c(cog, bot, db):
        await I.start_link(bot, ME, "Jonas")
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        await db(bot, "UPDATE inat_link_codes SET created_at=?", (old,), commit=True)
        expired = (await I.verify_link(bot, ME))[0]
        await I.start_link(bot, ME, "Jonas")
        it.api.down = True
        down = (await I.verify_link(bot, ME))[0]
        return expired, down
    assert it.run(c) == ("no_pending", "api")


def test_posts_reactions(it):
    async def c(cog, bot, db):
        await _linked(bot, ME, 5001, "jonas")
        await _linked(bot, OTHER, 5002, "fremd")
        a = FakeMsg(_link(101))                                                # erste Art überhaupt
        b = FakeMsg(_link(201), author=OTHER)                                  # dieselbe Art, andere Person
        c_ = FakeMsg(_link(103, 104, 105))                                     # neu für mich, fremd, keine Ameise
        d = FakeMsg(_link(101))                                                # Doppel-Post
        for m in (a, b, c_, d):
            await cog.on_message(m)
        return a, b, c_, d
    a, b, c_, d = it.run(c)
    assert a.reactions == ["✅", "🌟"]                                        # neu für die Community
    assert b.reactions == ["✅", "🆕"]                                        # neu für die Person
    assert c_.reactions == ["✅", "🌟", "❌"]
    assert d.reactions == []


def test_unlinked_hint_once_and_api_down(it):
    async def c(cog, bot, db):
        a, b = FakeMsg(_link(101)), FakeMsg(_link(103))
        await cog.on_message(a)
        await cog.on_message(b)
        it.api.down = True
        p = FakeMsg(_link(102))
        await cog.on_message(p)
        return a, b, p
    a, b, p = it.run(c)
    assert a.reactions == ["🔗"] and len(a.replies) == 1
    assert b.reactions == ["🔗"] and b.replies == []                           # Hinweis nur einmal
    assert p.reactions == ["⏳"]


def test_counting_rule_ranking_and_delete(it):
    async def c(cog, bot, db):
        await _linked(bot, ME, 5001, "jonas")
        await _linked(bot, OTHER, 5002, "fremd")
        m1 = FakeMsg(_link(101, 102, 103))
        await cog.on_message(m1)
        await cog.on_message(FakeMsg(_link(201), author=OTHER))
        me_all = await I.user_stats(bot, ME)
        me_2026 = await I.user_stats(bot, ME, 2026)
        rank = await I.ranking(bot)
        await cog.on_raw_message_delete(SimpleNamespace(channel_id=T.INAT_CHANNEL_ID, message_id=m1.id))
        after = await I.user_stats(bot, ME)
        return me_all, me_2026, rank, after
    me_all, me_2026, rank, after = it.run(c)
    assert me_all == {"obs": 3, "species": 2, "taxa": ["Lasius flavus", "Lasius niger"]}   # Gattung ausgestochen
    assert me_2026 == {"obs": 2, "species": 1, "taxa": ["Lasius niger"]}                   # Jahr 2026: nur niger
    assert [(r["discord_id"], r["species"], r["obs"]) for r in rank] == [(str(ME), 2, 3), (str(OTHER), 1, 1)]
    assert after["obs"] == 0


def test_genus_counts_when_alone():
    rows = [{"taxon_id": LASIUS, "taxon_rank": "genus", "taxon_name": "Lasius", "ancestors": "[1]"},
            {"taxon_id": 77, "taxon_rank": "genus", "taxon_name": "Messor", "ancestors": "[1]"},
            {"taxon_id": NIGER, "taxon_rank": "species", "taxon_name": "Lasius niger", "ancestors": f"[1, {LASIUS}]"}]
    assert sorted(r["taxon_name"] for r in I._taxa(rows)) == ["Lasius niger", "Messor"]


def test_ranking_trigger_cooldown(it, monkeypatch):
    posted = []

    async def fake_post(channel, year, key):
        posted.append(year)
        return True

    async def c(cog, bot, db):
        monkeypatch.setattr(cog, "_post_ranking", fake_post)
        a, b = FakeMsg("Rangliste"), FakeMsg("statistik")
        await cog.on_message(a)
        await cog.on_message(b)
        return a, b
    a, b = it.run(c)
    assert a.reactions == ["📊"] and b.reactions == ["⏱️"] and len(posted) == 1


def test_obs_link_regex():
    assert [int(x) for x in I.OBS_RE.findall(
        "schau: https://inaturalist.org/observations/123 und http://www.iNaturalist.org/observations/456?x=1")] == [123, 456]
    assert I.LOGIN_RE.match("jonas_ants-1") and not I.LOGIN_RE.match("a b")
