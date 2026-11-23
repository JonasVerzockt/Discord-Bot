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
web/features/login.py – Discord-Login (OAuth2) für Mitglieder und Admins: /map/login, /map/callback, /map/logout.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import hmac
import secrets
import time
import aiohttp
from aiohttp import web
from urllib.parse import urlencode
from utils.board_i18n import pick_lang, translate
from config import (
    BOARD_OAUTH_CLIENT_ID, BOARD_OAUTH_CLIENT_SECRET, BOARD_OAUTH_REDIRECT_URI, MAP_MAX_ZOOM,
)

from web.core.render import _render
from web.core.security import (
    _ADMIN_COOKIE, _admin_ids, _admin_sign, _ADMIN_TTL, _DISCORD_AUTH, _DISCORD_ME,
    _DISCORD_TOKEN, _hmac, _login_next, _LOGIN_NEXT_COOKIE, _MEMBER_COOKIE, _member_name,
    _member_sign, _MEMBER_TTL, _OAUTH_STATE_COOKIE, _safe_local_redirect,
)
from web.features.map import _map_asset_v, _map_lang_redirect

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


# Rücksprungziel nach dem Discord-Login – SERVERSEITIG unter dem zufälligen, signierten
# OAuth-state gemerkt (nicht im Cookie): So landet kein Nutzerwert in einem Set-Cookie-Header.
# Kurzlebig (wie der state-Cookie) und in der Größe begrenzt; geht bei einem Bot-Neustart
# während des Logins verloren → dann Rücksprung auf die Karte (wie ohne Ziel).
_NEXT_TTL = 600
_NEXT_MAX = 2000
_pending_next: dict[str, tuple[str, float]] = {}


def _remember_next(state: str, nxt: str) -> None:
    now = time.time()
    for k in [k for k, (_, exp) in _pending_next.items() if exp < now]:
        del _pending_next[k]
    if len(_pending_next) >= _NEXT_MAX:                       # Notbremse gegen Fluten
        for k in sorted(_pending_next, key=lambda k: _pending_next[k][1])[:len(_pending_next) // 2]:
            del _pending_next[k]
    _pending_next[state] = (nxt, now + _NEXT_TTL)


def _pop_next(state: str) -> str:
    nxt, exp = _pending_next.pop(state, ("", 0.0))
    return nxt if exp >= time.time() else ""


async def h_map_login(req):
    lang = pick_lang(req)
    if not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return _render(req, "map", title=translate(lang, "map_h"),
                       flash=translate(lang, "map_oauth_unconfigured"),
                       v=_map_asset_v(), member=False, max_zoom=MAP_MAX_ZOOM)
    state = secrets.token_urlsafe(24)
    nxt = _login_next(req.query.get("next", ""))
    params = urlencode({
        "client_id": BOARD_OAUTH_CLIENT_ID, "response_type": "code",
        "scope": "identify", "redirect_uri": BOARD_OAUTH_REDIRECT_URI,
        "state": state, "prompt": "none",
    })
    resp = web.HTTPFound(f"{_DISCORD_AUTH}?{params}")
    # State signiert im Cookie ablegen (CSRF-Schutz), kurzlebig.
    resp.set_cookie(_OAUTH_STATE_COOKIE, f"{state}.{_hmac('oauthstate', state)}",
                    max_age=600, httponly=True, samesite="Lax")
    if nxt:
        _remember_next(state, nxt)
    resp.del_cookie(_LOGIN_NEXT_COOKIE)          # Alt-Cookie früherer Versionen aufräumen
    raise resp


async def h_map_callback(req):
    lang = pick_lang(req)
    code = req.query.get("code", "")
    state = req.query.get("state", "")
    ck = req.cookies.get(_OAUTH_STATE_COOKIE, "")
    ok_state = False
    if "." in ck:
        s0, sig0 = ck.split(".", 1)
        ok_state = (s0 == state) and hmac.compare_digest(sig0, _hmac("oauthstate", s0))
    if not code or not ok_state:
        raise _map_lang_redirect("/map", lang)
    # Code gegen Token tauschen (server-to-server), dann /users/@me – Token danach verwerfen.
    uid = None
    try:
        data = {
            "client_id": BOARD_OAUTH_CLIENT_ID,
            "client_secret": BOARD_OAUTH_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code, "redirect_uri": BOARD_OAUTH_REDIRECT_URI,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(_DISCORD_TOKEN, data=data,
                              headers={"Content-Type": "application/x-www-form-urlencoded"},
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                tok = await r.json()
            access = tok.get("access_token")
            if access:
                async with s.get(_DISCORD_ME,
                                 headers={"Authorization": f"Bearer {access}"},
                                 timeout=aiohttp.ClientTimeout(total=15)) as r2:
                    me = await r2.json()
                    uid = str(me.get("id") or "")
    except Exception as e:
        logger.warning("🗺️ OAuth-Callback-Fehler: %s", e)
        uid = None
    if not uid:
        raise _map_lang_redirect("/map", lang)
    # Mitgliedschaft serverseitig prüfen (kein guilds-Scope nötig).
    if not _member_name(req.app, uid):
        resp = _map_lang_redirect("/map", lang)
        resp.del_cookie(_OAUTH_STATE_COOKIE)
        raise resp
    exp = int(time.time()) + _MEMBER_TTL
    nxt = _login_next(_pop_next(state))        # state ist oben bereits geprüft (HMAC)
    resp = (web.HTTPFound(_safe_local_redirect(f"{nxt}?lang={lang}", "/map"))
            if nxt else _map_lang_redirect("/map", lang))
    resp.del_cookie(_OAUTH_STATE_COOKIE)
    resp.del_cookie(_LOGIN_NEXT_COOKIE)
    if uid in _admin_ids():                    # Owner/Admin: zusätzlich Admin-Sitzung (7 Tage)
        aexp = int(time.time()) + _ADMIN_TTL
        resp.set_cookie(_ADMIN_COOKIE, f"{uid}.{aexp}.{_admin_sign(uid, aexp)}",
                        max_age=_ADMIN_TTL, httponly=True, samesite="Lax")
        logger.info("🔑 Admin-Login über Discord: %s", uid)
    elif nxt == "/admin":
        resp = web.HTTPFound(f"/admin/login?denied=1&lang={lang}")
        resp.del_cookie(_OAUTH_STATE_COOKIE)
        resp.del_cookie(_LOGIN_NEXT_COOKIE)
    resp.set_cookie(_MEMBER_COOKIE, f"{uid}.{exp}.{_member_sign(uid, exp)}",
                    max_age=_MEMBER_TTL, httponly=True, samesite="Lax")
    raise resp


async def h_map_logout(req):
    resp = _map_lang_redirect("/map", pick_lang(req))
    resp.del_cookie(_MEMBER_COOKIE)
    raise resp
