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
web/core/security.py – Sicherheit & Sitzungen: Rate-Limits, HMAC, CSRF, Captcha, Admin-/Mitglieder-Cookies, Login-Weiterleitung.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import hashlib
import hmac
import random
import re
import time
from collections import defaultdict
from urllib.parse import urlencode, urlparse
from aiohttp import web
from utils import account as ACC
from utils.board_i18n import pick_lang
from config import (
    BOARD_ADMIN_IDS, BOARD_AI_CHAT, BOARD_HASH_SALT, BOARD_OAUTH_CLIENT_ID, BOARD_PUBLIC_URL,
    BOARD_OAUTH_REDIRECT_URI, BOARD_OWNER_ID, MAP_ENABLED, MAP_GUILD_ID,
)
from utils.web_keys import BOT_KEY


def _make_captcha() -> tuple[str, str]:
    """Zufällige kleine Rechenaufgabe als leichter Bot-Schutz. Stateless: Rückgabe
    (Frage, signierte Antwort). Die Antwort selbst steht nie im Klartext im Formular –
    nur ihr HMAC; bei der Prüfung wird der HMAC der eingegebenen Zahl verglichen."""
    a, b = random.randint(2, 9), random.randint(2, 9)
    op = random.choice(["+", "-", "×"])
    if op == "-" and b > a:
        a, b = b, a
    ans = a + b if op == "+" else (a - b if op == "-" else a * b)
    return f"{a} {op} {b}", _hmac("captcha", str(ans))


def _captcha_ok(form) -> bool:
    sig = form.get("captcha_sig", "")
    raw = (form.get("captcha") or "").strip().replace(" ", "")
    try:
        val = str(int(raw))
    except (TypeError, ValueError):
        return False
    return bool(sig) and hmac.compare_digest(sig, _hmac("captcha", val))
# Cookies mit „Secure“ setzen (Browser sendet sie dann nur über HTTPS), sobald das Board
# öffentlich per HTTPS erreichbar ist. Bewusst aus BOARD_PUBLIC_URL statt req.secure: hinter
# dem Reverse-Proxy (TLS endet dort) sieht der Bot nur http. Lokal über http bleibt es aus.
def _https_url(url: str) -> bool:
    return (url or "").strip().lower().startswith("https://")


COOKIE_SECURE = _https_url(BOARD_PUBLIC_URL)
_ADMIN_COOKIE, _VOTER_COOKIE = "board_admin", "board_vid"

_hits: dict[str, list] = defaultdict(list)
# Aufräumen: Schlüssel (u. a. je IP-Hash) würden sonst bis zum Neustart im Speicher bleiben.
# Entfernt werden nur Schlüssel, deren letzter Eintrag älter ist als das größte jemals
# verwendete Zeitfenster – für die Limits sind sie dann garantiert bedeutungslos.
_SWEEP_EVERY = 300                       # höchstens alle 5 Minuten
_sweep_state = {"last": 0.0, "max_window": 0}


def _sweep_hits(now: float) -> int:
    cutoff = now - _sweep_state["max_window"]
    stale = [k for k, q in _hits.items() if not q or q[-1] < cutoff]
    for k in stale:
        del _hits[k]
    _sweep_state["last"] = now
    return len(stale)


def _rate(key: str, limit: int, window: int) -> bool:
    now = time.time()
    if window > _sweep_state["max_window"]:
        _sweep_state["max_window"] = window
    if now - _sweep_state["last"] >= _SWEEP_EVERY:
        _sweep_hits(now)
    q = _hits[key]
    while q and q[0] < now - window:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now); return True


def salt_problem(salt: bytes | None = None) -> str | None:
    """Prüft den HMAC-Schlüssel (BOARD_HASH_SALT). None = in Ordnung, sonst der Grund.

    Mit dem öffentlich bekannten Platzhalter (oder einem zu kurzen Wert) könnte jeder, der
    den Quellcode kennt, Admin-Cookies und CSRF-Tokens selbst signieren."""
    from config import BOARD_HASH_SALT_MIN_LEN, BOARD_HASH_SALT_PLACEHOLDER
    s = BOARD_HASH_SALT if salt is None else salt
    if not s or s == BOARD_HASH_SALT_PLACEHOLDER.encode():
        return "BOARD_HASH_SALT ist nicht gesetzt (Platzhalter aktiv)"
    if len(s) < BOARD_HASH_SALT_MIN_LEN:
        return f"BOARD_HASH_SALT ist zu kurz ({len(s)} Zeichen, mindestens {BOARD_HASH_SALT_MIN_LEN})"
    return None


def _hmac(*parts: str) -> str:
    # HMAC-SHA3-512 mit geheimem Salt als Schlüssel (IPs werden nie roh gespeichert).
    return hmac.new(BOARD_HASH_SALT, "|".join(parts).encode(), hashlib.sha3_512).hexdigest()


def _ip(req):
    xff = req.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (req.remote or "0.0.0.0")


_ADMIN_TTL = 7 * 86400                               # Admin-Sitzung: 7 Tage


def _admin_ids() -> set[str]:
    """Discord-IDs mit Admin-Rechten im Board: BOARD_OWNER_ID + optional BOARD_ADMIN_IDS."""
    ids = {str(i) for i in BOARD_ADMIN_IDS}
    if BOARD_OWNER_ID:
        ids.add(str(BOARD_OWNER_ID))
    return ids


def _admin_sign(uid: str, exp: int) -> str:
    return _hmac("admin", str(uid), str(exp))


def _admin_session(req) -> tuple[str, int] | None:
    """(Discord-ID, Ablauf) der Admin-Sitzung, sonst None. Cookie '<uid>.<exp>.<sig>',
    HMAC-signiert; die ID muss (noch) in der Admin-Liste stehen."""
    parts = req.cookies.get(_ADMIN_COOKIE, "").split(".")
    if len(parts) != 3:
        return None
    uid, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()) or not hmac.compare_digest(sig, _admin_sign(uid, exp)):
        return None
    if not ACC.session_ok(uid, exp - _ADMIN_TTL):        # „überall abmelden“ in Mein Konto
        return None
    return (uid, exp) if uid in _admin_ids() else None


def _is_admin(req) -> bool:
    return _admin_session(req) is not None


def _csrf_token(req) -> str:
    """CSRF-Token aus der Admin-Sitzung abgeleitet (kein User-Input, kein separater
    Cookie). Nur wer als Admin eingeloggt ist, bekommt es in die Formulare."""
    s = _admin_session(req)
    return _hmac("csrf", s[0], str(s[1])) if s else ""


def _csrf_ok(req, form) -> bool:
    exp = _csrf_token(req)
    return bool(exp) and hmac.compare_digest(form.get("csrf", ""), exp)


def _chat_ttl() -> int:
    import config as cfg
    return cfg.AI_CHAT_CONVERSATION_TTL_HOURS


def _chat_uid(req) -> str | None:
    """Discord-ID, wenn der Web-KI-Chat für diese Anfrage angeboten wird."""
    if not BOARD_AI_CHAT:
        return None
    try:
        if "AiChatCog" not in req.app[BOT_KEY].cogs:
            return None
    except Exception:
        return None
    return _aam_member(req)


def _aam_member(req) -> str | None:
    """Discord-ID, wenn eingeloggt UND aktuell Mitglied des AAM-Servers, sonst None."""
    uid = _is_member(req)
    return uid if (uid and _member_name(req.app, uid)) else None


def _login_url(req) -> str:
    if not (MAP_ENABLED and BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return ""
    return "/map/login?" + urlencode({"next": req.path, "lang": pick_lang(req)})


def _safe_local_redirect(target: str, fallback: str = "/") -> str:
    """Nur auf einen LOKALEN, relativen Pfad weiterleiten (Schutz vor Open-Redirect,
    CWE-601). Alles mit Schema/Host oder protokoll-relativem //host wird verworfen –
    dann greift der Fallback. Backslashes werden vor der Prüfung entfernt, da viele
    Browser sie wie / behandeln."""
    t = (target or "").replace("\\", "")
    p = urlparse(t)
    if t.startswith("/") and not t.startswith("//") and not p.scheme and not p.netloc:
        return t
    return fallback


def _admin_login_url(lang: str) -> str:
    if not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return ""
    return "/map/login?" + urlencode({"next": "/admin", "lang": lang})


async def _admin_guard(req):
    if not _is_admin(req):
        raise web.HTTPFound("/admin/login")
    d = await req.post()
    if not _csrf_ok(req, d):
        raise web.HTTPForbidden(text="CSRF-Token ungültig")
    return d

_MEMBER_COOKIE = "board_member"
_OAUTH_STATE_COOKIE = "board_oauth_state"
_MEMBER_TTL = 30 * 86400                             # 30 Tage Session
_DISCORD_AUTH = "https://discord.com/oauth2/authorize"
_DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
_DISCORD_ME = "https://discord.com/api/users/@me"

# Rate-Limit der Karten-JSON-Endpunkte (pro IP; _ip/_rate wie beim Board, HMAC-IP-basiert).
RATE_MAP_PER_MIN = 60


def _map_rate_ok(req) -> bool:
    return _rate("mapdata:" + _hmac("map", _ip(req)), RATE_MAP_PER_MIN, 60)


_LOGIN_NEXT_COOKIE = "board_login_next"
_LOGIN_NEXT_RE = re.compile(r"^/(?:art|shops|shop|admin|rueckblick|konto)(?:/[A-Za-z0-9_.%\-]{1,120})?$")


def _login_next(path: str) -> str:
    """Rücksprung nach dem Discord-Login – nur auf Art-/Händler-/Rückblick-/Kontoseiten (Whitelist)."""
    p = (path or "").strip()
    return p if _LOGIN_NEXT_RE.match(p) else ""


def _member_sign(uid: str, exp: int) -> str:
    return _hmac("member", str(uid), str(exp))


def _is_member(req) -> str | None:
    """Gibt die Discord-User-ID der gültigen Mitglieder-Session zurück, sonst None.
    Cookie-Format: '<uid>.<exp>.<sig>' – HMAC-signiert, mit Ablauf."""
    raw = req.cookies.get(_MEMBER_COOKIE, "")
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    uid, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    if not hmac.compare_digest(sig, _member_sign(uid, exp)):
        return None
    if not ACC.session_ok(uid, exp - _MEMBER_TTL):       # „überall abmelden“ in Mein Konto
        return None
    return uid


def _member_name(app, uid):
    """Löst den aktuellen Discord-Anzeigenamen live über die User-ID auf (nie gespeichert).
    None, wenn die Person nicht (mehr) Mitglied ist."""
    if not MAP_GUILD_ID:
        return None
    bot = app[BOT_KEY]
    g = bot.get_guild(MAP_GUILD_ID)
    if not g:
        return None
    m = g.get_member(int(uid))
    return m.display_name if m else None


def _contact_url(uid) -> str:
    """Discord-Profil-Link (öffnet Profil, von dort PN). Nur numerische IDs zulassen."""
    u = str(uid or "").strip()
    return f"https://discord.com/users/{u}" if u.isdigit() else ""


def _member_csrf(uid: str) -> str:
    """CSRF-Token für Mitglieder-Aktionen (zusätzlich zu SameSite=Lax am Login-Cookie)."""
    return _hmac("mcsrf", str(uid))[:32]
