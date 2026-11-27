# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wöchentlicher Digest (cogs/digest.py):
  • Preisstürze der letzten 7 Tage (gegen den Preis davor), größter zuerst, Merch nie
  • neue Arten/Shops: erster Lauf füllt nur die Basis, danach nur echte Neuheiten
  • Arten nach Gattung gruppiert, Link bevorzugt auf lagerndes Produkt
  • Aufteilung in mehrere PNs ohne einen Gattungs-Block zu zerreißen
  • nur montags, gesperrte DMs werden übersprungen
"""
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import board_snapshot as BS
import cogs.digest as D

UID = "840000000000000001"
BLOCKED = "840000000000000002"


def _p(pid, species, price=20, stock=True, title=None):
    return {"id": pid, "species": species, "title": title or species, "min_price": price, "max_price": price,
            "currency_iso": "EUR", "in_stock": stock, "is_active": True,
            "antcheck_url": f"https://antcheck.example/{pid}", "variants": []}


def _shops(extra_species=False, extra_shop=False):
    prods = [_p(1, "Lasius niger"), _p(2, "Messor barbarus"), _p(3, "Lasius niger", title="Poster Lasius niger")]
    if extra_species:
        prods += [_p(4, "Camponotus ligniperda", stock=False), _p(5, "Camponotus ligniperda"), _p(6, "Lasius flavus")]
    data = {"_meta": {"fetched_at": "2026-11-27T08:00:00+00:00"},
            "1": {"id": 1, "name": "ShopDE", "country": "de", "products": prods}}
    if extra_shop:
        data["9"] = {"id": 9, "name": "NeuShop", "country": "at", "products": []}
    return data


class _User:
    def __init__(self, uid, blocked=False):
        self.id, self.blocked, self.embeds = int(uid), blocked, []

    async def send(self, embed=None, **kw):
        if self.blocked:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "gesperrt")
        self.embeds.append(embed.description)


class _Bot(BS.FakeBot):
    def __init__(self, loop):
        super().__init__(loop)
        self.u = {}

    async def fetch_user(self, uid):
        return self.u.setdefault(int(uid), _User(uid, blocked=str(uid) == BLOCKED))


@pytest.fixture()
def dg():
    f = Path(os.environ["SHOPS_DATA_FILE"])
    original = f.read_text(encoding="utf-8")
    D.PRICE_HISTORY_DB.unlink(missing_ok=True)
    con = sqlite3.connect(D.PRICE_HISTORY_DB)
    import grabber
    con.executescript(grabber._PRICE_HISTORY_SCHEMA)
    rows = [(1, 30, "-20 days"), (1, 30, "-9 days"), (1, 21, "-2 days"),     # −30 %
            (2, 40, "-10 days"), (2, 36, "-1 days"),                          # −10 %
            (3, 50, "-10 days"), (3, 10, "-1 days")]                          # Merch → nie
    con.executemany("INSERT INTO product_price_history (product_id, min_price, max_price, recorded_at) "
                    "VALUES (?, ?, ?, datetime('now', ?))", [(a, b, b, c) for a, b, c in rows])
    con.commit()
    con.close()

    def run(rounds):
        async def go():
            from utils.db import init_db, execute_db
            bot = _Bot(asyncio.get_running_loop())
            await init_db(bot)
            for t in ("digest_subscribers", "known_species", "known_shops"):
                await execute_db(bot, f"DELETE FROM {t}", commit=True)
            for uid in (UID, BLOCKED):
                await execute_db(bot, "INSERT INTO digest_subscribers (user_id) VALUES (?)", (uid,), commit=True)
            cog = D.DigestCog.__new__(D.DigestCog)
            cog.bot = bot
            out = []
            for shops in rounds:
                f.write_text(json.dumps(shops), encoding="utf-8")
                before = len((await bot.fetch_user(UID)).embeds)
                await cog._run_digest()
                out.append("\n".join((await bot.fetch_user(UID)).embeds[before:]))
            return out
        return asyncio.run(go())
    yield run
    f.write_text(original, encoding="utf-8")
    D.PRICE_HISTORY_DB.unlink(missing_ok=True)


def test_first_run_drops_only_no_new_species(dg):
    (msg,) = dg([_shops()])
    assert "Lasius niger" in msg and "30.00 EUR" in msg and "21.00 EUR" in msg and "30" in msg
    assert msg.index("Lasius niger") < msg.index("Messor barbarus")          # größter Sturz zuerst
    assert "10.00 EUR" not in msg                                            # Poster (Merch) nie
    assert "NeuShop" not in msg and "Camponotus" not in msg


def test_second_run_lists_new_species_grouped_and_new_shop(dg):
    first, second = dg([_shops(), _shops(extra_species=True, extra_shop=True)])
    assert "Camponotus ligniperda" not in first
    assert "Camponotus ligniperda" in second and "Lasius flavus" in second and "NeuShop" in second
    assert "antcheck.example/5" in second and "antcheck.example/4" not in second   # lagernder Link bevorzugt
    assert second.count("Lasius niger") == 1                                  # nur noch als Preissturz


def test_blocked_dm_does_not_stop_others(dg):
    (msg,) = dg([_shops()])
    assert msg                                                                # UID bekam trotzdem den Digest


def test_only_on_monday(monkeypatch):
    called = []

    class _Fake:
        bot = None

        async def _run_digest(self):
            called.append(1)
    from datetime import datetime as real
    for wd, expect in ((0, 1), (3, 0)):
        class _DT(real):
            @classmethod
            def now(cls, tz=None):
                return real(2026, 11, 23 + wd, 9, 0, tzinfo=tz)
        monkeypatch.setattr(D, "datetime", _DT)
        called.clear()
        asyncio.run(D.DigestCog.weekly_digest.coro(_Fake()))
        assert len(called) == expect


def test_nothing_new_message():
    cog = D.DigestCog.__new__(D.DigestCog)
    (msg,) = cog._build_chunks("en", [], [], [])
    from utils.localization import l10n
    assert l10n.get("digest_nothing", "en") in msg


def test_chunk_blocks_keeps_blocks_whole():
    blocks = ["Titel"] + ["\n".join([f"Gattung {g}"] + [f"  Art {g}-{i}" for i in range(30)]) for g in range(6)]
    chunks = D._chunk_blocks(blocks, 600)
    assert all(len(c) <= 600 for c in chunks) and len(chunks) > 1
    for g in range(6):
        holder = [c for c in chunks if f"Gattung {g}\n" in c]
        assert len(holder) == 1 and f"Art {g}-29" in holder[0]                 # Block nie zerteilt
    huge = D._chunk_blocks(["\n".join(f"Zeile {i} " + "x" * 50 for i in range(40))], 600)
    assert len(huge) > 1 and all(len(c) <= 600 for c in huge)                  # Notfall: zeilenweise
