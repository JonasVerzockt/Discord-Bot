# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Review-Bot (cogs/reviews.py): Nachricht → KI → Tabellenzeile → Tracking.
  • neue Bewertung: Zeile anhängen, Tracking, Lieferzeit, Verfasser, 🟢
  • keine Bewertung / Bot / anderer Kanal → ignoriert
  • geteilte Bewertung (2 Nachrichten) → EINE Zeile mit zusammengeführtem Text
  • Shop unbekannt / KI-Fehler → 🟡 + Pending
  • Bearbeiten: vorhandene Zeile wird überschrieben, Schutz bei verrutschter Zuordnung
  • 🟡-Klick: erneut verarbeiten (bei getrackter Zeile mit force)
  • Löschen: Zeile geleert, DB bereinigt
  • Abgleich beim Start: Datum + Reihenfolge

Discord, Google Sheets und die KI sind durch Attrappen mit festen Antworten ersetzt.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

import board_snapshot as BS

CH = 777000777
UID = 880000000000000001
HEADER = ["Datum", "Benutzer", "Shop", "Typ", "Produkte", "Geld", "Bewertung", "Positiv", "Negativ"]
_MID = iter(range(8800000000000001, 8800000000999999))
NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)

REVIEW = "🛒 Shop: https://www.myrmeco.de/\nLasius niger Kolonie, schnell geliefert.\nFazit 9/10"


class FakeWS:
    def __init__(self):
        self.calls = []

    def append_row(self, row, **kw):
        self.calls.append(("append", list(row)))

    def update(self, range_name, values, **kw):
        self.calls.append(("update", range_name, values[0]))

    def batch_clear(self, ranges):
        self.calls.append(("clear", ranges[0]))


class FakeMsg:
    def __init__(self, content, *, author=UID, bot=False, channel=CH, created=NOW, mid=None):
        self.id = mid or next(_MID)
        self.content = content
        self.created_at = created
        self.author = SimpleNamespace(id=author, bot=bot)
        self.channel = SimpleNamespace(id=channel)
        self.guild = None
        self.reactions: list[str] = []

    async def add_reaction(self, e):
        if e not in self.reactions:             # wie Discord: dieselbe Reaktion nur einmal
            self.reactions.append(e)

    async def remove_reaction(self, e, who):
        if e in self.reactions:
            self.reactions.remove(e)


class FakeChannel:
    def __init__(self, msgs):
        self.id = CH
        self.msgs = {m.id: m for m in msgs}

    def history(self, limit=None, after=None, oldest_first=True):
        async def gen():
            for m in sorted(self.msgs.values(), key=lambda m: (m.created_at, m.id)):
                if after is None or m.created_at > after:
                    yield m
        return gen()

    async def fetch_message(self, mid):
        return self.msgs[mid]


class _FastAsyncio:
    """asyncio für das Cog, nur sleep ist sofort fertig (Debounce, Rate-Limit-Pause)."""
    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(_s):
        await asyncio.sleep(0)


@pytest.fixture()
def rv(monkeypatch):
    """Cog mit Attrappen. ai: Liste der KI-Aufrufe (content, shop, date)."""
    import cogs.reviews as R
    from utils.sheet import SheetCache

    sh = SheetCache()
    sh._ws = FakeWS()
    sh._rows = [list(HEADER)]
    monkeypatch.setattr(R, "sheet", sh)
    import utils.shop as S
    if hasattr(S, "sheet"):
        monkeypatch.setattr(S, "sheet", sh)              # Fuzzy-Abgleich gegen bekannte Shops
    monkeypatch.setattr(R, "REVIEW_CHANNEL_ID", CH)
    monkeypatch.setattr(R, "asyncio", _FastAsyncio())
    ai_calls = []
    state = {"fail": False}

    def fake_ai(content, shop, date):
        ai_calls.append((content, shop, date))
        if state["fail"]:
            raise ValueError("kein JSON")
        return {"datum": date, "benutzername": "tester", "shop_name": "KI-Name",
                "shop_typ": "Online", "produkte": "Lasius niger", "geld_ausgegeben": "25",
                "bewertung": 9, "positiv": "schnell", "negativ": "", "lieferzeit_tage": 3}
    monkeypatch.setattr(R, "parse_with_ai", fake_ai)

    env = SimpleNamespace(R=R, sheet=sh, ai=ai_calls, state=state)

    def run(coro_fn):
        async def go():
            from utils.db import init_db
            bot = BS.FakeBot(asyncio.get_running_loop())
            bot.user = SimpleNamespace(id=1)
            await init_db(bot)
            from utils.db import execute_db
            for t in ("review_tracking", "review_pending", "review_delivery", "review_authors"):
                await execute_db(bot, f"DELETE FROM {t}", commit=True)   # Test-DB, je Test frisch
            cog = R.ReviewsCog(bot)
            return await coro_fn(cog, bot)
        return asyncio.run(go())
    env.run = run
    return env


async def _db(bot, sql, *args):
    from utils.db import execute_db
    return [dict(r) for r in await execute_db(bot, sql, args, fetch=True)]


async def _send(cog, *msgs):
    for m in msgs:
        await cog.on_message(m)
    await asyncio.gather(*list(cog._acc_tasks.values()), return_exceptions=True)


def test_new_review_appended_and_tracked(rv):
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        await _send(cog, m)
        return (await _db(bot, "SELECT sheet_row FROM review_tracking WHERE message_id=?", str(m.id)),
                await _db(bot, "SELECT shop, days, review_date FROM review_delivery WHERE message_id=?", str(m.id)),
                await _db(bot, "SELECT user_id, shop, rating FROM review_authors WHERE message_id=?", str(m.id)))
    track, deliv, author = rv.run(c)
    row = rv.sheet.rows[1]
    assert row[0] == "20.11.2026" and row[2] == "myrmeco.de"        # Shop aus der Nachricht, nicht von der KI
    assert row[6] == "9"
    assert rv.ai[0][1] == "myrmeco.de"
    assert track == [{"sheet_row": 2}]
    assert deliv == [{"shop": "myrmeco.de", "days": 3, "review_date": "2026-11-20"}]
    assert author == [{"user_id": str(UID), "shop": "myrmeco.de", "rating": 9.0}]
    assert m.reactions == ["🟢"]
    assert rv.sheet._ws.calls[0][0] == "append"


@pytest.mark.parametrize("msg", [
    FakeMsg("Hallo zusammen, wer hat Lasius niger?"),
    FakeMsg(REVIEW, bot=True),
    FakeMsg(REVIEW, channel=123),
])
def test_ignored_messages(rv, msg):
    async def c(cog, bot):
        await _send(cog, msg)
    rv.run(c)
    assert rv.ai == [] and rv.sheet.row_count == 1 and msg.reactions == []


def test_split_review_is_combined(rv):
    a = FakeMsg(REVIEW)
    b = FakeMsg("Nachtrag: Königin war sehr aktiv.")              # sieht allein nicht wie Review aus

    async def c(cog, bot):
        await _send(cog, a, b)
    rv.run(c)
    assert len(rv.ai) == 1 and "Nachtrag" in rv.ai[0][0] and "Fazit" in rv.ai[0][0]
    assert rv.sheet.row_count == 2
    assert a.reactions == ["🟢"] and b.reactions == ["🟢"]


def test_continuation_without_buffer_ignored(rv):
    b = FakeMsg("Nachtrag: Königin war sehr aktiv.")

    async def c(cog, bot):
        await _send(cog, b)
    rv.run(c)
    assert rv.ai == []


def test_unknown_shop_goes_pending(rv):
    m = FakeMsg("🛒 Shop: Xyzzyplugh Qwertz\nFazit 7/10")

    async def c(cog, bot):
        await _send(cog, m)
        return await _db(bot, "SELECT reason, identifier FROM review_pending WHERE message_id=?", str(m.id))
    pend = rv.run(c)
    assert pend and pend[0]["reason"] == "unresolved_shop" and "Xyzzyplugh" in pend[0]["identifier"]
    assert m.reactions == ["🟡"] and rv.sheet.row_count == 1 and rv.ai == []


def test_ai_error_goes_pending(rv):
    rv.state["fail"] = True
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        await _send(cog, m)
        return await _db(bot, "SELECT reason FROM review_pending WHERE message_id=?", str(m.id))
    assert rv.run(c) == [{"reason": "parse_error"}]
    assert m.reactions == ["🟡"] and rv.sheet.row_count == 1


def test_edit_updates_existing_row(rv):
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        await _send(cog, m)
        before = FakeMsg(m.content, mid=m.id)
        m.content = REVIEW.replace("9/10", "6/10")
        await cog.on_message_edit(before, m)
        return await _db(bot, "SELECT COUNT(*) AS n FROM review_tracking WHERE message_id=?", str(m.id))
    assert rv.run(c) == [{"n": 1}]
    assert rv.sheet.row_count == 2                                    # keine zweite Zeile
    assert [c[0] for c in rv.sheet._ws.calls] == ["append", "update"]
    assert rv.sheet._ws.calls[1][1] == "A2:I2"
    assert m.reactions == ["🟢"]


def test_edit_unchanged_text_does_nothing(rv):
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        await _send(cog, m)
        await cog.on_message_edit(FakeMsg(m.content, mid=m.id), m)
    rv.run(c)
    assert len(rv.ai) == 1


def test_edit_protects_foreign_row_then_yellow_forces(rv):
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        await _send(cog, m)
        rv.sheet._rows[1][0] = "01.01.2025"                           # Zeile gehört inzwischen woanders hin
        before = FakeMsg(m.content, mid=m.id)
        m.content = REVIEW.replace("9/10", "6/10")
        await cog.on_message_edit(before, m)
        pend = await _db(bot, "SELECT reason FROM review_pending WHERE message_id=?", str(m.id))
        calls_after_edit = [x[0] for x in rv.sheet._ws.calls]

        bot.get_channel = lambda cid: FakeChannel([m]) if cid == CH else None
        await cog.on_raw_reaction_add(SimpleNamespace(emoji="🟡", user_id=UID, channel_id=CH, message_id=m.id))
        pend2 = await _db(bot, "SELECT reason FROM review_pending WHERE message_id=?", str(m.id))
        return pend, calls_after_edit, pend2
    pend, calls_after_edit, pend2 = rv.run(c)
    assert pend == [{"reason": "row_mismatch"}]
    assert calls_after_edit == ["append"]                             # NICHT überschrieben
    assert pend2 == []                                                # 🟡 = bestätigt → überschrieben
    assert [x[0] for x in rv.sheet._ws.calls] == ["append", "update"]
    assert rv.sheet.rows[1][0] == "20.11.2026" and "🟡" not in m.reactions and "🟢" in m.reactions


def test_yellow_ignored_without_pending_or_from_bot(rv):
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        bot.get_channel = lambda cid: FakeChannel([m])
        await cog.on_raw_reaction_add(SimpleNamespace(emoji="🟡", user_id=UID, channel_id=CH, message_id=m.id))
        await cog.on_raw_reaction_add(SimpleNamespace(emoji="🟡", user_id=1, channel_id=CH, message_id=m.id))
        await cog.on_raw_reaction_add(SimpleNamespace(emoji="👍", user_id=UID, channel_id=CH, message_id=m.id))
    rv.run(c)
    assert rv.ai == []


def test_delete_clears_row_and_db(rv):
    m = FakeMsg(REVIEW)

    async def c(cog, bot):
        await _send(cog, m)
        await cog.on_raw_message_delete(SimpleNamespace(channel_id=CH, message_id=m.id))
        out = {}
        for t in ("review_tracking", "review_delivery", "review_authors", "review_pending"):
            out[t] = await _db(bot, f"SELECT 1 FROM {t} WHERE message_id=?", str(m.id))
        return out
    left = rv.run(c)
    assert all(v == [] for v in left.values())
    assert rv.sheet._ws.calls[-1] == ("clear", "A2:I2")
    assert rv.sheet.row_count == 2 and rv.sheet.rows[1][0] == ""     # Zeilennummern bleiben stabil


def test_reconcile_maps_by_date_and_order(rv):
    d = datetime.now(timezone.utc) - timedelta(days=2)
    ds = d.strftime("%d.%m.%Y")
    a = FakeMsg(REVIEW, created=d)
    b = FakeMsg(REVIEW.replace("9/10", "5/10"), created=d + timedelta(minutes=5))
    old = FakeMsg(REVIEW, created=datetime.now(timezone.utc) - timedelta(days=400))   # außerhalb SCAN_DAYS
    rv.sheet._rows.append([ds, "x", "myrmeco.de", "", "", "", "8", "", ""])

    async def c(cog, bot):
        mapped, written = await cog._reconcile_scan(FakeChannel([a, b, old]))
        t = {}
        for m in (a, b, old):
            r = await _db(bot, "SELECT sheet_row FROM review_tracking WHERE message_id=?", str(m.id))
            t[m] = r[0]["sheet_row"] if r else None
        return mapped, written, t
    mapped, written, t = rv.run(c)
    assert (mapped, written) == (1, 1)
    assert t[a] == 2 and t[b] == 3 and t[old] is None                # a → vorhandene Zeile, b neu
    assert len(rv.ai) == 1                                            # nur b ging an die KI


def test_verify_row_rules():
    import cogs.reviews as R
    from utils.sheet import SheetCache
    sh = SheetCache()
    sh._rows = [list(HEADER), ["20.11.2026", "x", "myrmeco.de"], ["", "", ""]]
    orig = R.sheet
    R.sheet = sh
    try:
        R.ReviewsCog._verify_row(2, "20.11.2026", "Myrmeco.de")      # Groß/klein egal
        R.ReviewsCog._verify_row(3, "20.11.2026", "myrmeco.de")      # leere Zeile → ok
        with pytest.raises(R.RowMismatch):
            R.ReviewsCog._verify_row(2, "21.11.2026", "myrmeco.de")
        with pytest.raises(R.RowMismatch):
            R.ReviewsCog._verify_row(2, "20.11.2026", "antstore.net")
        with pytest.raises(R.RowMismatch):
            R.ReviewsCog._verify_row(9, "20.11.2026", "myrmeco.de")
    finally:
        R.sheet = orig
