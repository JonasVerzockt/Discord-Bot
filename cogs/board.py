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
cogs/board.py – Öffentliches Feedback-Board (Bugs/Features/Ideen) als Bot-Cog.

Läuft als aiohttp-Webserver IM Bot-Prozess (auf dem Bot-Loop, via AppRunner/TCPSite),
nutzt eine EIGENE DB (`utils/board_db.py` → `config.BOARD_DB_FILE`). Anonymes
Einreichen (Moderations-Queue), Upvotes (dedupe), Owner-Admin. Bei neuer Einreichung
private DM an den Owner (`BOARD_OWNER_ID`). Standardmäßig AUS (`BOARD_ENABLED`).

Sicherheit: nur an 127.0.0.1 binden (Reverse-Proxy/HTTPS davor), Honeypot,
Rate-Limits, HMAC-gehashte IPs (keine Roh-IP), CSRF auf Admin-Aktionen,
Jinja2-Autoescape gegen XSS.
"""
import csv as _csv
import hashlib
import hmac
import io
import logging
import secrets
import time
from collections import defaultdict

import discord
from aiohttp import web
from discord.ext import commands
from jinja2 import Environment, DictLoader, select_autoescape

from config import (BOARD_ENABLED, BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL,
                    BOARD_ADMIN_TOKEN, BOARD_OWNER_ID, BOARD_HASH_SALT)
from utils.board_db import (board_init, board_query, board_one, board_exec, board_execmany)

logger = logging.getLogger(__name__)

TYPES       = ["bug", "feature", "idea"]
STATUSES    = ["pending", "open", "planned", "in_progress", "done", "rejected", "duplicate"]
PUBLIC_COLS = [("open", "🗳️ Offen / Backlog"), ("planned", "📌 Geplant"),
               ("in_progress", "🔧 In Arbeit"), ("done", "✅ Erledigt")]
PRIORITIES  = ["", "P0", "P1", "P2", "P3"]
COMPONENTS  = ["", "Preis-Tracking", "Benachrichtigungen", "Shop-Suche/Grabber", "KI-Chat",
               "Digest", "iNat", "Rabattcodes", "Review-Bot", "Erfolge", "Moderation",
               "Infra/Deploy", "UI", "Lokalisierung", "Doku", "Sonstiges"]
RATE_SUBMIT_PER_H = 5
_ADMIN_COOKIE, _VOTER_COOKIE, _CSRF_COOKIE = "board_admin", "board_vid", "board_csrf"

_hits: dict[str, list] = defaultdict(list)


def _rate(key: str, limit: int, window: int) -> bool:
    now = time.time(); q = _hits[key]
    while q and q[0] < now - window:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now); return True


def _hmac(*parts: str) -> str:
    return hmac.new(BOARD_HASH_SALT, "|".join(parts).encode(), hashlib.sha256).hexdigest()


def _ip(req):
    xff = req.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (req.remote or "0.0.0.0")


def _is_admin(req) -> bool:
    exp = _hmac("owner", BOARD_ADMIN_TOKEN) if BOARD_ADMIN_TOKEN else ""
    return bool(exp) and hmac.compare_digest(req.cookies.get(_ADMIN_COOKIE, ""), exp)


def _csrf_ok(req, form) -> bool:
    c = req.cookies.get(_CSRF_COOKIE, "")
    return bool(c) and hmac.compare_digest(c, form.get("csrf", ""))


# ── Templates (Dark-Mode-ONLY) ────────────────────────────────────────────────
BASE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{{ title }} · AAM-Bot Board</title><style>
 :root{color-scheme:only dark} html,body{background:#0d1117}
 body{color:#e6edf3;font:15px/1.5 system-ui,Segoe UI,Arial;margin:0}
 option{background:#0d1117;color:#e6edf3} ::placeholder{color:#6e7681;opacity:1}
 a{color:#58a6ff;text-decoration:none} a:hover{text-decoration:underline}
 header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;gap:16px;align-items:center}
 header h1{font-size:18px;margin:0} .grow{flex:1}
 .wrap{max-width:1100px;margin:0 auto;padding:20px}
 .btn{background:#238636;color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer;font-size:14px}
 .btn.grey{background:#30363d} .btn.red{background:#8b2b2b} .btn.small{padding:3px 8px;font-size:13px}
 .cols{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
 .col h2{font-size:14px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;margin:0 0 8px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 12px;margin-bottom:10px}
 .card .t{font-weight:600} .muted{color:#8b949e;font-size:13px}
 .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;border:1px solid #30363d;margin-right:5px}
 .bug{color:#ff7b72;border-color:#ff7b72} .feature{color:#7ee787;border-color:#7ee787} .idea{color:#d2a8ff;border-color:#d2a8ff}
 .up{background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:20px;padding:3px 10px;cursor:pointer}
 input,textarea,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px;width:100%;box-sizing:border-box}
 label{display:block;margin:10px 0 3px;color:#8b949e;font-size:13px}
 table{width:100%;border-collapse:collapse} td,th{border-bottom:1px solid #21262d;padding:6px 8px;text-align:left;vertical-align:top}
 .hp{position:absolute;left:-9999px} .flash{background:#1f6feb22;border:1px solid #1f6feb;border-radius:6px;padding:10px 12px;margin-bottom:14px}
</style></head><body>
<header><h1>🐜 AAM-Bot · Ideen &amp; Bugs</h1>
 <a href="/">Board</a><a href="/submit">Einreichen</a><span class=grow></span>
 {% if admin %}<span class=muted>Owner</span> <a href="/admin">Admin</a> <a href="/admin/logout">Logout</a>
 {% else %}<a href="/admin/login">Owner-Login</a>{% endif %}</header>
<div class=wrap>{% if flash %}<div class=flash>{{ flash }}</div>{% endif %}{% block body %}{% endblock %}</div>
</body></html>"""

BOARD = """{% extends "base" %}{% block body %}
<p class=muted>Öffentliche Ideen &amp; gemeldete Bugs. Jeder darf anonym einreichen und hochvoten –
neue Einreichungen erscheinen erst nach Prüfung. <a href="/submit">+ Einreichen</a></p>
<div class=cols>{% for key,label in cols %}
 <div class=col><h2>{{ label }}</h2>
 {% for c in items if c.status==key %}
  <div class=card><span class="tag {{c.type}}">{{ c.type }}</span>
   {% if c.component %}<span class=tag>{{ c.component }}</span>{% endif %}
   {% if c.priority %}<span class=tag>{{ c.priority }}</span>{% endif %}
   <div class=t><a href="/submission/{{c.id}}">{{ c.title }}</a></div>
   {% if c.version %}<div class=muted>erledigt in {{ c.version }}</div>{% endif %}
   <form method=post action="/upvote/{{c.id}}" style="margin-top:6px"><button class=up>▲ {{ c.upvotes }}</button></form>
  </div>
 {% else %}<div class=muted>—</div>{% endfor %}</div>
{% endfor %}</div>
{% set rej = items|selectattr('status','equalto','rejected')|list %}
{% if rej %}<h2 style="color:#8b949e;margin-top:24px">🚫 Abgelehnt</h2>{% for c in rej %}<div class=muted>• {{ c.title }}</div>{% endfor %}{% endif %}
{% endblock %}"""

SUBMIT = """{% extends "base" %}{% block body %}
<h2>Idee oder Bug einreichen</h2>
<p class=muted>Anonym möglich. Deine Einreichung wird zuerst geprüft und erscheint dann öffentlich.</p>
<form method=post action="/submit">
 <label>Art</label><select name=type>{% for t in types %}<option value="{{t}}">{{ t }}</option>{% endfor %}</select>
 <label>Titel *</label><input name=title maxlength=120 required>
 <label>Beschreibung</label><textarea name=body rows=6 maxlength=4000></textarea>
 <label>Dein Name (optional)</label><input name=submitter_name maxlength=40 placeholder="anonym">
 <input class=hp type=text name=website tabindex=-1 autocomplete=off>
 <div style="margin-top:14px"><button class=btn>Absenden</button> <a href="/">Abbrechen</a></div>
</form>{% endblock %}"""

DETAIL = """{% extends "base" %}{% block body %}
<p><a href="/">← Board</a></p>
<span class="tag {{c.type}}">{{ c.type }}</span>{% if c.component %}<span class=tag>{{ c.component }}</span>{% endif %}
{% if c.priority %}<span class=tag>{{ c.priority }}</span>{% endif %}<span class=tag>{{ c.status }}</span>
<h2 style="margin:8px 0">{{ c.title }}</h2>
<form method=post action="/upvote/{{c.id}}"><button class=up>▲ {{ c.upvotes }} Upvotes</button></form>
<p style="white-space:pre-wrap;margin-top:14px">{{ c.body }}</p>
<p class=muted>Eingereicht: {{ c.created_at }}{% if c.version %} · erledigt in {{ c.version }}{% endif %}</p>{% endblock %}"""

LOGIN = """{% extends "base" %}{% block body %}
<h2>Owner-Login</h2><form method=post action="/admin/login" style="max-width:340px">
 <label>Admin-Token</label><input name=token type=password autofocus>
 <div style="margin-top:12px"><button class=btn>Anmelden</button></div></form>{% endblock %}"""

ADMIN = """{% extends "base" %}{% block body %}
<h2>🛡️ Moderations-Queue ({{ queue|length }})</h2>
{% if not queue %}<p class=muted>Nichts zu prüfen.</p>{% endif %}
{% for c in queue %}<div class=card><span class="tag {{c.type}}">{{ c.type }}</span> <b>{{ c.title }}</b>
 <div class=muted>{{ c.body }}</div>
 <form method=post action="/admin/{{c.id}}/approve" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small">✔ Freigeben</button></form>
 <form method=post action="/admin/{{c.id}}/reject" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small red">✖ Ablehnen</button></form>
 <form method=post action="/admin/{{c.id}}/delete" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small grey">🗑 Löschen</button></form>
</div>{% endfor %}
<h2 style="margin-top:24px">Alle Einträge ({{ items|length }})</h2>
<table><tr><th>#</th><th>Titel</th><th>Status / Prio / Komponente / Version</th><th>▲</th><th></th></tr>
{% for c in items if c.status!='pending' %}<tr><td>{{c.id}}</td>
 <td><span class="tag {{c.type}}">{{c.type}}</span> {{ c.title }}</td>
 <td><form method=post action="/admin/{{c.id}}/status"><input type=hidden name=csrf value="{{csrf}}"><div style="display:flex;gap:6px;flex-wrap:wrap">
   <select name=status>{% for s in statuses %}<option value="{{s}}" {{'selected' if s==c.status}}>{{s}}</option>{% endfor %}</select>
   <select name=priority>{% for p in priorities %}<option value="{{p}}" {{'selected' if p==c.priority}}>{{p or '–'}}</option>{% endfor %}</select>
   <select name=component>{% for k in components %}<option value="{{k}}" {{'selected' if k==c.component}}>{{k or '–'}}</option>{% endfor %}</select>
   <input name=version value="{{c.version}}" placeholder="Version" style="width:90px">
   <button class="btn small">Speichern</button></div></form></td>
 <td>{{ c.upvotes }}</td>
 <td><form method=post action="/admin/{{c.id}}/delete"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small grey">🗑</button></form></td></tr>
{% endfor %}</table>
<h3 style="margin-top:24px">📥 CSV-Import (rückwirkende Historie)</h3>
<form method=post action="/admin/import" enctype="multipart/form-data"><input type=hidden name=csrf value="{{csrf}}">
 <input type=file name=file accept=".csv"> <button class="btn small">Importieren</button>
 <div class=muted>Spalten: type,title,body,status,component,priority,version,created_at,source</div></form>
{% endblock %}"""

ENV = Environment(loader=DictLoader({"base": BASE, "board": BOARD, "submit": SUBMIT,
                                     "detail": DETAIL, "login": LOGIN, "admin": ADMIN}),
                  autoescape=select_autoescape(["html", "xml"], default=True))

_ROWQ = ("SELECT s.*, (SELECT COUNT(*) FROM board_votes v WHERE v.submission_id=s.id) AS upvotes "
         "FROM board_submissions s ")


def _render(req, name, title="Board", flash="", **ctx):
    html = ENV.get_template(name).render(title=title, flash=flash, admin=_is_admin(req), **ctx)
    return web.Response(text=html, content_type="text/html")


async def _rows(where="", params=()):
    return [dict(r) for r in await board_query(_ROWQ + where, params)]


async def _one(sid):
    r = await board_one(_ROWQ + "WHERE s.id=?", (sid,))
    return dict(r) if r else None


# ── Handlers ──────────────────────────────────────────────────────────────────
async def h_board(req):
    items = await _rows("WHERE status!='pending' ORDER BY id DESC")
    return _render(req, "board", items=items, cols=PUBLIC_COLS, flash=req.query.get("m", ""))


async def h_submit_form(req):
    return _render(req, "submit", title="Einreichen", types=TYPES)


async def h_submit(req):
    d = await req.post()
    if (d.get("website") or "").strip():
        raise web.HTTPFound("/?m=Danke, wird geprüft.")
    if not _rate("submit:" + _ip(req), RATE_SUBMIT_PER_H, 3600):
        raise web.HTTPFound("/?m=Zu viele Einreichungen – bitte später erneut.")
    title = (d.get("title") or "").strip()[:120]
    if not title:
        return _render(req, "submit", title="Einreichen", types=TYPES, flash="Titel fehlt.")
    sh = _hmac("submit", _ip(req))
    n = await board_one("SELECT COUNT(*) AS n FROM board_submissions WHERE submitter_hash=? "
                        "AND created_at > datetime('now','-1 hour')", (sh,))
    if n and n["n"] >= RATE_SUBMIT_PER_H:
        raise web.HTTPFound("/?m=Zu viele Einreichungen – bitte später erneut.")
    typ = d.get("type") if d.get("type") in TYPES else "idea"
    sid = await board_exec(
        "INSERT INTO board_submissions (type,title,body,submitter_hash,submitter_name,status,source) "
        "VALUES (?,?,?,?,?, 'pending','public')",
        (typ, title, (d.get("body") or "").strip()[:4000], sh, (d.get("submitter_name") or "").strip()[:40]))
    sub = await _one(sid)
    await notify_owner(req.app, sub)
    raise web.HTTPFound("/?m=Danke! Deine Einreichung wird geprüft und erscheint dann öffentlich.")


async def h_upvote(req):
    sid = int(req.match_info["id"])
    resp = web.HTTPFound(req.headers.get("Referer", "/"))
    if not _rate("vote:" + _ip(req), 30, 300):
        raise resp
    sub = await _one(sid)
    if not sub or sub["status"] == "pending":
        raise resp
    vid = req.cookies.get(_VOTER_COOKIE)
    if not vid:
        vid = secrets.token_hex(8)
        resp.set_cookie(_VOTER_COOKIE, vid, max_age=31536000, httponly=True, samesite="Lax")
    await board_exec("INSERT OR IGNORE INTO board_votes (submission_id, voter_hash) VALUES (?,?)",
                     (sid, _hmac("vote", _ip(req), vid)))
    raise resp


async def h_detail(req):
    sub = await _one(int(req.match_info["id"]))
    if not sub or (sub["status"] == "pending" and not _is_admin(req)):
        raise web.HTTPFound("/")
    return _render(req, "detail", title=sub["title"], c=sub)


# ── Admin ─────────────────────────────────────────────────────────────────────
async def h_login_form(req):
    return _render(req, "login", title="Login")


async def h_login(req):
    d = await req.post()
    if BOARD_ADMIN_TOKEN and hmac.compare_digest((d.get("token") or ""), BOARD_ADMIN_TOKEN):
        resp = web.HTTPFound("/admin")
        resp.set_cookie(_ADMIN_COOKIE, _hmac("owner", BOARD_ADMIN_TOKEN),
                        max_age=604800, httponly=True, samesite="Lax")
        resp.set_cookie(_CSRF_COOKIE, secrets.token_hex(16), max_age=604800, samesite="Lax")
        raise resp
    return _render(req, "login", title="Login", flash="Falsches Token.")


async def h_logout(req):
    resp = web.HTTPFound("/")
    resp.del_cookie(_ADMIN_COOKIE)
    raise resp


async def h_admin(req):
    if not _is_admin(req):
        raise web.HTTPFound("/admin/login")
    items = await _rows("ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC")
    queue = [c for c in items if c["status"] == "pending"]
    csrf = req.cookies.get(_CSRF_COOKIE) or secrets.token_hex(16)
    resp = _render(req, "admin", title="Admin", items=items, queue=queue, csrf=csrf,
                   statuses=STATUSES, priorities=PRIORITIES, components=COMPONENTS)
    if not req.cookies.get(_CSRF_COOKIE):
        resp.set_cookie(_CSRF_COOKIE, csrf, max_age=604800, samesite="Lax")
    return resp


async def _admin_guard(req):
    if not _is_admin(req):
        raise web.HTTPFound("/admin/login")
    d = await req.post()
    if not _csrf_ok(req, d):
        raise web.HTTPForbidden(text="CSRF-Token ungültig")
    return d


async def h_approve(req):
    await _admin_guard(req)
    await board_exec("UPDATE board_submissions SET status='open', approved_at=datetime('now'), "
                     "updated_at=datetime('now') WHERE id=? AND status='pending'", (int(req.match_info["id"]),))
    raise web.HTTPFound("/admin")


async def h_reject(req):
    await _admin_guard(req)
    await board_exec("UPDATE board_submissions SET status='rejected', updated_at=datetime('now') WHERE id=?",
                     (int(req.match_info["id"]),))
    raise web.HTTPFound("/admin")


async def h_status(req):
    d = await _admin_guard(req)
    st = d.get("status") if d.get("status") in STATUSES else None
    if st:
        appr = ", approved_at=COALESCE(approved_at, datetime('now'))" if st != "pending" else ""
        await board_exec(f"UPDATE board_submissions SET status=?, priority=?, component=?, version=?, "
                         f"updated_at=datetime('now'){appr} WHERE id=?",
                         (st, d.get("priority", ""), d.get("component", ""), d.get("version", ""),
                          int(req.match_info["id"])))
    raise web.HTTPFound("/admin")


async def h_delete(req):
    await _admin_guard(req)
    sid = int(req.match_info["id"])
    await board_exec("DELETE FROM board_submissions WHERE id=?", (sid,))
    await board_exec("DELETE FROM board_votes WHERE submission_id=?", (sid,))
    raise web.HTTPFound("/admin")


async def h_import(req):
    d = await _admin_guard(req)
    f = d.get("file")
    if not f or not hasattr(f, "file"):
        raise web.HTTPFound("/admin")
    text = f.file.read().decode("utf-8", "replace")
    n = 0
    for r in _csv.DictReader(io.StringIO(text)):
        title = (r.get("title") or "").strip()
        if not title:
            continue
        st = (r.get("status") or "done").strip()
        appr = None if st == "pending" else "datetime('now')"
        await board_exec(
            "INSERT INTO board_submissions (type,title,body,status,component,priority,version,source,approved_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?, " + ("datetime('now')" if appr else "NULL") + ", COALESCE(?, datetime('now')))",
            ((r.get("type") or "idea").strip()[:20], title[:120], (r.get("body") or "").strip()[:4000],
             st, (r.get("component") or "").strip(), (r.get("priority") or "").strip(),
             (r.get("version") or "").strip(), (r.get("source") or "import").strip(),
             (r.get("created_at") or "").strip() or None))
        n += 1
    raise web.HTTPFound(f"/?m={n} Einträge importiert.")


async def notify_owner(app, sub: dict) -> None:
    """Private DM an den Owner bei neuer Einreichung. Kein Crash, wenn OWNER_ID/Bot fehlt."""
    bot = app.get("bot")
    if not BOARD_OWNER_ID or bot is None:
        logger.warning("🔔 Neue Board-Einreichung #%s (%s) – Owner-DM übersprungen "
                       "(BOARD_OWNER_ID nicht gesetzt).", sub["id"], sub["type"])
        return
    try:
        user = await bot.fetch_user(BOARD_OWNER_ID)
        e = discord.Embed(title=f"🗳️ Neue Board-Einreichung: {sub['title'][:230]}",
                          description=(sub["body"] or "")[:1500], color=0x00BFA5)
        e.add_field(name="Typ", value=sub["type"])
        e.add_field(name="Von", value=sub.get("submitter_name") or "anonym")
        if BOARD_PUBLIC_URL:
            e.add_field(name="Prüfen", value=f"{BOARD_PUBLIC_URL}/admin", inline=False)
        await user.send(embed=e)
    except discord.Forbidden:
        logger.warning("🔔 Owner-DM blockiert (DMs zu?) – Einreichung #%s", sub["id"])
    except Exception as ex:
        logger.error("❌ Owner-DM fehlgeschlagen: %s", ex)


def build_app(bot) -> web.Application:
    app = web.Application(client_max_size=1024*1024)
    app["bot"] = bot
    app.add_routes([
        web.get("/", h_board), web.get("/submit", h_submit_form), web.post("/submit", h_submit),
        web.post("/upvote/{id}", h_upvote), web.get("/submission/{id}", h_detail),
        web.get("/admin/login", h_login_form), web.post("/admin/login", h_login),
        web.get("/admin/logout", h_logout), web.get("/admin", h_admin),
        web.post("/admin/{id}/approve", h_approve), web.post("/admin/{id}/reject", h_reject),
        web.post("/admin/{id}/status", h_status), web.post("/admin/{id}/delete", h_delete),
        web.post("/admin/import", h_import),
    ])
    return app


class BoardCog(commands.Cog, name="Board"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.runner: web.AppRunner | None = None
        if BOARD_ENABLED:
            self._task = bot.loop.create_task(self._start())

    async def _start(self):
        await self.bot.wait_until_ready()
        if not BOARD_ADMIN_TOKEN:
            logger.warning("⚠️ Board aktiv, aber BOARD_ADMIN_TOKEN leer – Owner-Login unmöglich.")
        if not BOARD_OWNER_ID:
            logger.warning("⚠️ Board aktiv, aber BOARD_OWNER_ID=0 – Owner-DMs werden übersprungen.")
        try:
            await board_init()
            self.runner = web.AppRunner(build_app(self.bot))
            await self.runner.setup()
            await web.TCPSite(self.runner, BOARD_BIND, BOARD_PORT).start()
            logger.info("🌐 Feedback-Board läuft auf http://%s:%d (öffentlich: %s)",
                        BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL or "—")
        except Exception as e:
            logger.error("❌ Board-Start fehlgeschlagen: %s", e, exc_info=True)

    def cog_unload(self):
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())


def setup(bot: discord.Bot):
    bot.add_cog(BoardCog(bot))
