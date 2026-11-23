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
web/features/status.py – Status-Dashboard: /status.json, Check-Details, Vorfall-Notizen (Logik in utils/health.py).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

from aiohttp import web
from utils.board_db import board_exec, board_query
from utils.timez import berlin_from_utc_naive, now_berlin
from utils.board_i18n import pick_lang
from config import VERSION

from utils.web_keys import BOT_KEY
from utils.health import _collect_health, _system_metrics
from web.core.render import _render
from web.core.security import _admin_guard, _csrf_token, _safe_local_redirect


async def h_status_json(req):
    """Nur die Health-Daten als JSON – fürs 5-Sekunden-Polling des Status-Bereichs
    (der Rest der Seite wird NICHT neu geladen)."""
    overall, sections = await _collect_health(req.app[BOT_KEY], pick_lang(req))
    return web.json_response(
        {"overall": overall, "version": VERSION, "sections": sections,
         "metrics": _system_metrics(), "generated": now_berlin("%H:%M:%S")},
        headers={"Cache-Control": "no-store"},
    )


async def h_status_detail(req):
    """Vorfall-Historie eines Health-Checks (Kachel-Klick). Zeigt die letzten 10
    'nicht OK'-Phasen; Admins können je Vorfall eine Notiz hinterlegen."""
    key = req.match_info["key"]
    try:
        _, sections = await _collect_health(req.app[BOT_KEY], pick_lang(req))
        current = next((c for sec in sections for c in sec["checks"] if c["name"] == key), None)
    except Exception:
        current = None
    rows = await board_query(
        "SELECT * FROM board_incidents WHERE check_key=? ORDER BY id DESC LIMIT 10", (key,))
    incidents = []
    for r in rows:
        d = dict(r)
        d["started_local"] = berlin_from_utc_naive(d["started_at"], "%Y-%m-%d %H:%M:%S")
        d["ended_local"] = berlin_from_utc_naive(d["ended_at"], "%Y-%m-%d %H:%M:%S") if d["ended_at"] else None
        incidents.append(d)
    return _render(req, "statusdetail", title=f"Status: {key}", key=key,
                   current=current, incidents=incidents, csrf=_csrf_token(req))


async def h_incident_note(req):
    d = await _admin_guard(req)
    cid = int(req.match_info["id"])
    key = (d.get("key") or "").strip()
    await board_exec("UPDATE board_incidents SET admin_note=? WHERE id=?",
                     ((d.get("note") or "").strip()[:500], cid))
    from urllib.parse import quote
    lang = pick_lang(req)
    raise web.HTTPFound(_safe_local_redirect(f"/status/check/{quote(key, safe='')}?lang={lang}") if key else f"/?lang={lang}")
