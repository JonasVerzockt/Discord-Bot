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
web/core/render.py – Rendering: Jinja2-Umgebung (Templates aus web/templates/), _render(), Filter, Asset-Versionen.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import re
from pathlib import Path
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape
from utils import board_swarm as SWB
from datetime import datetime
from utils.timez import berlin_from_utc_naive
from urllib.parse import urlencode
from utils.board_i18n import (
    FLAG_TITLE, FLAGS, LANGS, pick_lang, T as _BI18N_T, translate, type_label,
)
from config import BASE_DIR, VERSION

from web.core.partners import _load_partners
from web.core.security import _aam_member, _chat_ttl, _chat_uid, _is_admin, _member_csrf


# Vendored statische Assets (Chart.js self-hosted, kein CDN) unter <repo>/static/.
STATIC_DIR = BASE_DIR / "static"

_FILTER_JS = """<script>(function(){var inp=document.getElementById("dirfilter"),
 rows=document.querySelectorAll("#dirlist [data-q]"); if(!inp) return;
 function apply(){var q=inp.value.trim().toLowerCase();
  rows.forEach(function(r){r.style.display=(!q||r.getAttribute("data-q").indexOf(q)>=0)?"":"none";});}
 inp.addEventListener("input",apply); apply();})();</script>"""


class _TemplateLoader(FileSystemLoader):
    """Templates liegen als web/templates/<name>.html; angesprochen werden sie weiter
    ohne Endung („base“, „board“ …) – Namen in _render() und {% extends %} bleiben gleich."""

    def get_source(self, environment, template):
        if "." not in template.rsplit("/", 1)[-1]:
            template += ".html"
        return super().get_source(environment, template)


TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
ENV = Environment(loader=_TemplateLoader(str(TEMPLATE_DIR), encoding="utf-8"),
                  autoescape=select_autoescape(["html", "xml"], default=True))


def _switch_urls(req) -> dict:
    """Baut je Sprache die AKTUELLE URL mit gesetztem ?lang= – für den Flaggen-Umschalter
    im Header (bleibt auf derselben Seite, tauscht nur die Sprache)."""
    q = dict(req.query)
    out = {}
    for code in LANGS:
        q2 = dict(q)
        q2["lang"] = code
        out[code] = req.path + "?" + urlencode(q2)
    return out


def _render(req, name, title="Board", flash="", **ctx):
    lang = pick_lang(req)
    tt = lambda key, **kw: translate(lang, key, **kw)
    i18n = dict(lang=lang, t=tt, langs=LANGS, flags=FLAGS, flag_title=FLAG_TITLE,
                switch_urls=_switch_urls(req), qs=(lambda: "?lang=" + lang),
                type_label=(lambda ty: type_label(lang, ty)),
                v=VERSION,    # Cache-Busting für statische Dateien (z. B. Favicon); Seiten dürfen überschreiben
                # Partner-Leiste über dem Footer – nur wenn Partner eingetragen sind und nicht
                # auf der Karte (dort steht der Partner-Kasten in der rechten Spalte).
                partner_strip=(_load_partners() if name != "map" else []))
    i18n["nav_member"] = bool(_aam_member(req))
    i18n["intro_v"] = _intro_asset_v()
    i18n["css_v"] = _css_asset_v()
    i18n["swarm_banner"] = SWB.season_info(lang) if name not in ("schwarmflug", "map") else None
    chat_uid = _chat_uid(req) if name != "map" else None
    i18n.update(chat_ok=bool(chat_uid), chat_csrf=_member_csrf(chat_uid) if chat_uid else "",
                chat_ttl=_chat_ttl())
    i18n.update(ctx)   # template-spezifischer Kontext (items, cols, …) ergänzt/gewinnt
    html = ENV.get_template(name).render(title=title, flash=flash, admin=_is_admin(req), **i18n)
    resp = web.Response(text=html, content_type="text/html")
    if chat_uid:
        resp.headers["Cache-Control"] = "private, no-store"   # Seite enthält Mitglieder-Token
    return resp


def _ver_key(v: str) -> tuple:
    """Semantischer Versions-Sortierschlüssel: '1.10.0' > '1.9.0'. Leere/fehlende
    Version -> (0,0,0,0), landet damit hinter allen echten Versionen."""
    parts = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    return tuple(parts) + (0,) * (4 - len(parts))


def _eur_fmt(v) -> str:
    return (f"{v:.2f} €").replace(".", ",") if isinstance(v, (int, float)) else "–"


def _konto_flash(req) -> str:
    m = req.query.get("m", "")
    return translate(pick_lang(req), "konto_m_" + m, n=req.query.get("n", "")) \
        if m and ("konto_m_" + m) in _BI18N_T else ""


def _css_asset_v() -> str:
    """Cache-Busting für die Board-Stylesheets: Version + jüngste Änderungszeit aller
    static/board*.css – ändert sich bei jedem Update einer CSS-Datei, sonst stabil."""
    try:
        newest = max(int(p.stat().st_mtime) for p in STATIC_DIR.glob("board*.css"))
        return f"{VERSION}.{newest}"
    except (OSError, ValueError):
        return VERSION


def _intro_asset_v() -> str:
    try:
        return f"{VERSION}.{int((STATIC_DIR / 'intro.js').stat().st_mtime)}"
    except OSError:
        return VERSION


def _f_dmy(v) -> str:
    s = str(v or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return s


def _f_berlin(v) -> str:
    try:
        return berlin_from_utc_naive(str(v), "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M")
    except Exception:
        return str(v or "")


ENV.filters["dmy"] = _f_dmy
ENV.filters["berlin"] = _f_berlin
