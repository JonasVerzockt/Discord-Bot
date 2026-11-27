# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Angebots-Alarme (cogs/offer_alerts.py):
  • Text-Erkennung: Schlagwort als Wort, Synonyme, „verkauft“ pro Zeile/Nachricht,
    Durchstreichung, Begrüßungs-/Kopfzeilen, Nachtruhe-Fenster
  • Scanner: Erstlauf setzt nur den Cursor, Puffer-Zeit, Bot-Nachrichten, Dedup,
    :sold:-Reaktion nur vom Anbieter, Admin-⏸️ und Nachtruhe zurückstellen und
    nachliefern, persönliche Ruhezeit (Warteschlange), ✅/🔄 auf die PN
  • Rückblick (Backfill) für ein neues Schlagwort

Discord ist durch Attrappen ersetzt.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

import board_snapshot as BS
import cogs.offer_alerts as OA

CH = 600600600
UID = "870000000000000001"
SELLER = 870000000000000099
SOLD = OA.OFFER_SOLD_EMOTE_ID


# ── reine Text-Helfer ─────────────────────────────────────────────────────────

def test_keyword_is_whole_word_and_ignores_markdown():
    assert OA._kw_in(OA._plain("Biete **Lasius NIGER** Kolonie"), "lasius niger")
    assert not OA._kw_in(OA._plain("Messor barbaruss"), "messor barbarus")
    assert not OA._kw_in("", "x") and not OA._kw_in("abc", "")


@pytest.mark.parametrize("line,sold", [
    ("Lasius niger 20€ verkauft", True),
    ("Lasius niger – SOLD", True),
    ("Camponotus reserviert", True),
    (f"Messor <:sold:{SOLD}>", True),
    ("~~1x Lasius niger~~ 20€", True),
    ("~~1x Lasius niger~~ noch Messor da", False),
    ("Lasius niger ist weg vom Fenster? nein, noch da", False),     # „weg“ allein ist zu mehrdeutig
    ("Lasius niger 20€", False),
])
def test_line_sold(line, sold):
    assert OA._line_sold(line) is sold


@pytest.mark.parametrize("content,whole", [
    ("Biete Lasius niger\nalles verkauft", True),
    ("Biete Lasius niger\nsind weg", True),
    ("~~Biete Lasius niger\nMessor barbarus 30€~~", True),
    ("Biete Lasius niger\n~~Messor~~", False),
])
def test_message_sold_whole(content, whole):
    assert OA._message_sold_whole(content, False) is whole
    assert OA._message_sold_whole("irgendwas", True) is True        # gültige :sold:-Reaktion


def test_open_hits_per_line():
    content = "Hallo zusammen, biete:\n~~Lasius niger 20€~~\nMessor barbarus 30€\nCamponotus verkauft"
    hits = OA._open_hits(content, False, {"lasius niger", "messor barbarus", "camponotus"})
    assert hits == {"messor barbarus": "Messor barbarus 30€"}


def test_open_hits_synonym(monkeypatch):
    OA._expand_terms.cache_clear()
    from utils import species_catalog
    monkeypatch.setattr(species_catalog, "canonical",
                        lambda s: "Camponotus ligniperda" if s == "camponotus ligniperdus" else None)
    try:
        hits = OA._open_hits("Biete Camponotus ligniperda Königin", False, {"camponotus ligniperdus"})
        assert "camponotus ligniperdus" in hits
    finally:
        OA._expand_terms.cache_clear()


def test_first_content_line_skips_greeting():
    assert OA._first_content_line("Hallo Leute!\n## Biete:\nLasius niger Kolonie 20€") == "Lasius niger Kolonie 20€"
    assert OA._first_content_line("Hallo!") == "Hallo!"
    assert OA._is_filler_line("Moin zusammen, verkaufe folgendes:")
    assert not OA._is_filler_line("Lasius niger Kolonie")


def test_quiet_window(monkeypatch):
    monkeypatch.setattr(OA, "OFFER_QUIET_START_HOUR", 23)
    monkeypatch.setattr(OA, "OFFER_QUIET_END_HOUR", 9)
    at = lambda h: datetime(2026, 11, 20, h, 30, tzinfo=OA.BERLIN)
    assert OA._in_quiet_hours(at(23)) and OA._in_quiet_hours(at(3)) and not OA._in_quiet_hours(at(9))
    assert OA._current_quiet_start(at(3)) == datetime(2026, 11, 19, 23, 0, tzinfo=OA.BERLIN)
    assert OA._current_quiet_start(at(12)) is None
    monkeypatch.setattr(OA, "OFFER_QUIET_END_HOUR", 23)
    assert not OA._in_quiet_hours(at(23))                              # Start = Ende → aus


# ── Scanner mit Attrappen ─────────────────────────────────────────────────────

class FakeDM:
    _n = 9100000000000000

    def __init__(self, embed):
        FakeDM._n += 1
        self.id = FakeDM._n
        self.embed = embed
        self.reactions = []

    async def add_reaction(self, e):
        self.reactions.append(e)


class FakeUser:
    def __init__(self, uid):
        self.id = int(uid)
        self.dms: list[FakeDM] = []
        self.texts: list[str] = []

    async def send(self, content=None, embed=None):
        if embed is None:
            self.texts.append(content)
            return None
        dm = FakeDM(embed)
        self.dms.append(dm)
        return dm


class FakeReaction:
    def __init__(self, emoji, users):
        self.emoji = emoji
        self._users = users

    def users(self):
        async def gen():
            for u in self._users:
                yield u
        return gen()


class FakeMsg:
    _n = 9000000000000000

    def __init__(self, content, *, age_min=120, author=SELLER, bot=False, reactions=None):
        FakeMsg._n += 1
        self.id = FakeMsg._n
        self.content = content
        self.created_at = discord.utils.utcnow() - timedelta(minutes=age_min)
        self.author = SimpleNamespace(id=author, bot=bot)
        self.webhook_id = None
        self.reactions = reactions or []
        self.guild = None
        self.jump_url = f"https://discord.com/channels/1/{CH}/{self.id}"


class FakeChannel:
    def __init__(self):
        self.id = CH
        self.msgs: dict[int, FakeMsg] = {}

    def add(self, *ms):
        for m in ms:
            self.msgs[m.id] = m

    def history(self, limit=None, after=None, before=None, oldest_first=None):
        async def gen():
            ms = sorted(self.msgs.values(), key=lambda m: m.id, reverse=not oldest_first and limit != 1)
            if limit == 1:
                ms = sorted(self.msgs.values(), key=lambda m: m.id, reverse=True)[:1]
            for m in ms:
                if isinstance(after, discord.Object) and m.id <= after.id:
                    continue
                if isinstance(after, datetime) and m.created_at <= after:
                    continue
                if isinstance(before, datetime) and m.created_at >= before:
                    continue
                yield m
        return gen()

    async def fetch_message(self, mid):
        if mid not in self.msgs:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "weg")
        return self.msgs[mid]


class OfferBot(BS.FakeBot):
    def __init__(self, loop, channel):
        super().__init__(loop)
        self.user = SimpleNamespace(id=1)
        self.channel = channel
        self.users: dict[int, FakeUser] = {}

    def get_channel(self, cid):
        return self.channel if int(cid) == CH else None

    def get_user(self, uid):
        return self.users.setdefault(int(uid), FakeUser(uid))

    async def fetch_user(self, uid):
        return self.get_user(uid)


@pytest.fixture()
def oa(monkeypatch):
    monkeypatch.setattr(OA, "OFFER_CHANNEL_ID", CH)
    monkeypatch.setattr(OA, "_in_quiet_hours", lambda dt: False)       # Nachtruhe je Test gezielt an
    quiet = set()
    monkeypatch.setattr(OA, "is_quiet", lambda uid: str(uid) in quiet)
    env = SimpleNamespace(quiet=quiet, ch=FakeChannel())

    def run(coro_fn):
        async def go():
            from utils.db import init_db, execute_db
            bot = OfferBot(asyncio.get_running_loop(), env.ch)
            await init_db(bot)
            for t in ("offer_keywords", "offer_alert_seen", "offer_cursor", "offer_alert_msgs",
                      "offer_deferred", "quiet_offer_queue"):
                await execute_db(bot, f"DELETE FROM {t}", commit=True)
            cog = OA.OfferAlertsCog.__new__(OA.OfferAlertsCog)            # ohne Timer-Start
            cog.bot = bot
            env.bot, env.cog = bot, cog
            return await coro_fn(cog, bot)
        return asyncio.run(go())
    env.run = run
    return env


async def _kw(bot, *kws, uid=UID):
    from utils.db import execute_db
    for k in kws:
        await execute_db(bot, "INSERT INTO offer_keywords (user_id, keyword, keyword_raw) VALUES (?,?,?)",
                         (uid, k.lower(), k), commit=True)


async def _start(cog, ch):
    """Erstlauf: Cursor auf die letzte vorhandene Nachricht setzen."""
    ch.add(FakeMsg("alte Nachricht", age_min=600))
    await cog.scan_offers()


def _user(env, uid=UID):
    return env.bot.get_user(int(uid))


def test_first_scan_only_sets_cursor(oa):
    async def c(cog, bot):
        await _kw(bot, "Lasius niger")
        oa.ch.add(FakeMsg("Biete Lasius niger", age_min=600))
        await cog.scan_offers()
        return await cog._get_cursor()
    cursor = oa.run(c)
    assert cursor is not None
    assert _user(oa).dms == []


def test_new_offer_alert_dm_and_dedup(oa):
    async def c(cog, bot):
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        m = FakeMsg("Hallo, biete:\nLasius niger Kolonie 20€")
        oa.ch.add(m)
        await cog.scan_offers()
        await cog._forward_alert(UID, "lasius niger", "Lasius niger", m, "x")     # zweiter Versuch → Dedup
        return m
    m = oa.run(c)
    dms = _user(oa).dms
    assert len(dms) == 1
    e = dms[0].embed
    assert "Lasius niger" in e.title and "Lasius niger Kolonie 20€" in e.description and m.jump_url in e.description
    assert dms[0].reactions == ["✅", "🔄"]


def test_offer_inside_buffer_waits(oa):
    async def c(cog, bot):
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        oa.ch.add(FakeMsg("Biete Lasius niger", age_min=5))                      # < 60 min Puffer
        await cog.scan_offers()
    oa.run(c)
    assert _user(oa).dms == []


@pytest.mark.parametrize("msg", [
    lambda: FakeMsg("Biete Lasius niger", bot=True),
    lambda: FakeMsg("Biete Lasius niger – verkauft"),
    lambda: FakeMsg("Biete Messor barbarus"),
    lambda: FakeMsg("Biete Lasius niger",
                    reactions=[FakeReaction(SimpleNamespace(id=SOLD), [SimpleNamespace(id=SELLER)])]),
])
def test_no_alert_cases(oa, msg):
    async def c(cog, bot):
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        oa.ch.add(msg())
        await cog.scan_offers()
    oa.run(c)
    assert _user(oa).dms == []


def test_sold_reaction_from_stranger_is_ignored(oa):
    async def c(cog, bot):
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        oa.ch.add(FakeMsg("Biete Lasius niger",
                          reactions=[FakeReaction(SimpleNamespace(id=SOLD), [SimpleNamespace(id=42)])]))
        await cog.scan_offers()
    oa.run(c)
    assert len(_user(oa).dms) == 1


def test_night_quiet_defers_then_releases(oa, monkeypatch):
    async def c(cog, bot):
        from utils.db import execute_db
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        oa.ch.add(FakeMsg("Biete Lasius niger"))
        monkeypatch.setattr(OA, "_in_quiet_hours", lambda dt: True)
        await cog.scan_offers()
        deferred = await execute_db(bot, "SELECT reason FROM offer_deferred", fetch=True)
        n_night = len(_user(oa).dms)
        monkeypatch.setattr(OA, "_in_quiet_hours", lambda dt: False)
        await cog.scan_offers()
        left = await execute_db(bot, "SELECT 1 FROM offer_deferred", fetch=True)
        return [d["reason"] for d in deferred], n_night, left
    reasons, n_night, left = oa.run(c)
    assert reasons == ["quiet"] and n_night == 0
    assert len(_user(oa).dms) == 1 and left == []


def test_deferred_message_deleted_is_dropped(oa, monkeypatch):
    async def c(cog, bot):
        from utils.db import execute_db
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        m = FakeMsg("Biete Lasius niger")
        oa.ch.add(m)
        monkeypatch.setattr(OA, "_in_quiet_hours", lambda dt: True)
        await cog.scan_offers()
        del oa.ch.msgs[m.id]
        monkeypatch.setattr(OA, "_in_quiet_hours", lambda dt: False)
        await cog.scan_offers()
        return await execute_db(bot, "SELECT 1 FROM offer_deferred", fetch=True)
    assert oa.run(c) == []
    assert _user(oa).dms == []


def test_admin_pause_defers(oa, monkeypatch):
    async def c(cog, bot):
        from utils.db import execute_db
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        oa.ch.add(FakeMsg("Biete Lasius niger", reactions=[FakeReaction("⏸️", [SimpleNamespace(id=5)])]))
        monkeypatch.setattr(cog, "_paused_by_admin", lambda m: _true())
        await cog.scan_offers()
        return await execute_db(bot, "SELECT reason FROM offer_deferred", fetch=True)
    assert [r["reason"] for r in oa.run(c)] == ["pause"]
    assert _user(oa).dms == []


async def _true():
    return True


def test_personal_quiet_queues_then_delivers(oa):
    async def c(cog, bot):
        from utils.db import execute_db
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger")
        oa.ch.add(FakeMsg("Biete Lasius niger"))
        oa.quiet.add(UID)
        await cog.scan_offers()
        queued = await execute_db(bot, "SELECT keyword FROM quiet_offer_queue", fetch=True)
        n_quiet = len(_user(oa).dms)
        oa.quiet.clear()
        await cog.scan_offers()
        left = await execute_db(bot, "SELECT 1 FROM quiet_offer_queue", fetch=True)
        return [q["keyword"] for q in queued], n_quiet, left
    queued, n_quiet, left = oa.run(c)
    assert queued == ["lasius niger"] and n_quiet == 0
    assert len(_user(oa).dms) == 1 and left == []


def test_quiet_queue_skips_sold_and_removed_keyword(oa):
    async def c(cog, bot):
        from utils.db import execute_db
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger", "Messor barbarus")
        a = FakeMsg("Biete Lasius niger")
        b = FakeMsg("Biete Messor barbarus")
        oa.ch.add(a, b)
        oa.quiet.add(UID)
        await cog.scan_offers()
        a.content = "Biete Lasius niger – verkauft"                                # inzwischen verkauft
        await execute_db(bot, "DELETE FROM offer_keywords WHERE keyword='messor barbarus'", commit=True)
        oa.quiet.clear()
        await cog.scan_offers()
        return await execute_db(bot, "SELECT 1 FROM quiet_offer_queue", fetch=True)
    assert oa.run(c) == []
    assert _user(oa).dms == []


def test_done_reaction_removes_keyword_keep_keeps(oa):
    async def c(cog, bot):
        from utils.db import execute_db
        await _start(cog, oa.ch)
        await _kw(bot, "Lasius niger", "Messor barbarus")
        oa.ch.add(FakeMsg("Biete Lasius niger"), FakeMsg("Biete Messor barbarus"))
        await cog.scan_offers()
        d1, d2 = _user(oa).dms
        kw_of = {d.id: d.embed.title for d in (d1, d2)}
        done = d1 if "Lasius" in kw_of[d1.id] else d2
        keep = d2 if done is d1 else d1
        ev = lambda dm, e, who=int(UID): SimpleNamespace(user_id=who, message_id=dm.id, emoji=e)
        await cog.on_raw_reaction_add(ev(done, "✅", who=123))                    # fremde Person → nichts
        await cog.on_raw_reaction_add(ev(done, "✅"))
        await cog.on_raw_reaction_add(ev(keep, "🔄"))
        return await execute_db(bot, "SELECT keyword FROM offer_keywords WHERE user_id=?", (UID,), fetch=True)
    left = oa.run(c)
    assert [r["keyword"] for r in left] == ["messor barbarus"]
    texts = _user(oa).texts
    assert any(t.startswith("✅") for t in texts) and any(t.startswith("🔄") for t in texts)


def test_backfill_lists_open_offers(oa):
    async def c(cog, bot):
        oa.ch.add(FakeMsg("Biete Lasius niger 20€", age_min=60 * 24 * 3),
                  FakeMsg("Lasius niger verkauft", age_min=60 * 24 * 2),
                  FakeMsg("Biete Lasius niger 15€", age_min=60 * 24 * 200),        # älter als Rückblick
                  FakeMsg("Biete Lasius niger 10€", age_min=10))                   # noch im Puffer
        return await cog._backfill(_user(oa), UID, "lasius niger", "Lasius niger")
    n = oa.run(c)
    assert n == 1
    d = _user(oa).dms[0].embed
    assert "Bestehende Angebote" in d.title and "20€" in d.description and "15€" not in d.description


def test_long_alert_is_split(oa):
    async def c(cog, bot):
        lines = [f"• Zeile {i} " + "x" * 100 for i in range(80)]
        ok = await cog._send_alert_dm(_user(oa), UID, "k", "Titel", "Intro", lines)
        from utils.db import execute_db
        maps = await execute_db(bot, "SELECT message_id FROM offer_alert_msgs", fetch=True)
        return ok, maps
    ok, maps = oa.run(c)
    dms = _user(oa).dms
    assert ok and len(dms) >= 3
    assert dms[0].embed.title == "Titel" and dms[-1].reactions == ["✅", "🔄"] and dms[0].reactions == []
    assert [m["message_id"] for m in maps] == [str(dms[-1].id)]                   # nur letzte PN zählt
