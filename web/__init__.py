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
web/__init__.py – Web-Oberfläche des Bots (Feedback-Board, Karte, Konto, …) – aiohttp im Bot-Prozess.

build_app(bot) registriert alle Routen; gestartet wird der Server von cogs/board.py.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

from aiohttp import web

from web.core.static import h_favicon, h_static
from web.features.account import h_konto, h_konto_export, h_konto_ics, h_konto_post
from web.features.chat import h_chat_models, h_chat_send
from web.features.events import h_map_event_ics, h_map_event_rsvp, h_map_events, h_map_events_ics
from web.features.feedback import (
    h_admin, h_approve, h_board, h_comment_add, h_comment_del, h_delete, h_detail, h_edit,
    h_edit_form, h_import, h_login_form, h_logout, h_reject, h_status, h_submit,
    h_submit_form, h_upvote,
)
from web.features.help import h_hilfe, h_intro
from web.features.legal import (
    h_datenschutz, h_gewinnspiel, h_impressum, h_impressum_contact, h_partner_logo,
    h_partner_page,
)
from web.features.login import h_map_callback, h_map_login, h_map_logout
from web.features.map import h_map, h_map_list, h_map_pins, h_map_regions, h_map_stats
from web.features.rueckblick import h_rueckblick
from web.features.shops import h_shop, h_shops
from web.features.species import h_art, h_art_card, h_art_index, h_inat
from web.features.stats import h_stats
from web.features.status import h_incident_note, h_status_detail, h_status_json
from web.features.swarm import h_swarm, h_swarm_admin, h_swarm_fix, h_swarm_names, h_swarm_status
from utils.web_keys import BOT_KEY


def build_app(bot) -> web.Application:
    app = web.Application(client_max_size=1024*1024)
    app[BOT_KEY] = bot
    app.add_routes([
        web.get("/", h_board), web.get("/favicon.ico", h_favicon),
        web.get("/stats", h_stats), web.get("/static/{name}", h_static),
        web.get("/art", h_art_index), web.get(r"/art/{name}", h_art),
        web.get(r"/art/{name}/karte.png", h_art_card),
        web.get("/inat", h_inat),
        web.get("/shops", h_shops), web.get(r"/shop/{domain}", h_shop),
        web.get("/chat/models", h_chat_models), web.post("/chat/send", h_chat_send),
        web.get("/rueckblick", h_rueckblick), web.get(r"/rueckblick/{year:\d{4}}", h_rueckblick),
        web.get("/impressum", h_impressum), web.get("/datenschutz", h_datenschutz),
        web.get("/partner", h_partner_page), web.get("/gewinnspiel", h_gewinnspiel),
        web.post("/impressum/contact", h_impressum_contact),
        web.get("/status.json", h_status_json),
        web.get("/status/check/{key}", h_status_detail),
        web.post("/status/incident/{id}/note", h_incident_note),
        web.get("/submit", h_submit_form), web.post("/submit", h_submit),
        web.post("/upvote/{id}", h_upvote), web.get("/submission/{id}", h_detail),
        web.get("/admin/login", h_login_form),
        web.get("/admin/logout", h_logout), web.get("/admin", h_admin),
        web.post("/admin/{id}/approve", h_approve), web.post("/admin/{id}/reject", h_reject),
        web.post("/admin/{id}/status", h_status), web.post("/admin/{id}/delete", h_delete),
        web.get("/admin/{id}/edit", h_edit_form), web.post("/admin/{id}/edit", h_edit),
        web.post("/admin/{id}/comment", h_comment_add),
        web.post("/admin/comment/{cid}/delete", h_comment_del),
        web.post("/admin/import", h_import),
        # ── Halter-Karte ──────────────────────────────────────────────────────
        web.get("/map", h_map),
        web.get("/map/login", h_map_login), web.get("/map/callback", h_map_callback),
        web.get("/map/logout", h_map_logout),
        web.get("/map/regions.json", h_map_regions),
        web.get("/map/pins.json", h_map_pins),
        web.get("/map/list.json", h_map_list),
        web.get("/map/events.json", h_map_events),
        web.get("/map/stats.json", h_map_stats),
        web.get(r"/partner-logo/{name:[A-Za-z0-9_.-]+}", h_partner_logo),
        web.get("/map/events.ics", h_map_events_ics),
        web.get(r"/map/events/{eid:\d+}.ics", h_map_event_ics),
        web.post(r"/map/events/{eid:\d+}/rsvp", h_map_event_rsvp),
        # ── Mein Konto ────────────────────────────────────────────────────────
        web.get("/intro.json", h_intro),
        web.get("/hilfe", h_hilfe),
        web.get("/schwarmflug", h_swarm),
        web.get("/admin/schwarmflug", h_swarm_admin),
        web.post("/admin/schwarmflug/fix", h_swarm_fix),
        web.post("/admin/schwarmflug/names", h_swarm_names),
        web.get("/schwarmflug/status.json", h_swarm_status),
        web.get("/konto", h_konto),
        web.get("/konto/daten.json", h_konto_export),
        web.get(r"/konto/kalender/{uid:\d+}/{salt:[0-9a-f]{16}}.ics", h_konto_ics),
        web.post(r"/konto/{action:[a-z]+(?:/[a-z]+)?}", h_konto_post),
    ])
    return app
