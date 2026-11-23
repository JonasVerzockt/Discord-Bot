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
web/features/feedback.py – Feedback-Board: Liste, Einreichen, Upvotes, Detailseite sowie Board-Admin (Moderation, Import).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import csv as _csv
import io
import secrets
from urllib.parse import urlparse
import discord
from aiohttp import web
from utils.board_db import board_exec, board_one, board_query
from utils.timez import now_berlin
from utils.board_i18n import flash_text, pick_lang, translate
from config import BOARD_OWNER_ID, BOARD_PUBLIC_URL, VERSION

from utils.health import _collect_health, _metrics_chip, _system_metrics
from web.core.render import _render, _ver_key
from web.core.security import (
    COOKIE_SECURE, _ADMIN_COOKIE, _admin_guard, _admin_ids, _admin_login_url, _csrf_token, _hmac, _ip,
    _is_admin, _is_member, _rate, _safe_local_redirect, _VOTER_COOKIE,
)
from utils.web_keys import BOT_KEY

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


TYPES       = ["bug", "feature", "idea"]
STATUSES    = ["pending", "open", "planned", "in_progress", "done", "rejected", "duplicate"]
# (Status-Schlüssel, i18n-Schlüssel für die Spaltenüberschrift) – Label via t() im Template.
PUBLIC_COLS = [("open", "col_open"), ("planned", "col_planned"),
               ("in_progress", "col_in_progress"), ("done", "col_done"),
               ("rejected", "col_rejected")]
PRIORITIES  = ["", "P0", "P1", "P2", "P3"]
COMPONENTS  = ["", "Preis-Tracking", "Benachrichtigungen", "Shop-Suche/Grabber", "KI-Chat",
               "Digest", "iNat", "Rabattcodes", "Review-Bot", "Erfolge", "Moderation",
               "Infra/Deploy", "UI", "Lokalisierung", "Doku", "Sonstiges"]
RATE_SUBMIT_PER_H = 5

_ROWQ = ("SELECT s.*, "
         "(SELECT COUNT(*) FROM board_votes v WHERE v.submission_id=s.id) AS upvotes, "
         "(SELECT COUNT(*) FROM board_comments c WHERE c.submission_id=s.id) AS comments "
         "FROM board_submissions s ")


async def _rows(where="", params=()):
    return [dict(r) for r in await board_query(_ROWQ + where, params)]


async def _one(sid):
    r = await board_one(_ROWQ + "WHERE s.id=?", (sid,))
    return dict(r) if r else None


async def _comments(sid):
    rows = await board_query(
        "SELECT * FROM board_comments WHERE submission_id=? ORDER BY id ASC", (sid,))
    return [dict(r) for r in rows]


async def h_board(req):
    lang = pick_lang(req)
    items = await _rows("WHERE status!='pending' ORDER BY id DESC")
    # 'Erledigt'-Karten tragen eine Version -> nach Version absteigend (neueste oben);
    # alle anderen Spalten haben keine Version ((0,0,0,0)) und bleiben so bei id DESC.
    items.sort(key=lambda c: (_ver_key(c.get("version") or ""), c.get("id") or 0), reverse=True)
    overall, sections = await _collect_health(req.app[BOT_KEY], lang)
    flash = flash_text(lang, req.query.get("m", ""), n=req.query.get("n", ""), s=req.query.get("s", ""))
    resp = _render(req, "board", title=translate(lang, "nav_board"), items=items, cols=PUBLIC_COLS,
                   overall=overall, sections=sections, version=VERSION,
                   generated=now_berlin("%H:%M:%S"), metrics_html=_metrics_chip(_system_metrics()),
                   flash=flash)
    resp.headers["Cache-Control"] = "no-store"   # kein veraltetes HTML aus Proxy/Browser-Cache
    return resp


async def h_submit_form(req):
    return _render(req, "submit", title=translate(pick_lang(req), "submit_h"), types=TYPES)


async def h_submit(req):
    lang = pick_lang(req)
    d = await req.post()
    if (d.get("website") or "").strip():
        raise web.HTTPFound(f"/?m=thanks_review&lang={lang}")
    if not _rate("submit:" + _ip(req), RATE_SUBMIT_PER_H, 3600):
        raise web.HTTPFound(f"/?m=too_many&lang={lang}")
    title = (d.get("title") or "").strip()[:120]
    if not title:
        return _render(req, "submit", title=translate(lang, "submit_h"), types=TYPES,
                       flash=translate(lang, "flash_title_missing"))
    sh = _hmac("submit", _ip(req))
    n = await board_one("SELECT COUNT(*) AS n FROM board_submissions WHERE submitter_hash=? "
                        "AND created_at > datetime('now','-1 hour')", (sh,))
    if n and n["n"] >= RATE_SUBMIT_PER_H:
        raise web.HTTPFound(f"/?m=too_many&lang={lang}")
    typ = d.get("type") if d.get("type") in TYPES else "idea"
    sid = await board_exec(
        "INSERT INTO board_submissions (type,title,body,submitter_hash,submitter_name,status,source) "
        "VALUES (?,?,?,?,?, 'pending','public')",
        (typ, title, (d.get("body") or "").strip()[:4000], sh, (d.get("submitter_name") or "").strip()[:40]))
    sub = await _one(sid)
    await notify_owner(req.app, sub)
    raise web.HTTPFound(f"/?m=submitted&lang={lang}")


async def h_upvote(req):
    sid = int(req.match_info["id"])
    # Open-Redirect-Schutz (CWE-601): Referer nur als Redirect-Ziel zulassen, wenn
    # er KEINEN Host/kein Schema enthält (also seitenintern ist). Backslashes werden
    # entfernt und der geprüfte Originalstring durchgereicht – exakt das von CodeQL
    # empfohlene urlparse-Muster (py/url-redirection).
    target = (req.headers.get("Referer") or "/").replace("\\", "")
    if urlparse(target).netloc or urlparse(target).scheme or not target.startswith("/"):
        target = "/"
    resp = web.HTTPFound(target)
    if not _rate("vote:" + _ip(req), 30, 300):
        raise resp
    sub = await _one(sid)
    if not sub or sub["status"] == "pending":
        raise resp
    vid = req.cookies.get(_VOTER_COOKIE)
    if not vid:
        vid = secrets.token_hex(8)
        resp.set_cookie(_VOTER_COOKIE, vid, max_age=31536000, httponly=True, samesite="Lax", secure=COOKIE_SECURE)
    await board_exec("INSERT OR IGNORE INTO board_votes (submission_id, voter_hash) VALUES (?,?)",
                     (sid, _hmac("vote", _ip(req), vid)))
    raise resp


async def h_detail(req):
    sub = await _one(int(req.match_info["id"]))
    if not sub or (sub["status"] == "pending" and not _is_admin(req)):
        raise web.HTTPFound("/")
    comments = await _comments(sub["id"])
    return _render(req, "detail", title=sub["title"], c=sub, comments=comments)


async def h_login_form(req):
    """Owner-/Admin-Login nur noch über „Login mit Discord“ (kein Token mehr).
    Ist schon eine Mitglieder-Sitzung ohne Admin-Rechte da, wird das angezeigt statt
    erneut weiterzuleiten (keine Login-Schleife)."""
    lang = pick_lang(req)
    if _is_admin(req):
        raise web.HTTPFound(f"/admin?lang={lang}")
    url = _admin_login_url(lang)
    member = _is_member(req)
    if member and member in _admin_ids():          # Admin-Sitzung abgelaufen → neu anmelden
        member = None
    if url and not member and req.query.get("denied") is None:
        raise web.HTTPFound(url)
    flash = ""
    if member or req.query.get("denied") is not None:
        flash = translate(lang, "login_no_admin")
    elif not url:
        flash = translate(lang, "login_oauth_missing")
    return _render(req, "login", title=translate(lang, "login_h"), flash=flash, login_url=url)


async def h_logout(req):
    resp = web.HTTPFound("/")
    resp.del_cookie(_ADMIN_COOKIE)
    raise resp


async def h_admin(req):
    if not _is_admin(req):
        raise web.HTTPFound(f"/admin/login?lang={pick_lang(req)}")
    # Nach Status gruppiert (pending zuerst für die Queue, dann in Board-Spalten-
    # Reihenfolge), innerhalb eines Status nach ID (neueste zuerst).
    items = await _rows(
        "ORDER BY CASE status "
        "WHEN 'pending' THEN 0 WHEN 'open' THEN 1 WHEN 'planned' THEN 2 "
        "WHEN 'in_progress' THEN 3 WHEN 'done' THEN 4 WHEN 'rejected' THEN 5 "
        "WHEN 'duplicate' THEN 6 ELSE 7 END, id DESC"
    )
    queue = [c for c in items if c["status"] == "pending"]
    return _render(req, "admin", title=translate(pick_lang(req), "nav_admin"),
                   items=items, queue=queue, csrf=_csrf_token(req),
                   statuses=STATUSES, priorities=PRIORITIES, components=COMPONENTS)


async def h_approve(req):
    await _admin_guard(req)
    await board_exec("UPDATE board_submissions SET status='open', approved_at=datetime('now'), "
                     "updated_at=datetime('now') WHERE id=? AND status='pending'", (int(req.match_info["id"]),))
    raise web.HTTPFound(f"/admin?lang={pick_lang(req)}")


async def h_reject(req):
    await _admin_guard(req)
    await board_exec("UPDATE board_submissions SET status='rejected', updated_at=datetime('now') WHERE id=?",
                     (int(req.match_info["id"]),))
    raise web.HTTPFound(f"/admin?lang={pick_lang(req)}")


async def h_status(req):
    d = await _admin_guard(req)
    st = d.get("status") if d.get("status") in STATUSES else None
    if st:
        appr = ", approved_at=COALESCE(approved_at, datetime('now'))" if st != "pending" else ""
        await board_exec(f"UPDATE board_submissions SET status=?, priority=?, component=?, version=?, "
                         f"updated_at=datetime('now'){appr} WHERE id=?",
                         (st, d.get("priority", ""), d.get("component", ""), d.get("version", ""),
                          int(req.match_info["id"])))
    raise web.HTTPFound(f"/admin?lang={pick_lang(req)}")


async def h_delete(req):
    await _admin_guard(req)
    sid = int(req.match_info["id"])
    await board_exec("DELETE FROM board_submissions WHERE id=?", (sid,))
    await board_exec("DELETE FROM board_votes WHERE submission_id=?", (sid,))
    await board_exec("DELETE FROM board_comments WHERE submission_id=?", (sid,))
    raise web.HTTPFound(f"/admin?lang={pick_lang(req)}")


async def h_edit_form(req):
    """Editier-Seite eines Eintrags (Titel/Beschreibung/Meta) inkl. Kommentaren."""
    if not _is_admin(req):
        raise web.HTTPFound("/admin/login")
    sub = await _one(int(req.match_info["id"]))
    if not sub:
        raise web.HTTPFound(f"/admin?lang={pick_lang(req)}")
    comments = await _comments(sub["id"])
    return _render(req, "edit", title=translate(pick_lang(req), "edit_h", id=sub["id"]),
                   c=sub, comments=comments,
                   csrf=_csrf_token(req), types=TYPES, statuses=STATUSES,
                   priorities=PRIORITIES, components=COMPONENTS)


async def h_edit(req):
    """Speichert die bearbeiteten Felder eines Eintrags (inkl. Titel & Beschreibung)."""
    d = await _admin_guard(req)
    sid = int(req.match_info["id"])
    cur = await _one(sid)
    if not cur:
        raise web.HTTPFound(f"/admin?lang={pick_lang(req)}")
    title = (d.get("title") or "").strip()[:120]
    if not title:
        raise web.HTTPFound(f"/admin/{sid}/edit?lang={pick_lang(req)}")
    typ  = d.get("type") if d.get("type") in TYPES else cur["type"]
    st   = d.get("status") if d.get("status") in STATUSES else cur["status"]
    prio = d.get("priority") if d.get("priority") in PRIORITIES else ""
    comp = d.get("component") if d.get("component") in COMPONENTS else ""
    appr = ", approved_at=COALESCE(approved_at, datetime('now'))" if st != "pending" else ""
    await board_exec(
        f"UPDATE board_submissions SET type=?, title=?, body=?, status=?, priority=?, "
        f"component=?, version=?, updated_at=datetime('now'){appr} WHERE id=?",
        (typ, title, (d.get("body") or "").strip()[:4000], st, prio, comp,
         (d.get("version") or "").strip()[:40], sid))
    raise web.HTTPFound(f"/admin/{sid}/edit?lang={pick_lang(req)}")


async def h_comment_add(req):
    d = await _admin_guard(req)
    sid = int(req.match_info["id"])
    body = (d.get("body") or "").strip()[:4000]
    if body:
        author = (d.get("author") or "Owner").strip()[:40] or "Owner"
        await board_exec("INSERT INTO board_comments (submission_id, author, body) VALUES (?,?,?)",
                         (sid, author, body))
    raise web.HTTPFound(f"/admin/{sid}/edit?lang={pick_lang(req)}")


async def h_comment_del(req):
    d = await _admin_guard(req)
    cid = int(req.match_info["cid"])
    sid = (d.get("sid") or "").strip()
    await board_exec("DELETE FROM board_comments WHERE id=?", (cid,))
    lang = pick_lang(req)
    raise web.HTTPFound(_safe_local_redirect(
        f"/admin/{int(sid)}/edit?lang={lang}" if sid.isdigit() else f"/admin?lang={lang}"))


def _parse_import_rows(text: str):
    """Parst CSV-Text robust und normalisiert auf gültige, ANZEIGBARE Werte.

    Toleranzen (häufige stille Import-Fehler):
      • BOM (utf-8-sig) und Feldnamen case-/whitespace-tolerant,
      • Trennzeichen automatisch erkannt (Komma / Semikolon / Tab) – nicht nur Komma,
      • unbekannter ``type`` → 'idea'; unbekannter ``status`` → 'open' (statt in KEINER
        Spalte zu landen und dadurch unsichtbar zu sein).

    Erwartete Spalten (Reihenfolge egal, Groß/klein egal):
      type,title,body,status,component,priority,version,created_at,source
    Pflicht ist nur ``title``. Rückgabe: (rows, skipped) mit rows als DB-fertige Dicts
    (inkl. ``_line``/``_note`` für Logging) und skipped als Liste (Zeile, Grund)."""
    rows, skipped = [], []
    if not text.strip():
        return rows, skipped
    header = text.splitlines()[0].lstrip("﻿")
    counts = {",": header.count(","), ";": header.count(";"), "\t": header.count("\t")}
    delim = max(counts, key=counts.get) if max(counts.values()) else ","
    reader = _csv.DictReader(io.StringIO(text), delimiter=delim)
    if reader.fieldnames:
        reader.fieldnames = [(fn or "").strip().lstrip("﻿").lower() for fn in reader.fieldnames]
    for i, r in enumerate(reader, start=2):  # Zeile 1 = Header
        g = lambda k: (r.get(k) or "").strip()
        title = g("title")
        if not title:
            skipped.append((i, "kein Titel – Spalte 'title' nicht erkannt (falsches Trennzeichen?)"))
            continue
        typ = g("type").lower() or "idea"
        if typ not in TYPES:
            typ = "idea"
        st = g("status").lower() or "done"
        note = ""
        if st not in STATUSES:
            note = f"Status '{st}' unbekannt → als 'open' importiert"
            st = "open"
        rows.append({
            "_line": i, "_note": note,
            "type": typ, "title": title[:120], "body": g("body")[:4000], "status": st,
            "component": g("component"), "priority": g("priority"), "version": g("version"),
            "source": g("source") or "import", "created_at": g("created_at") or None,
        })
    return rows, skipped


async def h_import(req):
    lang = pick_lang(req)
    d = await _admin_guard(req)
    f = d.get("file")
    if not f or not hasattr(f, "file"):
        raise web.HTTPFound(f"/?m=no_csv&lang={lang}")
    raw = f.file.read()
    text = raw.decode("utf-8-sig", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    rows, skipped = _parse_import_rows(text)
    for row in rows:
        appr = row["status"] != "pending"
        await board_exec(
            "INSERT INTO board_submissions (type,title,body,status,component,priority,version,source,approved_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?, " + ("datetime('now')" if appr else "NULL") + ", COALESCE(?, datetime('now')))",
            (row["type"], row["title"], row["body"], row["status"], row["component"],
             row["priority"], row["version"], row["source"], row["created_at"]))
    detail = list(skipped) + [(r["_line"], r["_note"]) for r in rows if r["_note"]]
    if detail:
        logger.warning("📥 Board-CSV-Import: %d importiert, %d übersprungen | %s",
                       len(rows), len(skipped),
                       "; ".join(f"Z{ln}: {rs}" for ln, rs in detail[:25]))
    raise web.HTTPFound(f"/?m=imported&n={len(rows)}&s={len(skipped)}&lang={lang}")


async def notify_owner(app, sub: dict) -> None:
    """Private DM an den Owner bei neuer Einreichung. Kein Crash, wenn OWNER_ID/Bot fehlt."""
    bot = app.get(BOT_KEY)
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
