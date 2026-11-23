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
web/features/events.py – Karten-Events: Termine, Wiederholungen, Zusagen (RSVP) und ICS-Kalender.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import hmac
from urllib.parse import urlparse
from aiohttp import web
from datetime import datetime, timedelta, timezone
from utils.db import execute_db
from utils.timez import BERLIN
from utils.board_i18n import pick_lang, translate
from config import BOARD_PUBLIC_URL

from utils.map_events import _event_local, _event_next, _isoparse
from web.core.security import _aam_member, _hmac, _map_rate_ok, _member_csrf, _member_name, _rate
from utils.web_keys import BOT_KEY


# ════════════════════════════════════════════════════════════════════════════
#  HALTER-KARTE (Map-Feature) – Web-Teil: OAuth-Login, Seite, JSON-APIs
# ════════════════════════════════════════════════════════════════════════════



async def _approved_events(app):
    rows = await execute_db(app[BOT_KEY],
        "SELECT * FROM map_events WHERE status='approved'", fetch=True) or []
    out = []
    for r in rows:
        nxt = _event_next(r)
        if not nxt:
            continue
        out.append((nxt, r))
    out.sort(key=lambda t: t[0])
    return out


def _occ_date(nxt) -> str:
    """Datum (Berliner Zeit) des Vorkommens – Schlüssel für die Teilnahme."""
    return nxt.astimezone(BERLIN).strftime("%Y-%m-%d")


async def _rsvp_info(app, keys, me):
    """{(event_id, occ): {"count", "going", "names"}} für die angefragten Vorkommen."""
    out = {k: {"count": 0, "going": False, "names": []} for k in keys}
    if not keys:
        return out
    oldest = min(k[1] for k in keys)
    rows = await execute_db(app[BOT_KEY],
        "SELECT event_id, occ_date, user_id FROM map_event_rsvp WHERE occ_date >= ? "
        "ORDER BY created_at", (oldest,), fetch=True) or []
    for r in rows:
        k = (r["event_id"], r["occ_date"])
        if k not in out:
            continue
        name = _member_name(app, r["user_id"])
        if not name:                                  # kein Mitglied mehr -> nicht mitzählen
            continue
        out[k]["count"] += 1
        out[k]["names"].append(name)
        if me and str(r["user_id"]) == str(me):
            out[k]["going"] = True
    return out


async def h_map_events(req):
    """ÖFFENTLICH: kommende, freigegebene Events (+ Teilnahme: Anzahl öffentlich,
    Namen und eigener Status nur für eingeloggte Mitglieder)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    evs = await _approved_events(req.app)
    me = _aam_member(req)
    rsvp = await _rsvp_info(req.app, [(r["id"], _occ_date(n)) for n, r in evs], me)
    data = []
    for nxt, r in evs:
        end_iso, end_has_time = "", False
        try:
            st = _event_local(r["start_at"])
            en = _event_local(r["end_at"]) if r["end_at"] else None
        except Exception:
            st = en = None
        if st and en and en >= st:
            end_has_time = bool(en.hour or en.minute) and not r["all_day"]
            end_iso = (nxt + (en - st)).isoformat()
        data.append({
            "all_day": bool(r["all_day"]), "end": end_iso, "end_has_time": end_has_time,
            "id": r["id"], "title": r["title"], "type": r["type"],
            "country": r["country"] or "", "lat": r["lat"], "lon": r["lon"],
            "venue": r["venue"] or "", "url": r["url"] or "",
            "description": r["description"] or "",
            "next": nxt.isoformat(), "recurring": bool(r["rrule"]),
            "occ": _occ_date(nxt),
            "going_count": rsvp[(r["id"], _occ_date(nxt))]["count"],
            "going": rsvp[(r["id"], _occ_date(nxt))]["going"] if me else False,
            "going_names": rsvp[(r["id"], _occ_date(nxt))]["names"] if me else [],
        })
    return web.json_response({"events": data}, headers={"Cache-Control": "no-store"})


async def h_map_event_rsvp(req):
    """Mitglieder: Teilnahme am NÄCHSTEN Vorkommen eines Events an/aus (Toggle)."""
    uid = _aam_member(req)
    if not uid:
        raise web.HTTPForbidden(text="login required")
    if not hmac.compare_digest(req.headers.get("X-Map-CSRF", ""), _member_csrf(uid)):
        raise web.HTTPForbidden(text="bad csrf")
    if not _rate("rsvp:" + _hmac("rsvp", uid), 30, 300):
        raise web.HTTPTooManyRequests(text="rate limited")
    try:
        eid = int(req.match_info["eid"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()
    rows = await execute_db(req.app[BOT_KEY],
        "SELECT * FROM map_events WHERE id=? AND status='approved'", (eid,), fetch=True) or []
    nxt = _event_next(rows[0]) if rows else None
    if not nxt:
        raise web.HTTPNotFound()
    occ = _occ_date(nxt)
    bot = req.app[BOT_KEY]
    have = await execute_db(bot,
        "SELECT 1 FROM map_event_rsvp WHERE event_id=? AND occ_date=? AND user_id=?",
        (eid, occ, str(uid)), fetch=True)
    if have:
        await execute_db(bot, "DELETE FROM map_event_rsvp WHERE event_id=? AND occ_date=? AND user_id=?",
                         (eid, occ, str(uid)), commit=True)
    else:
        await execute_db(bot, "INSERT OR IGNORE INTO map_event_rsvp (event_id, occ_date, user_id) "
                              "VALUES (?,?,?)", (eid, occ, str(uid)), commit=True)
    info = (await _rsvp_info(req.app, [(eid, occ)], uid))[(eid, occ)]
    return web.json_response({"occ": occ, "going": info["going"], "going_count": info["count"],
                              "going_names": info["names"]}, headers={"Cache-Control": "no-store"})


def _ics_escape(s: str) -> str:
    """TEXT-Werte nach RFC 5545 §3.3.11 maskieren."""
    return ((s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """Zeilen > 75 Oktette falten (RFC 5545 §3.1), ohne UTF-8-Zeichen zu zerteilen."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        limit = 75 if not out else 74          # Folgezeilen beginnen mit einem Leerzeichen
        if size + n > limit:
            out.append(cur); cur, size = "", 0
        cur += ch; size += n
    out.append(cur)
    return "\r\n ".join(out)


# Alle Event-Zeiten werden als Berliner Ortszeit gespeichert (naiv). Damit Serien auch über
# die Sommer-/Winterzeit-Umstellung zur richtigen Uhrzeit liegen, wird TZID=Europe/Berlin
# mit passender VTIMEZONE ausgeliefert (statt UTC oder "floating time").
_ICS_TZID = "Europe/Berlin"
_ICS_VTIMEZONE = [
    "BEGIN:VTIMEZONE", f"TZID:{_ICS_TZID}",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST",
    "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET",
    "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE",
]
_ICS_REFRESH = "PT12H"   # Empfohlenes Abruf-Intervall für Kalender-Abos


def _naive(s):
    """ISO-String -> naive datetime (Berliner Ortszeit) oder None."""
    if not s:
        return None
    try:
        d = _isoparse(str(s))
    except Exception:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(BERLIN).replace(tzinfo=None)
    return d


def _ics_calendar_url(req) -> str:
    """Absolute https-URL des Feeds (BOARD_PUBLIC_URL bevorzugt, sonst aus dem Request)."""
    base = BOARD_PUBLIC_URL or f"{req.scheme}://{req.host}"
    return f"{base}/map/events.ics"


def _ics_build(req, rows, feed: bool = True, name: str | None = None, source: str | None = None) -> bytes:
    """iCalendar (RFC 5545) für die übergebenen Event-Zeilen.
    feed=True: abonnierbarer Feed (Name, Abruf-Intervall, Quelle); False: Einzeltermin-Download.

    - Ganztägige Events als DATE (DTEND exklusiv = Folgetag des letzten Tages).
    - Termine mit Uhrzeit in Europe/Berlin (TZID + VTIMEZONE), Serien via RRULE/EXDATE.
    - Mit Endzeit -> echtes DTEND; nur Enddatum (ohne Endzeit) -> Ende = 23:59 des letzten Tages.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = urlparse(BOARD_PUBLIC_URL).hostname if BOARD_PUBLIC_URL else (req.host or "board").split(":")[0]
    lang = pick_lang(req)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AAM-Bot//Map//DE",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    if feed:
        cal = name or "AAM – Termine"
        lines += [f"NAME:{_ics_escape(cal)}", f"X-WR-CALNAME:{_ics_escape(cal)}",
                  f"X-WR-TIMEZONE:{_ICS_TZID}",
                  f"REFRESH-INTERVAL;VALUE=DURATION:{_ICS_REFRESH}",
                  f"X-PUBLISHED-TTL:{_ICS_REFRESH}",
                  f"SOURCE;VALUE=URI:{source or _ics_calendar_url(req)}"]
    lines += _ICS_VTIMEZONE
    dfmt, tfmt = "%Y%m%d", "%Y%m%dT%H%M%S"
    for r in rows:
        start = _naive(r["start_at"])
        if start is None:
            continue
        end = _naive(r["end_at"])
        all_day = bool(r["all_day"])
        ev = ["BEGIN:VEVENT", f"UID:aam-event-{r['id']}@{host}", f"DTSTAMP:{stamp}",
              f"SUMMARY:{_ics_escape(r['title'])}"]
        if all_day:
            last = end.date() if end and end.date() >= start.date() else start.date()
            ev += [f"DTSTART;VALUE=DATE:{start.strftime(dfmt)}",
                   f"DTEND;VALUE=DATE:{(last + timedelta(days=1)).strftime(dfmt)}"]
        else:
            ev.append(f"DTSTART;TZID={_ICS_TZID}:{start.strftime(tfmt)}")
            fin = None
            if end and (end.hour or end.minute) and end > start:
                fin = end                                              # echte Endzeit
            elif end and end.date() > start.date():
                fin = end.replace(hour=23, minute=59, second=0)        # nur Enddatum bekannt
            if fin:
                ev.append(f"DTEND;TZID={_ICS_TZID}:{fin.strftime(tfmt)}")
            # ohne Ende -> kein DTEND (RFC 5545: Ende = Beginn)
        if r["rrule"]:
            ev.append(f"RRULE:{r['rrule']}")
            exs = [d for d in (_naive(x.strip()) for x in (r["exdates"] or "").split(",")) if d]
            if exs:
                if all_day:
                    ev.append("EXDATE;VALUE=DATE:" + ",".join(d.strftime(dfmt) for d in exs))
                else:
                    ev.append(f"EXDATE;TZID={_ICS_TZID}:" + ",".join(
                        d.replace(hour=start.hour, minute=start.minute, second=start.second).strftime(tfmt)
                        for d in exs))
        loc = r["venue"] or ""
        if r["plz"] and r["plz"] not in loc:
            loc = f"{loc}, {r['plz']}" if loc else r["plz"]
        if r["country"]:
            loc = f"{loc} ({r['country'].upper()})" if loc else r["country"].upper()
        if loc:
            ev.append(f"LOCATION:{_ics_escape(loc)}")
        if r["lat"] is not None and r["lon"] is not None:
            ev.append(f"GEO:{float(r['lat']):.6f};{float(r['lon']):.6f}")
        ev.append(f"CATEGORIES:{_ics_escape(translate(lang, 'map_evtype_' + (r['type'] or 'other')))}")
        if r["url"]:
            ev.append(f"URL:{r['url']}")
        desc = r["description"] or ""
        if r["url"]:
            desc = (desc + "\n\n" if desc else "") + r["url"]
        if desc:
            ev.append(f"DESCRIPTION:{_ics_escape(desc)}")
        ev += ["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"]
        lines += ev
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_ics_fold(l) for l in lines) + "\r\n").encode("utf-8")


async def h_map_events_ics(req):
    """Abonnierbarer ICS-Feed aller freigegebenen Events."""
    rows = await execute_db(req.app[BOT_KEY],
        "SELECT * FROM map_events WHERE status='approved' ORDER BY start_at", fetch=True) or []
    return web.Response(body=_ics_build(req, rows, feed=True),
                        headers={"Content-Type": "text/calendar; charset=utf-8",
                                 "Content-Disposition": "inline; filename=aam-events.ics",
                                 "Cache-Control": "public, max-age=3600"})


async def h_map_event_ics(req):
    """Einzelner freigegebener Termin als .ics-Download („In Kalender“)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    try:
        eid = int(req.match_info["eid"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()
    rows = await execute_db(req.app[BOT_KEY],
        "SELECT * FROM map_events WHERE id=? AND status='approved'", (eid,), fetch=True) or []
    if not rows:
        raise web.HTTPNotFound()
    return web.Response(body=_ics_build(req, rows, feed=False),
                        headers={"Content-Type": "text/calendar; charset=utf-8",
                                 "Content-Disposition": f"attachment; filename=aam-event-{eid}.ics",
                                 "Cache-Control": "public, max-age=3600"})
