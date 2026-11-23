# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discord-Login mit Rücksprungziel (serverseitig gemerkt, nicht im Cookie) und die
Schwarmflug-Admin-Weiterleitung – offline mit simuliertem Discord."""
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import aiohttp

import board_snapshot as BS


class _Resp:
    def __init__(self, data):
        self._d = data

    async def json(self):
        return self._d

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeDiscordSession:
    """Ersatz für aiohttp.ClientSession im Login: Token-Tausch + /users/@me."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, *a, **k):
        return _Resp({"access_token": "fake"})

    def get(self, *a, **k):
        return _Resp({"id": BS.MEMBER_ID})


def _fake_discord(monkeypatch, L):
    """Nur im Login-Modul ersetzen – der Test-Client braucht das echte aiohttp."""
    monkeypatch.setattr(L, "aiohttp", SimpleNamespace(ClientSession=_FakeDiscordSession,
                                                      ClientTimeout=aiohttp.ClientTimeout))


def _run(fn):
    from aiohttp.test_utils import TestClient, TestServer
    from web import build_app

    async def go():
        bot = BS.FakeBot(asyncio.get_running_loop())
        async with TestClient(TestServer(build_app(bot))) as c:
            return await fn(c)
    return asyncio.run(go())


def _login(c, nxt):
    async def go():
        r = await c.get(f"/map/login?lang=de&next={nxt}", allow_redirects=False)
        assert r.status == 302
        set_cookies = r.headers.getall("Set-Cookie", [])
        # Rücksprung steht NICHT mehr im Cookie (höchstens das Löschen eines Alt-Cookies)
        assert not any(h.startswith("board_login_next=") and not h.startswith('board_login_next=""')
                       for h in set_cookies), set_cookies
        state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
        return state, r.cookies["board_oauth_state"].value
    return go()


def _callback(c, state, state_cookie):
    async def go():
        r = await c.get(f"/map/callback?lang=de&code=abc&state={state}",
                        cookies={"board_oauth_state": state_cookie}, allow_redirects=False)
        return r.status, r.headers.get("Location", ""), "board_member" in r.cookies
    return go()


def test_login_returns_to_requested_page(monkeypatch):
    import web.features.login as L
    _fake_discord(monkeypatch, L)

    async def flow(c):
        state, ck = await _login(c, "/shop/antmeup.example")
        status, loc, member = await _callback(c, state, ck)
        assert status == 302 and loc == "/shop/antmeup.example?lang=de" and member
        # Ziel gilt nur einmal: erneuter Callback mit demselben state → Karte
        status, loc, _ = await _callback(c, state, ck)
        assert loc.startswith("/map")
    _run(flow)


def test_foreign_or_invalid_target_is_ignored(monkeypatch):
    import web.features.login as L
    _fake_discord(monkeypatch, L)

    async def flow(c):
        for bad in ("https://boese.example/x", "//boese.example", "/map;evil", "/konto%0d%0aSet-Cookie:x"):
            state, ck = await _login(c, bad)
            status, loc, _ = await _callback(c, state, ck)
            assert loc.startswith("/map"), (bad, loc)
    _run(flow)


def test_pending_targets_expire_and_are_bounded(monkeypatch):
    import web.features.login as L
    clock = {"t": 1000.0}
    monkeypatch.setattr(L.time, "time", lambda: clock["t"])
    monkeypatch.setattr(L, "_pending_next", {})
    L._remember_next("a", "/konto")
    clock["t"] += L._NEXT_TTL + 1
    assert L._pop_next("a") == ""                       # abgelaufen
    for i in range(L._NEXT_MAX + 10):
        L._remember_next(f"s{i}", "/konto")
    assert len(L._pending_next) <= L._NEXT_MAX


def test_swarm_fix_redirect_contains_only_number(monkeypatch):
    import utils.swarm_resolve as R

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(R, "set_override", noop)
    monkeypatch.setattr(R, "apply_override", noop)

    async def flow(c):
        r = await c.post("/admin/schwarmflug/fix?lang=de",
                         data={"mid": "12a3<x>4", "where": "", "csrf": BS._admin_csrf()},
                         cookies=BS._cookies("admin"), allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == "/admin/schwarmflug?lang=de&ok=1234#m1234"
    _run(flow)
