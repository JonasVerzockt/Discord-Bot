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
web/core/static.py – Statische Auslieferung (/static, Favicon) und Access-Logger des Webservers.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from web.core.render import STATIC_DIR


# Strikte Allowlist auslieferbarer Dateien -> {Dateiname: Content-Type}. Neue Assets
# hier eintragen (der /static-Handler baut den Pfad nur aus diesen Literalen).
_STATIC_FILES = {
    "chart.umd.js": "application/javascript",
    "chartjs-chart-treemap.min.js": "application/javascript",
    "stats.js": "application/javascript",
    "intro.js": "application/javascript",     # Anlass-Intros (utils/intro.py)
    "map_stats.js": "application/javascript",
    # Halter-Karte: Leaflet self-hosted + Kartenlogik + GeoJSON-Layer.
    # Große Dateien stellt der Bot via utils/map_geodata.py bereit (fehlen -> 404, Seite
    # degradiert sauber). Alle Namen sind feste Literale (CodeQL path-injection safe).
    "leaflet.js": "application/javascript",
    "leaflet.css": "text/css",
    # Pin-Clustering (Leaflet.markercluster, MIT) – optional, ohne Datei ungeclustert.
    "leaflet.markercluster.js": "application/javascript",
    "MarkerCluster.css": "text/css",
    "MarkerCluster.Default.css": "text/css",
    "map.js": "application/javascript",
    # Stylesheets der Board-Seiten (aus den Templates ausgelagert, 5.1.0).
    "board.css": "text/css",
    "board-chat.css": "text/css",
    "board-help.css": "text/css",
    "board-konto.css": "text/css",
    "board-map.css": "text/css",
    "board-swarm.css": "text/css",
    "de_bundeslaender.geojson": "application/geo+json",
    "at_bundeslaender.geojson": "application/geo+json",
    "ch_kantone.geojson": "application/geo+json",
    "li_gemeinden.geojson": "application/geo+json",
    "li_land.geojson": "application/geo+json",
    "map_cities.json": "application/json",
    # Favicon: statisches PNG (alle Browser), animiertes GIF (nur Firefox animiert Favicons),
    # Homescreen-Icon für Smartphones.
    "favicon.png": "image/png",
    "favicon.gif": "image/gif",
    "apple-touch-icon.png": "image/png",
}


async def h_static(req):
    """Liefert vendored statische Assets (self-hosted Chart.js / stats.js) aus <repo>/static/.

    Sicherheit gegen Pfad-Traversal (CodeQL py/path-injection): strikte ALLOWLIST.
    Der Dateiname aus der URL wird nur mit festen Literalen verglichen; der Pfad wird
    ausschließlich aus der Konstante gebaut (nie aus dem User-Wert) -> untainted.
    Neue Assets hier explizit eintragen."""
    name = req.match_info["name"]
    for fname, ct in _STATIC_FILES.items():
        if name == fname:
            p = STATIC_DIR / fname          # Pfad aus Literal, nicht aus dem User-Wert
            if not p.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(p, headers={"Cache-Control": "public, max-age=86400",
                                                "Content-Type": ct})
    raise web.HTTPNotFound()


class _DebugAccessLogger(AbstractAccessLogger):
    """Access-Log auf DEBUG statt INFO – haelt das normale (INFO-)Log frei vom
    HTTP-Grundrauschen (Scanner/Bots). Sichtbar nur, wenn der Loglevel DEBUG ist."""
    def log(self, request, response, time):
        self.logger.debug(
            '%s "%s %s" %s %s "%s"',
            getattr(request, "remote", "-"), request.method, request.path_qs,
            response.status, response.body_length,
            request.headers.get("User-Agent", "-"),
        )


# Rückfall-Favicon (SVG-Ameise), falls static/favicon.png fehlt. Das eigentliche Favicon
# liegt als static/favicon.png (+ animiertes favicon.gif für Firefox) im Repo.
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#e9a23b"/>'
    '<g stroke="#1b1b1b" stroke-width="3" stroke-linecap="round" fill="none">'
    '<path d="M28 34 L16 24 M28 36 L14 36 M28 38 L16 48"/>'
    '<path d="M36 34 L48 24 M36 36 L50 36 M36 38 L48 48"/>'
    '<path d="M46 30 L55 22 M46 32 L57 28"/></g>'
    '<g fill="#1b1b1b"><circle cx="20" cy="36" r="9"/>'
    '<circle cx="32" cy="36" r="6"/><circle cx="44" cy="34" r="7"/></g></svg>'
)


async def h_favicon(req):
    """/favicon.ico (wird von manchen Browsern direkt abgefragt): das PNG-Favicon,
    falls vorhanden, sonst das eingebaute SVG als Rückfallebene."""
    p = STATIC_DIR / "favicon.png"
    if p.is_file():
        return web.FileResponse(p, headers={"Cache-Control": "public, max-age=86400",
                                            "Content-Type": "image/png"})
    return web.Response(text=_FAVICON, content_type="image/svg+xml")
