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
import asyncio
import csv as _csv
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import time

import psutil
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import discord
import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from discord.ext import commands, tasks
from jinja2 import Environment, DictLoader, select_autoescape

from config import (BOARD_ENABLED, BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL,
                    BOARD_ADMIN_TOKEN, BOARD_OWNER_ID, BOARD_HASH_SALT,
                    SHOPS_DATA_FILE, SPECIES_CATALOG_FILE, DATA_DIRECTORY, AI_CHAT_PUBLIC,
                    VERSION,
                    MAP_ENABLED, MAP_GUILD_ID, MAP_JITTER_METERS, MAP_MAX_ZOOM,
                    BOARD_OAUTH_CLIENT_ID, BOARD_OAUTH_CLIENT_SECRET, BOARD_OAUTH_REDIRECT_URI)
from utils import geo, map_tags
from datetime import datetime, timezone, timedelta
from utils.board_db import (board_init, board_query, board_one, board_exec, board_execmany)
from utils.db import execute_db
from utils.timez import BERLIN, now_berlin, berlin_from_utc_naive
from urllib.parse import urlencode
from utils.board_i18n import (LANGS, FLAGS, FLAG_TITLE, pick_lang, translate,
                              type_label, flash_text, country_name)
from utils.currency import ensure_rates
from utils import shop_stats

# Vendored statische Assets (Chart.js self-hosted, kein CDN) unter <repo>/static/.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Rechtsseiten-Inhalte unter <repo>/legal/. Echte Datei (z.B. impressum.html) liegt
# NICHT im Git (gitignored) und wird bevorzugt geladen; sonst die .example-Vorlage.
LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"
_LEGAL_PAGES = {"impressum", "datenschutz"}


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


async def _send_contact_dm(app, message: str, name: str, email: str, tel: str) -> bool:
    """Kontaktformular-Nachricht als Discord-DM an den Owner (BOARD_OWNER_ID)."""
    bot = app.get("bot")
    if not BOARD_OWNER_ID or bot is None:
        logger.warning("✉️ Kontaktformular: BOARD_OWNER_ID/Bot fehlt – Nachricht verworfen.")
        return False
    try:
        user = await bot.fetch_user(BOARD_OWNER_ID)
        e = discord.Embed(title="✉️ Board-Kontaktformular", description=message[:4000], color=0x1F6FEB)
        e.add_field(name="Name", value=name or "—")
        e.add_field(name="E-Mail", value=email or "—")
        if tel:
            e.add_field(name="Telefon", value=tel, inline=False)
        await user.send(embed=e)
        return True
    except Exception as ex:  # noqa: BLE001
        logger.error("✉️ Kontaktformular-DM fehlgeschlagen: %s", ex)
        return False


def _legal_content(name: str) -> tuple[str, bool]:
    """Lädt den HTML-Body einer Rechtsseite. Rückgabe: (html, is_example).
    Reihenfolge: legal/<name>.html (echt) > legal/<name>.example.html (Vorlage)."""
    if name not in _LEGAL_PAGES:
        return ("", True)
    for path, is_example in ((LEGAL_DIR / f"{name}.html", False),
                             (LEGAL_DIR / f"{name}.example.html", True)):
        try:
            return (path.read_text(encoding="utf-8"), is_example)
        except OSError:
            continue
    return ("<p>[[Seite noch nicht konfiguriert]]</p>", True)
# Strikte Allowlist auslieferbarer Dateien -> {Dateiname: Content-Type}. Neue Assets
# hier eintragen (der /static-Handler baut den Pfad nur aus diesen Literalen).
_STATIC_FILES = {
    "chart.umd.js": "application/javascript",
    "chartjs-chart-treemap.min.js": "application/javascript",
    "stats.js": "application/javascript",
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
    "de_bundeslaender.geojson": "application/geo+json",
    "at_bundeslaender.geojson": "application/geo+json",
    "ch_kantone.geojson": "application/geo+json",
    "li_gemeinden.geojson": "application/geo+json",
    "li_land.geojson": "application/geo+json",
    # Favicon: statisches PNG (alle Browser), animiertes GIF (nur Firefox animiert Favicons),
    # Homescreen-Icon für Smartphones.
    "favicon.png": "image/png",
    "favicon.gif": "image/gif",
    "apple-touch-icon.png": "image/png",
}

logger = logging.getLogger(__name__)

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
_ADMIN_COOKIE, _VOTER_COOKIE = "board_admin", "board_vid"

_hits: dict[str, list] = defaultdict(list)


def _rate(key: str, limit: int, window: int) -> bool:
    now = time.time(); q = _hits[key]
    while q and q[0] < now - window:
        q.pop(0)
    if len(q) >= limit:
        return False
    q.append(now); return True


def _hmac(*parts: str) -> str:
    # HMAC-SHA3-512 mit geheimem Salt als Schlüssel (IPs werden nie roh gespeichert).
    return hmac.new(BOARD_HASH_SALT, "|".join(parts).encode(), hashlib.sha3_512).hexdigest()


def _ip(req):
    xff = req.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (req.remote or "0.0.0.0")


def _is_admin(req) -> bool:
    exp = _hmac("owner", BOARD_ADMIN_TOKEN) if BOARD_ADMIN_TOKEN else ""
    return bool(exp) and hmac.compare_digest(req.cookies.get(_ADMIN_COOKIE, ""), exp)


def _csrf_token() -> str:
    """CSRF-Token serverseitig aus dem Admin-Token abgeleitet (kein User-Input,
    kein separater Cookie). Nur wer eingeloggt ist, bekommt es in die Formulare."""
    return _hmac("csrf", BOARD_ADMIN_TOKEN) if BOARD_ADMIN_TOKEN else ""


def _csrf_ok(form) -> bool:
    exp = _csrf_token()
    return bool(exp) and hmac.compare_digest(form.get("csrf", ""), exp)


# ── Templates (Dark-Mode-ONLY) ────────────────────────────────────────────────
BASE = """<!doctype html><html lang="{{ lang }}"><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{{ title }} · AAM-Bot Board</title><link rel="icon" id="favicon" type="image/png" href="/static/favicon.png?v={{ v }}"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v={{ v }}"><script>if(/Firefox\//.test(navigator.userAgent)){var f=document.getElementById("favicon");f.type="image/gif";f.href="/static/favicon.gif?v={{ v }}";}</script><style>
 :root{color-scheme:only dark} html,body{background:#0d1117}
 body{color:#e6edf3;font:15px/1.5 system-ui,Segoe UI,Arial;margin:0}
 option{background:#0d1117;color:#e6edf3} ::placeholder{color:#6e7681;opacity:1}
 a{color:#58a6ff;text-decoration:none} a:hover{text-decoration:underline}
 header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;gap:16px;align-items:center}
 header h1{font-size:18px;margin:0} .grow{flex:1}
 .wrap{max-width:1100px;margin:0 auto;padding:20px}
 .btn{background:#238636;color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer;font-size:14px}
 .btn.grey{background:#30363d} .btn.red{background:#8b2b2b} .btn.small{padding:3px 8px;font-size:13px}
 .cols{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;align-items:start}
 .col{display:flex;flex-direction:column;min-height:0;background:#0f141a;border:1px solid #21262d;border-radius:10px;padding:10px 8px 8px}
 .col h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;margin:0 0 8px;padding:0 2px}
 .col-body{max-height:68vh;overflow-y:auto;overflow-x:hidden;padding:0 4px 2px;scrollbar-width:thin;scrollbar-color:#30363d transparent}
 .col-body::-webkit-scrollbar{width:8px}
 .col-body::-webkit-scrollbar-thumb{background:#30363d;border-radius:4px}
 .col-body::-webkit-scrollbar-thumb:hover{background:#3d444d}
 .col-body::-webkit-scrollbar-track{background:transparent}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 12px;margin-bottom:10px}
 .status-panel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin-bottom:18px}
 summary.status-head{cursor:pointer;list-style:none;user-select:none}
 summary.status-head::-webkit-details-marker{display:none}
 .status-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-weight:600;font-size:15px;margin-bottom:0}
 details[open]>.status-head{margin-bottom:4px}
 .status-ver{color:#8b949e;font-size:12px;font-weight:600;border:1px solid #30363d;border-radius:20px;padding:2px 9px;white-space:nowrap}
 .status-metrics{font-size:12px;color:#8b949e;font-weight:400;white-space:nowrap}
 .status-metrics .mok{color:#3fb950} .status-metrics .mwarn{color:#d29922} .status-metrics .mdown{color:#f85149}
 .status-stand{margin-left:auto;color:#6e7681;font-size:11px;font-weight:400;white-space:nowrap}
 .status-toggle{color:#8b949e;font-size:12px;font-weight:400;white-space:nowrap}
 .status-toggle::after{content:"▸";display:inline-block;margin-left:6px;transition:transform .15s}
 details[open] .status-toggle::after{transform:rotate(90deg)}
 .status-badge{display:inline-flex;align-items:center;gap:7px;padding:4px 12px;border-radius:20px;font-size:14px;font-weight:600}
 .status-badge::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor;box-shadow:0 0 6px currentColor}
 .s-ok{background:#3fb95022;border:1px solid #3fb95066;color:#3fb950} .s-warn{background:#d2992222;border:1px solid #d2992266;color:#d29922} .s-down{background:#f8514922;border:1px solid #f8514966;color:#f85149}
 .status-section{margin-top:14px} .status-section:first-of-type{margin-top:4px}
 .status-sub{font-size:13px;font-weight:600;color:#c9d1d9;margin:0 0 8px;padding-bottom:6px;border-bottom:1px solid #21262d}
 .status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:8px}
 .hc{display:flex;align-items:flex-start;gap:9px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px}
 a.hc{text-decoration:none;color:inherit} a.hc:hover{border-color:#3d444d;background:#11161d}
 .dot{width:10px;height:10px;border-radius:50%;margin-top:4px;flex:0 0 auto}
 .dot.ok{background:#3fb950} .dot.warn{background:#d29922} .dot.down{background:#f85149} .dot.off{background:#6e7681}
 .hc .n{font-weight:600;font-size:13px} .hc .d{color:#8b949e;font-size:12px;margin-top:1px}
 @media(max-width:820px){.cols{grid-template-columns:1fr} .col-body{max-height:none}}
 .card .t{font-weight:600;overflow-wrap:anywhere} .muted{color:#8b949e;font-size:13px}
 .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;border:1px solid #30363d;margin-right:5px}
 .bug{color:#ff7b72;border-color:#ff7b72} .feature{color:#7ee787;border-color:#7ee787} .idea{color:#d2a8ff;border-color:#d2a8ff}
 .legend{font-size:12px} .legend .tag{margin:0 3px} a.btn{display:inline-block;text-decoration:none}
 .cardfoot{display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap}
 .cmark{font-size:12px;white-space:nowrap} .cardmore{margin-left:auto}
 .up{background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:20px;padding:3px 10px;cursor:pointer}
 input,textarea,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:8px;width:100%;box-sizing:border-box}
 label{display:block;margin:10px 0 3px;color:#8b949e;font-size:13px}
 table{width:100%;border-collapse:collapse} td,th{border-bottom:1px solid #21262d;padding:6px 8px;text-align:left;vertical-align:top}
 .hp{position:absolute;left:-9999px} .flash{background:#1f6feb22;border:1px solid #1f6feb;border-radius:6px;padding:10px 12px;margin-bottom:14px}
 .langsw{display:inline-flex;gap:4px;align-items:center}
 .langsw a{border:1px solid #30363d;border-radius:6px;padding:2px 7px;font-size:13px;line-height:1.4;color:#8b949e}
 .langsw a:hover{text-decoration:none;border-color:#3d444d}
 .langsw a.on{border-color:#58a6ff;color:#e6edf3;background:#1f6feb22}
 .langsw svg.fl{width:18px;height:12px;vertical-align:middle;border-radius:2px;border:1px solid #30363d;margin-right:1px}
 .statmeta{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 6px}
 .statmeta span{font-size:12px;color:#8b949e;background:#0f141a;border:1px solid #21262d;border-radius:20px;padding:3px 10px}
 .secnav{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:sticky;top:0;z-index:5;background:#0d1117;padding:10px 0;border-bottom:1px solid #21262d;margin-bottom:8px}
 .secnav a{border:1px solid #30363d;border-radius:20px;padding:3px 10px;font-size:13px}
 .statsec{scroll-margin-top:58px;padding:16px 0;border-bottom:1px solid #161b22}
 .statsec h3{margin:0 0 12px;font-size:17px}
 .kpigrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
 .kpi{background:#0f141a;border:1px solid #21262d;border-radius:10px;padding:12px 14px}
 .kpi .v{font-size:22px;font-weight:700} .kpi .l{color:#8b949e;font-size:12px;margin-top:2px}
 .chartbox{background:#0f141a;border:1px solid #21262d;border-radius:10px;padding:12px;margin-top:12px}
 .chartbox h4{margin:0 0 8px;font-size:14px;color:#c9d1d9;font-weight:600}
 .info{cursor:help;color:#8b949e;font-size:12px;font-weight:400;user-select:none}
 .info:hover{color:#58a6ff}
 .chartwrap{position:relative;height:320px}
 .raritygrid{columns:2;column-gap:18px;margin-top:8px;font-size:13px;color:#8b949e}
 .raritygrid div{break-inside:avoid;padding:1px 0;font-style:italic}
 @media(max-width:640px){.raritygrid{columns:1}}
 .rangesw{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
 .rangesw a{border:1px solid #30363d;border-radius:20px;padding:3px 10px;font-size:13px}
 .rangesw a.on{border-color:#58a6ff;color:#e6edf3;background:#1f6feb22}
 .legal{max-width:820px} .legal h3{margin:18px 0 6px;font-size:15px;color:#c9d1d9} .legal p{margin:0 0 8px} .legal code{background:#161b22;border:1px solid #30363d;border-radius:4px;padding:1px 5px}
</style></head><body>
<header><h1>🐜 {{ t('brand') }}</h1>
 <a href="/{{ qs() }}">{{ t('nav_board') }}</a><a href="/stats{{ qs() }}">{{ t('nav_stats') }}</a><a href="/map{{ qs() }}">{{ t('nav_map') }}</a><a href="/submit{{ qs() }}">{{ t('nav_submit') }}</a><a href="https://paypal.me/JonasBeier1998" target="_blank" rel="noopener">{{ t('nav_support') }}</a><span class=grow></span>
 <span class="langsw">{% for code in langs %}<a class="{{ 'on' if code==lang }}" href="{{ switch_urls[code] }}" title="{{ flag_title[code] }}">{{ flags[code][0]|safe }} {{ flags[code][1] }}</a>{% endfor %}</span>
 {% if admin %}<span class=muted>{{ t('nav_owner') }}</span> <a href="/admin{{ qs() }}">{{ t('nav_admin') }}</a> <a href="/admin/logout">{{ t('nav_logout') }}</a>
 {% else %}<a href="/admin/login{{ qs() }}">{{ t('nav_login') }}</a>{% endif %}</header>
<div class=wrap>{% if flash %}<div class=flash>{{ flash }}</div>{% endif %}{% block body %}{% endblock %}</div>
<footer style="max-width:1100px;margin:28px auto 12px;padding:14px 20px;border-top:1px solid #30363d;color:#8b949e;font-size:13px;text-align:center;line-height:1.6">
  💖 <strong>{{ t('footer_run') }}</strong>
  <a href="https://paypal.me/JonasBeier1998" target="_blank" rel="noopener" style="color:#58a6ff">paypal.me/JonasBeier1998</a>
  · <a href="https://github.com/JonasVerzockt/Discord-Bot" target="_blank" rel="noopener" style="color:#58a6ff">{{ t('footer_source') }}</a>
  · <a href="/impressum?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_impressum') }}</a>
  · <a href="/datenschutz?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_privacy') }}</a>
</footer>
</body></html>"""

BOARD = """{% extends "base" %}{% block body %}
<details class="status-panel">
 <summary class="status-head">{{ t('status_head') }}
  <span id="hc-badge" class="status-badge s-{{ overall[0] }}">{{ overall[1] }}</span>
  <span id="hc-ver" class="status-ver" title="{{ t('ver_title') }}">v{{ version }}</span>
  <span id="hc-metrics" class="status-metrics">{{ metrics_html|safe }}</span>
  <span id="hc-stand" class="status-stand" title="{{ t('stand_title') }}">{{ t('stand_label') }} {{ generated }}</span>
  <span class="status-toggle">{{ t('details') }}</span></summary>
 <div id="hc-body" class="status-body">
 {% for sec in sections %}
 <div class="status-section">
  <div class="status-sub">{{ sec.title }}{% if sec.note %} <span class=muted>· {{ sec.note }}</span>{% endif %}</div>
  <div class="status-grid">
  {% for hc in sec.checks %}
   <a class=hc href="/status/check/{{ hc.name|urlencode }}?lang={{ lang }}" title="{{ t('incident_history') }}"><span class="dot {{ hc.state }}"></span>
    <div><div class=n>{{ hc.name }}</div><div class=d>{{ hc.detail }}</div></div></a>
  {% endfor %}
  </div>
 </div>
 {% endfor %}
 </div>
</details>
<script>
var I18N={incident:{{ t('incident_history')|tojson }},stand:{{ t('stand_label')|tojson }},noconn:{{ t('js_noconn')|tojson }},lang:{{ lang|tojson }}};
(function(){
  // Aktualisiert alle 5 s NUR den Status-Bereich (Rest der Seite bleibt unberührt);
  // rendert nur neu, wenn sich die Daten gegenüber dem letzten Poll geändert haben.
  // last=null → der erste Poll (nach 5 s) gleicht die Anzeige einmal mit dem Server
  // ab, danach wird ausschließlich bei echten Änderungen neu gezeichnet.
  var last = null;
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function _gbjs(b){return (b/1073741824).toFixed(1);}
  function _seg(part,label,val,title){ if(!part){return '';} var cls={ok:'mok',warn:'mwarn',down:'mdown'}[part.state]||''; return '<span class="'+cls+'" title="'+esc(title)+'">'+label+' '+val+'</span>'; }
  function buildMetrics(m){ if(!m){return '';} var p=[];
    if(m.cpu){ var lo=m.cpu.load; p.push(_seg(m.cpu,'Load',lo[0].toFixed(2),'1/5/15 min: '+lo[0].toFixed(2)+' / '+lo[1].toFixed(2)+' / '+lo[2].toFixed(2)+' · '+m.cpu.cores+' Kerne')); }
    if(m.ram){ p.push(_seg(m.ram,'RAM',m.ram.percent+'%',_gbjs(m.ram.used)+'/'+_gbjs(m.ram.total)+' GB')); }
    if(m.disk){ p.push(_seg(m.disk,'SSD',m.disk.percent+'%',_gbjs(m.disk.used)+'/'+_gbjs(m.disk.total)+' GB')); }
    return p.length ? '⚙️ '+p.join(' · ') : ''; }
  function setMetrics(m){ var e=document.getElementById('hc-metrics'); if(e){ e.innerHTML=buildMetrics(m); } }
  function build(d){
    var badge=document.getElementById('hc-badge');
    if(badge){ badge.className='status-badge s-'+d.overall[0]; badge.textContent=d.overall[1]; }
    var ver=document.getElementById('hc-ver'); if(ver){ ver.textContent='v'+d.version; }
    var body=document.getElementById('hc-body'); if(!body){ return; }
    var frag=document.createDocumentFragment();
    (d.sections||[]).forEach(function(sec){
      var s=document.createElement('div'); s.className='status-section';
      var sub=document.createElement('div'); sub.className='status-sub'; sub.textContent=sec.title;
      if(sec.note){ var m=document.createElement('span'); m.className='muted'; m.textContent=' · '+sec.note; sub.appendChild(m); }
      s.appendChild(sub);
      var grid=document.createElement('div'); grid.className='status-grid';
      (sec.checks||[]).forEach(function(hc){
        var card=document.createElement('a'); card.className='hc';
        card.href='/status/check/'+encodeURIComponent(hc.name)+'?lang='+I18N.lang;
        card.title=I18N.incident;
        var dot=document.createElement('span'); dot.className='dot '+hc.state; card.appendChild(dot);
        var box=document.createElement('div');
        var n=document.createElement('div'); n.className='n'; n.textContent=hc.name; box.appendChild(n);
        var de=document.createElement('div'); de.className='d'; de.textContent=hc.detail; box.appendChild(de);
        card.appendChild(box); grid.appendChild(card);
      });
      s.appendChild(grid); frag.appendChild(s);
    });
    body.replaceChildren(frag);
  }
  function setStand(txt){ var st=document.getElementById('hc-stand'); if(st){ st.textContent=txt; } }
  function tick(){
    // Cache-Bust gegen Proxy-/Browser-Caching; Fehler werden SICHTBAR gemacht,
    // damit ein Reverse-Proxy-/CSP-Problem nicht still bleibt.
    fetch('/status.json?lang='+I18N.lang+'&_='+Date.now(),{cache:'no-store'}).then(function(r){
      if(!r.ok){ throw new Error('HTTP '+r.status); }
      return r.json();
    }).then(function(d){
      setStand(I18N.stand+' '+d.generated);   // Zeitstempel bei JEDEM Poll aktualisieren
      setMetrics(d.metrics);                  // CPU-Load/RAM/SSD bei JEDEM Poll aktualisieren
      var sig=JSON.stringify([d.overall, d.version, d.sections]);  // 'generated' bewusst NICHT vergleichen
      if(sig===last){ return; }   // Health unverändert -> Kacheln nicht neu rendern
      last=sig; build(d);
    }).catch(function(){ setStand(I18N.noconn); });
  }
  tick();                 // sofort (nicht erst nach 5 s)
  setInterval(tick, 5000);
})();
</script>
<p class=muted>{{ t('board_intro') }} <a href="/submit?lang={{ lang }}">+ {{ t('nav_submit') }}</a></p>
<p class="muted legend">{{ t('legend_priority') }} <span class=tag>P0</span> {{ t('prio_p0') }} · <span class=tag>P1</span> {{ t('prio_p1') }} · <span class=tag>P2</span> {{ t('prio_p2') }} · <span class=tag>P3</span> {{ t('prio_p3') }} &nbsp;|&nbsp; {{ t('legend_upvotes') }} · {{ t('legend_comments') }} · {{ t('legend_more') }}</p>
<div class=cols>{% for key,tkey in cols %}
 <div class=col><h2>{{ t(tkey) }}</h2>
  <div class=col-body>
  {% for c in items if c.status==key %}
   <div class=card><span class="tag {{c.type}}">{{ type_label(c.type) }}</span>
    {% if c.component %}<span class=tag>{{ c.component }}</span>{% endif %}
    {% if c.priority %}<span class=tag>{{ c.priority }}</span>{% endif %}
    <div class=t><a href="/submission/{{c.id}}?lang={{ lang }}">{{ c.title }}</a></div>
    {% if c.version %}<div class=muted>{{ t('done_in', v=c.version) }}</div>{% endif %}
    <div class=cardfoot>
     <form method=post action="/upvote/{{c.id}}" style="margin:0"><button class=up>▲ {{ c.upvotes }}</button></form>
     {% if c.comments %}<a class="muted cmark" href="/submission/{{c.id}}?lang={{ lang }}" title="{{ t('n_comments_title', n=c.comments) }}">💬 {{ c.comments }}</a>{% endif %}
     <a class="muted cmark cardmore" href="/submission/{{c.id}}?lang={{ lang }}" title="{{ t('more') }}">{{ t('more') }}</a>
    </div>
   </div>
  {% else %}<div class=muted>—</div>{% endfor %}
  </div>
 </div>
{% endfor %}</div>
{% endblock %}"""

SUBMIT = """{% extends "base" %}{% block body %}
<h2>{{ t('submit_h') }}</h2>
<p class=muted>{{ t('submit_anon') }}</p>
<p class=muted>{{ t('submit_terms') }}</p>
<form method=post action="/submit?lang={{ lang }}">
 <label>{{ t('f_type') }}</label><select name=type>{% for ty in types %}<option value="{{ty}}">{{ type_label(ty) }}</option>{% endfor %}</select>
 <label>{{ t('f_title') }}</label><input name=title maxlength=120 required>
 <label>{{ t('f_desc') }}</label><textarea name=body rows=6 maxlength=4000></textarea>
 <label>{{ t('f_name') }}</label><input name=submitter_name maxlength=40 placeholder="{{ t('ph_anon') }}">
 <input class=hp type=text name=website tabindex=-1 autocomplete=off>
 <div style="margin-top:14px"><button class=btn>{{ t('btn_send') }}</button> <a href="/?lang={{ lang }}">{{ t('cancel') }}</a></div>
</form>{% endblock %}"""

DETAIL = """{% extends "base" %}{% block body %}
<p><a href="/?lang={{ lang }}">{{ t('back_board') }}</a></p>
<span class="tag {{c.type}}">{{ type_label(c.type) }}</span>{% if c.component %}<span class=tag>{{ c.component }}</span>{% endif %}
{% if c.priority %}<span class=tag>{{ c.priority }}</span>{% endif %}<span class=tag>{{ c.status }}</span>
<h2 style="margin:8px 0">{{ c.title }}</h2>
<form method=post action="/upvote/{{c.id}}?lang={{ lang }}"><button class=up>{{ t('upvotes_n', n=c.upvotes) }}</button></form>
<p style="white-space:pre-wrap;margin-top:14px">{{ c.body }}</p>
<p class=muted>{{ t('submitted_at', d=c.created_at) }}{% if c.version %} · {{ t('done_in', v=c.version) }}{% endif %}</p>
{% if comments %}<h3 style="margin-top:22px">{{ t('comments_h') }}</h3>
{% for k in comments %}<div class=card><b>{{ k.author or 'Owner' }}</b> <span class=muted>· {{ k.created_at }}</span>
 <div style="white-space:pre-wrap;margin-top:4px">{{ k.body }}</div></div>{% endfor %}{% endif %}
{% if admin %}<p style="margin-top:16px"><a class="btn small" href="/admin/{{c.id}}/edit?lang={{ lang }}">{{ t('edit_or_comment') }}</a></p>{% endif %}
{% endblock %}"""

EDIT = """{% extends "base" %}{% block body %}
<p><a href="/admin?lang={{ lang }}">{{ t('back_admin') }}</a> · <a href="/submission/{{c.id}}?lang={{ lang }}">{{ t('public_view') }}</a></p>
<h2>{{ t('edit_h', id=c.id) }}</h2>
<form method=post action="/admin/{{c.id}}/edit?lang={{ lang }}"><input type=hidden name=csrf value="{{csrf}}">
 <label>{{ t('f_type') }}</label><select name=type>{% for ty in types %}<option value="{{ty}}" {{'selected' if ty==c.type}}>{{ type_label(ty) }}</option>{% endfor %}</select>
 <label>{{ t('f_title') }}</label><input name=title maxlength=120 required value="{{ c.title }}">
 <label>{{ t('f_desc') }}</label><textarea name=body rows=8 maxlength=4000>{{ c.body }}</textarea>
 <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
  <div style="flex:1;min-width:120px"><label>{{ t('f_status') }}</label><select name=status>{% for s in statuses %}<option value="{{s}}" {{'selected' if s==c.status}}>{{s}}</option>{% endfor %}</select></div>
  <div style="flex:1;min-width:100px"><label>{{ t('f_priority') }}</label><select name=priority>{% for p in priorities %}<option value="{{p}}" {{'selected' if p==c.priority}}>{{p or '–'}}</option>{% endfor %}</select></div>
  <div style="flex:1;min-width:150px"><label>{{ t('f_component') }}</label><select name=component>{% for k in components %}<option value="{{k}}" {{'selected' if k==c.component}}>{{k or '–'}}</option>{% endfor %}</select></div>
  <div style="min-width:110px"><label>{{ t('f_version') }}</label><input name=version value="{{ c.version }}" style="width:110px"></div>
 </div>
 <p class="muted legend" style="margin-top:8px">{{ t('legend_priority') }} <span class=tag>P0</span> {{ t('prio_p0') }} · <span class=tag>P1</span> {{ t('prio_p1') }} · <span class=tag>P2</span> {{ t('prio_p2') }} · <span class=tag>P3</span> {{ t('prio_p3') }}</p>
 <div style="margin-top:12px"><button class=btn>{{ t('btn_save_disk') }}</button></div>
</form>
<h3 style="margin-top:26px">{{ t('comments_count_h', n=comments|length) }}</h3>
{% for k in comments %}<div class=card>
 <form method=post action="/admin/comment/{{k.id}}/delete?lang={{ lang }}" style="float:right"><input type=hidden name=csrf value="{{csrf}}"><input type=hidden name=sid value="{{c.id}}"><button class="btn small grey">🗑</button></form>
 <b>{{ k.author or 'Owner' }}</b> <span class=muted>· {{ k.created_at }}</span>
 <div style="white-space:pre-wrap;margin-top:4px">{{ k.body }}</div></div>{% endfor %}
<form method=post action="/admin/{{c.id}}/comment?lang={{ lang }}" style="margin-top:12px"><input type=hidden name=csrf value="{{csrf}}">
 <label>{{ t('new_comment') }}</label><textarea name=body rows=3 maxlength=4000 required placeholder="{{ t('ph_comment') }}"></textarea>
 <label>{{ t('f_author') }}</label><input name=author maxlength=40 value="Owner" style="max-width:220px">
 <div style="margin-top:10px"><button class=btn>{{ t('add_comment') }}</button></div>
</form>{% endblock %}"""

STATUSDETAIL = """{% extends "base" %}{% block body %}
<p><a href="/?lang={{ lang }}">{{ t('back_board') }}</a></p>
<h2 style="margin-bottom:4px">🩺 {{ key }}</h2>
{% if current %}<p><span class="dot {{ current.state }}" style="display:inline-block;vertical-align:middle"></span>
 {{ t('current_label') }} <b>{{ current.state|upper }}</b> · {{ current.detail }}</p>{% endif %}
<p class=muted>{{ t('inc_intro') }}</p>
<h3 style="margin-top:16px">{{ t('inc_recent_h') }}</h3>
{% if not incidents %}<p class=muted>{{ t('inc_none') }}</p>{% endif %}
{% for inc in incidents %}<div class=card>
 {% if admin %}<form method=post action="/status/incident/{{ inc.id }}/note?lang={{ lang }}" style="float:right"><input type=hidden name=csrf value="{{csrf}}"><input type=hidden name=key value="{{ key }}">
  <input name=note maxlength=500 value="{{ inc.admin_note }}" placeholder="{{ t('ph_admin_note') }}" style="width:220px"> <button class="btn small">📝</button></form>{% endif %}
 <span class="dot {{ inc.state }}" style="display:inline-block;vertical-align:middle"></span> <b>{{ inc.state|upper }}</b>
 <div style="margin-top:4px">🔴 {{ t('inc_since') }} <b>{{ inc.started_local }}</b> —
  {% if inc.ended_local %}🟢 {{ t('inc_ok_since') }} <b>{{ inc.ended_local }}</b>{% else %}<span class=muted>{{ t('inc_running') }}</span>{% endif %}</div>
 {% if inc.detail %}<div class=muted style="margin-top:3px">{{ inc.detail }}</div>{% endif %}
 {% if inc.admin_note %}<div style="margin-top:4px">📝 {{ inc.admin_note }}</div>{% endif %}
</div>{% endfor %}
{% endblock %}"""

LOGIN = """{% extends "base" %}{% block body %}
<h2>{{ t('login_h') }}</h2><form method=post action="/admin/login?lang={{ lang }}" style="max-width:340px">
 <label>{{ t('f_token') }}</label><input name=token type=password autofocus>
 <div style="margin-top:12px"><button class=btn>{{ t('btn_login') }}</button></div></form>{% endblock %}"""

ADMIN = """{% extends "base" %}{% block body %}
<h2>{{ t('queue_h', n=queue|length) }}</h2>
{% if not queue %}<p class=muted>{{ t('nothing_review') }}</p>{% endif %}
{% for c in queue %}<div class=card><span class="tag {{c.type}}">{{ type_label(c.type) }}</span> <b>{{ c.title }}</b>
 <div class=muted>{{ c.body }}</div>
 <form method=post action="/admin/{{c.id}}/approve?lang={{ lang }}" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small">{{ t('btn_approve') }}</button></form>
 <form method=post action="/admin/{{c.id}}/reject?lang={{ lang }}" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small red">{{ t('btn_reject') }}</button></form>
 <form method=post action="/admin/{{c.id}}/delete?lang={{ lang }}" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small grey">{{ t('btn_delete') }}</button></form>
</div>{% endfor %}
<h2 style="margin-top:24px">{{ t('all_entries_h', n=items|length) }}</h2>
<p class="muted legend">{{ t('legend_priority') }} <span class=tag>P0</span> {{ t('prio_p0') }} · <span class=tag>P1</span> {{ t('prio_p1') }} · <span class=tag>P2</span> {{ t('prio_p2') }} · <span class=tag>P3</span> {{ t('prio_p3') }} · {{ t('admin_legend_edit') }}</p>
<table><tr><th>#</th><th>{{ t('th_title') }}</th><th>{{ t('th_status_meta') }}</th><th>▲</th><th></th></tr>
{% for c in items if c.status!='pending' %}<tr><td>{{c.id}}</td>
 <td><span class="tag {{c.type}}">{{ type_label(c.type) }}</span> {{ c.title }}</td>
 <td><form method=post action="/admin/{{c.id}}/status?lang={{ lang }}"><input type=hidden name=csrf value="{{csrf}}"><div style="display:flex;gap:6px;flex-wrap:wrap">
   <select name=status>{% for s in statuses %}<option value="{{s}}" {{'selected' if s==c.status}}>{{s}}</option>{% endfor %}</select>
   <select name=priority>{% for p in priorities %}<option value="{{p}}" {{'selected' if p==c.priority}}>{{p or '–'}}</option>{% endfor %}</select>
   <select name=component>{% for k in components %}<option value="{{k}}" {{'selected' if k==c.component}}>{{k or '–'}}</option>{% endfor %}</select>
   <input name=version value="{{c.version}}" placeholder="{{ t('f_version') }}" style="width:90px">
   <button class="btn small">{{ t('btn_save') }}</button></div></form></td>
 <td>{{ c.upvotes }}</td>
 <td style="white-space:nowrap"><a class="btn small" href="/admin/{{c.id}}/edit?lang={{ lang }}">✏️</a>
   <form method=post action="/admin/{{c.id}}/delete?lang={{ lang }}" style="display:inline"><input type=hidden name=csrf value="{{csrf}}"><button class="btn small grey">🗑</button></form></td></tr>
{% endfor %}</table>
<h3 style="margin-top:24px">{{ t('csv_h') }}</h3>
<form method=post action="/admin/import?lang={{ lang }}" enctype="multipart/form-data"><input type=hidden name=csrf value="{{csrf}}">
 <input type=file name=file accept=".csv"> <button class="btn small">{{ t('csv_import') }}</button>
 <div class=muted>{{ t('csv_help')|safe }}</div></form>
{% endblock %}"""

STATS = """{% extends "base" %}{% block body %}
{% set sections = [('overview','st_sec_overview'),('species','st_sec_species'),('shops','st_sec_shops'),('prices','st_sec_prices'),('availability','st_sec_availability'),('quality','st_sec_quality'),('trends','st_sec_trends')] %}
<h2 style="margin-bottom:6px">{{ t('nav_stats') }}</h2>
{% if not data %}
<div class=flash>{{ t('st_error') }}</div>
{% else %}
<p class=muted style="margin-top:0">{{ t('st_intro') }}</p>
<div class="statmeta">
 <span>📅 {{ t('st_data_as_of', d=data.meta.fetched_at) }}</span>
 <span>💶 {{ t('st_fx_note') }}</span>
 <span>♻️ {{ t('st_cache_note') }}</span>
 <span>🕒 {{ t('st_generated', d=data.meta.generated_at) }}</span>
</div>
<nav class="secnav"><span class=muted>{{ t('st_nav') }}</span>
 {% for aid,key in sections %}<a href="#{{ aid }}">{{ t(key) }}</a>{% endfor %}
</nav>
{% for aid,key in sections %}
<section id="{{ aid }}" class="statsec">
 <h3>{{ t(key) }} <span class="info" title="{{ t('exp_sec_' ~ aid) }}">ⓘ</span></h3>
 {% if aid=='overview' %}
  {% set o = data.overview %}
  <div class=kpigrid>
   <div class=kpi><div class=v>{{ o.shops_total }}</div><div class=l>{{ t('kpi_shops') }}</div></div>
   <div class=kpi><div class=v>{{ o.shops_with_products }}</div><div class=l>{{ t('kpi_shops_with') }}</div></div>
   <div class=kpi><div class=v>{{ o.live_products }}</div><div class=l>{{ t('kpi_live') }}</div></div>
   <div class=kpi><div class=v>{{ o.merch_products }}</div><div class=l>{{ t('kpi_merch') }}</div></div>
   <div class=kpi><div class=v>{{ o.species_total }}</div><div class=l>{{ t('kpi_species') }}</div></div>
   <div class=kpi><div class=v>{{ o.genera_total }}</div><div class=l>{{ t('kpi_genera') }}</div></div>
   <div class=kpi><div class=v>{{ o.instock_pct }}&nbsp;%</div><div class=l>{{ t('kpi_instock_pct') }}</div></div>
   <div class=kpi><div class=v>{{ o.countries|length }}</div><div class=l>{{ t('kpi_countries') }}</div></div>
  </div>
  <div class=chartbox><h4>{{ t('ch_countries_title') }}</h4><div class=chartwrap><canvas id="chCountries"></canvas></div></div>
  <div class=chartbox><h4>{{ t('ch_stock_title') }}</h4><div class="chartwrap" style="height:260px"><canvas id="chStock"></canvas></div></div>
 {% elif aid=='species' %}
  {% set sp = data.species %}
  <div class=chartbox><h4>{{ t('sp_genera_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chGenera"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sp_reach_title') }}</h4><div class="chartwrap" style="height:400px"><canvas id="chReach"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sp_longtail_title') }}</h4><div class=chartwrap><canvas id="chLongtail"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sp_rarities_title') }} <span class="info" title="{{ t('exp_rarities') }}">ⓘ</span></h4>
   <p class=muted style="margin-top:0">{{ t('sp_rarities_count', n=sp.rarities_count) }}</p>
   {% if sp.rarities_sample %}<details><summary style="cursor:pointer;color:#58a6ff">{{ t('sp_rarities_show', n=sp.rarities_sample|length) }}</summary>
    <div class=raritygrid>{% for r in sp.rarities_sample %}<div>{{ r }}</div>{% endfor %}</div></details>{% endif %}
  </div>
 {% elif aid=='shops' %}
  <div class=chartbox><h4>{{ t('sh_offers_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chShopOffers"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sh_breadth_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chShopBreadth"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sh_exclusive_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chShopExclusive"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sh_scatter_title') }}</h4><div class="chartwrap" style="height:380px"><canvas id="chShopScatter"></canvas></div></div>
 {% elif aid=='prices' %}
  {% set ps = data.prices.stats %}
  <div class=kpigrid>
   <div class=kpi><div class=v>{{ ps.median }}&nbsp;€</div><div class=l>{{ t('kpi_price_median') }}</div></div>
   <div class=kpi><div class=v>{{ ps.mean }}&nbsp;€</div><div class=l>{{ t('kpi_price_mean') }}</div></div>
   <div class=kpi><div class=v>{{ ps.p25 }}&nbsp;€</div><div class=l>{{ t('kpi_price_p25') }}</div></div>
   <div class=kpi><div class=v>{{ ps.p75 }}&nbsp;€</div><div class=l>{{ t('kpi_price_p75') }}</div></div>
   <div class=kpi><div class=v>{{ ps.min }}&nbsp;€</div><div class=l>{{ t('kpi_price_min') }}</div></div>
   <div class=kpi><div class=v>{{ ps.max }}&nbsp;€</div><div class=l>{{ t('kpi_price_max') }}</div></div>
  </div>
  <p class=muted style="margin-top:8px">{{ t('pr_basis_note') }}</p>
  <div class=chartbox><h4>{{ t('pr_hist_title') }}</h4><div class=chartwrap><canvas id="chPriceHist"></canvas></div></div>
  <div class=chartbox><h4>{{ t('pr_genus_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chPriceGenus"></canvas></div></div>
  <div class=chartbox><h4>{{ t('pr_spread_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chPriceSpread"></canvas></div></div>
  <div class=chartbox><h4>{{ t('pr_spread_small_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chPriceSpreadSmall"></canvas></div></div>
 {% elif aid=='availability' %}
  <div class=chartbox><h4>{{ t('av_genus_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chAvGenus"></canvas></div></div>
  <div class=chartbox><h4>{{ t('av_country_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chAvCountry"></canvas></div></div>
  <div class=chartbox><h4>{{ t('av_shop_best_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chAvShopBest"></canvas></div></div>
  <div class=chartbox><h4>{{ t('av_shop_worst_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chAvShopWorst"></canvas></div></div>
  <div class=chartbox><h4>{{ t('av_hardest_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chAvHardest"></canvas></div></div>
 {% elif aid=='quality' %}
  {% set q = data.quality %}
  <p class=muted style="margin-top:0">{{ t('dq_intro') }}</p>
  <div class=kpigrid>
   <div class=kpi><div class=v>{{ q.coverage_pct }}&nbsp;%</div><div class=l>{{ t('kpi_dq_coverage') }}</div></div>
   <div class=kpi><div class=v>{{ q.uncanon }}</div><div class=l>{{ t('kpi_dq_uncanon') }}</div></div>
   <div class=kpi><div class=v>{{ q.adjusted_pct }}&nbsp;%</div><div class=l>{{ t('kpi_dq_adjusted') }}</div></div>
  </div>
  {% if q.shop_uncanon %}<div class=chartbox><h4>{{ t('dq_shop_uncanon_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chDqShopUncanon"></canvas></div></div>{% endif %}
  <div class=chartbox><h4>{{ t('dq_shop_adjusted_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chDqShopAdjusted"></canvas></div></div>
  <div class=chartbox><h4>{{ t('dq_variants_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chDqVariants"></canvas></div></div>
  <div class=chartbox><h4>{{ t('dq_uncanon_list_title') }}</h4>
   {% if q.uncanon_raw %}<details><summary style="cursor:pointer;color:#58a6ff">{{ t('dq_uncanon_show', n=q.uncanon_raw|length) }}</summary>
    <div class=raritygrid>{% for name,cnt in q.uncanon_raw %}<div>{{ name }} <span class=muted>· {{ cnt }}</span></div>{% endfor %}</div></details>
   {% else %}<p class=muted style="margin-top:0">{{ t('dq_all_resolved') }}</p>{% endif %}
  </div>
 {% elif aid=='trends' %}
  <div class="rangesw">
   <span class=muted>{{ t('tr_range_label') }}</span>
   <a class="{{ 'on' if ts_range=='3' }}" href="/stats?lang={{ lang }}&range=3#trends">{{ t('tr_range_3') }}</a>
   <a class="{{ 'on' if ts_range=='12' }}" href="/stats?lang={{ lang }}&range=12#trends">{{ t('tr_range_12') }}</a>
   <a class="{{ 'on' if ts_range=='all' }}" href="/stats?lang={{ lang }}&range=all#trends">{{ t('tr_range_all') }}</a>
  </div>
  {% if not ts_available %}
   <p class=muted>{{ t('tr_unavailable') }}</p>
  {% else %}
   <div class=chartbox><h4>{{ t('tr_price_title') }}</h4><p class=muted style="margin:0 0 6px">{{ t('tr_price_note') }}</p><div class=chartwrap><canvas id="chTrPrice"></canvas></div></div>
   <div class=chartbox><h4>{{ t('tr_changes_title') }}</h4><div class=chartwrap><canvas id="chTrChanges"></canvas></div></div>
   <div class=chartbox><h4>{{ t('tr_drops_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chTrDrops"></canvas></div></div>
   <div class=chartbox><h4>{{ t('tr_increases_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chTrIncreases"></canvas></div></div>
   <div class=chartbox><h4>{{ t('tr_avail_title') }}</h4>
    {% if l10n.tr_avail and l10n.tr_avail.empty %}<p class=muted>{{ t('tr_avail_empty') }}</p>{% else %}<div class=chartwrap><canvas id="chTrAvail"></canvas></div>{% endif %}
   </div>
  {% endif %}
 {% else %}
  <p class=muted>{{ t('st_wip') }}</p>
 {% endif %}
</section>
{% endfor %}
<script src="/static/chart.umd.js?v={{ ver }}"></script>
<script src="/static/chartjs-chart-treemap.min.js?v={{ ver }}"></script>
<script>var STATS = {{ data|tojson }}; var STATS_L = {{ l10n|tojson }};</script>
<script src="/static/stats.js?v={{ ver }}"></script>
{% endif %}
{% endblock %}"""

# Rechtsseiten (Impressum/Datenschutz): schlanker Wrapper; der eigentliche Inhalt
# kommt aus Dateien in legal/ (echte Datei bevorzugt, sonst .example, siehe _legal_content).
LEGAL = """{% extends "base" %}{% block body %}
<h2>{{ heading }}</h2>
{% if is_example %}<div class=flash>{{ t('legal_draft_note') }}</div>{% endif %}
<p class=muted>{{ t('legal_lang_note') }}</p>
<div class="legal">{{ body|safe }}</div>
{% if show_contact %}
<div class="legal" style="margin-top:18px">
 <h3>{{ t('contact_h') }}</h3>
 <div class=flash style="max-width:640px">{{ t('contact_intro')|safe }}</div>
 <form method=post action="/impressum/contact?lang={{ lang }}" style="max-width:640px">
  <label>{{ t('contact_name') }}</label><input name=name maxlength=80 required>
  <label>{{ t('contact_email') }}</label><input name=email type=email maxlength=120 required>
  <label>{{ t('contact_tel') }}</label><input name=tel maxlength=40>
  <label>{{ t('contact_msg') }}</label><textarea name=message rows=5 maxlength=2000 required></textarea>
  <label>{{ t('contact_captcha', q=captcha_q) }}</label><input name=captcha maxlength=6 required autocomplete=off inputmode=numeric>
  <input type=hidden name=captcha_sig value="{{ captcha_sig }}">
  <input class=hp type=text name=website tabindex=-1 autocomplete=off>
  <div style="margin-top:12px"><button class=btn>{{ t('contact_send') }}</button></div>
  <p class=muted style="margin-top:8px">{{ t('contact_privacy') }} <a href="/datenschutz?lang={{ lang }}">{{ t('nav_privacy') }}</a></p>
 </form>
</div>
{% endif %}
{% endblock %}"""

MAP = """{% extends "base" %}{% block body %}
<link rel="stylesheet" href="/static/leaflet.css?v={{ v }}">
<link rel="stylesheet" href="/static/MarkerCluster.css?v={{ v }}">
<link rel="stylesheet" href="/static/MarkerCluster.Default.css?v={{ v }}">
<style>
 .mapgrid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:14px;align-items:start}
 .mapgrid>*{min-width:0}   /* Inhalt darf die Spalten nicht aufweiten (Karte bleibt 2/3 breit) */
 @media(max-width:820px){.mapgrid{grid-template-columns:1fr}}
 .mrow{display:flex;gap:9px;align-items:flex-start;padding:7px 2px;border-bottom:1px solid #21262d}
 .mrow .fl2{font-size:13px;color:#8b949e;white-space:nowrap}
 .mrow .nm{font-weight:600;font-size:14px;overflow-wrap:anywhere}
 .mrow .tg{font-size:11px;color:#8b949e;margin-top:2px}
 .mrow>div{min-width:0}
 a.cbtn{display:inline-block;margin-top:4px;padding:2px 9px;border:1px solid #5865f2;border-radius:6px;color:#c9d1d9;background:#5865f222;font-size:12px;text-decoration:none}
 a.cbtn:hover{background:#5865f255}
 .evact{margin-top:4px} .evact a.cbtn{margin:2px 4px 2px 0} a.cbtn.rsvp.on{border-color:#3fb950;background:#3fb95022}
 #agenda .fl2{white-space:normal;overflow-wrap:anywhere}   /* Termine: lange Orte umbrechen */
 .mrow[data-ref]{cursor:pointer} .mrow.hl{background:#1f6feb33;border-radius:6px}
 /* Leaflet ans Board-Dark-Theme angleichen (Zoom-Buttons, Attribution, Popups) */
 .leaflet-container{background:#0f141a}
 .mpin,.mevw,.mcl{background:none;border:0}
 .mpin svg{display:block;filter:drop-shadow(0 0 1px #000)}
 .mev{display:flex;align-items:center;justify-content:center;border-radius:5px;box-sizing:border-box;line-height:1;box-shadow:0 0 2px #000}
 .mcl div{border-radius:50%;text-align:center;font-weight:700;font-size:13px;box-sizing:border-box;box-shadow:0 0 3px #000}
 .lgi{display:inline-flex;align-items:center;gap:4px;margin:2px 12px 2px 0;vertical-align:middle}
 .lgi .mev{display:inline-flex}
 .lgsw{display:inline-block;width:14px;height:12px;border-radius:2px;border:1px solid #0b0f14}
 html[data-mapcolors=contrast] .lgsw{border-color:#fff}
 html[data-mapcolors=contrast] #map{border-color:#ffffff !important}
 #tagfilter a{font-size:12px;padding:2px 9px} #tagfilter .tgrp{font-size:12px;margin-left:6px}
 .leaflet-bar a,.leaflet-bar a:hover{background:#161b22;color:#e6edf3;border-bottom-color:#30363d}
 .leaflet-bar{border:1px solid #30363d}
 .leaflet-control-attribution{background:#161b22cc !important;color:#8b949e}
 .leaflet-control-attribution a{color:#58a6ff}
 .leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#161b22;color:#e6edf3;border:1px solid #30363d}
 .leaflet-popup-content{color:#e6edf3}
 .leaflet-popup-content a{color:#58a6ff}
 a.leaflet-popup-close-button{color:#8b949e}
</style>
<h2>{{ t('map_h') }}</h2>
<p class=muted style="max-width:860px">{{ t('map_intro') }}</p>
<div class=flash style="max-width:860px">ℹ️ {{ t('map_u18_notice') }}</div>
<div class=rangesw>
  <span class=muted>{{ t('map_layer') }}:</span>
  <a href="#" class="on" data-layer="map">{{ t('map_layer_map') }}</a>
  <a href="#" data-layer="events">{{ t('map_layer_events') }}</a>
  <a href="#" data-layer="all">{{ t('map_layer_all') }}</a>
</div>
<div class=rangesw id=choroswitch>
  <span class=muted>{{ t('map_region_level') }}:</span>
  <a href="#" class="on" data-level="bundesland">{{ t('map_level_state') }}</a>
  <a href="#" data-level="plz">{{ t('map_level_plz') }}</a>
</div>
<div class=rangesw id=colorswitch role=group aria-label="{{ t('map_colors') }}">
  <span class=muted>🎨 {{ t('map_colors') }}:</span>
  <a href="#" data-colors="standard" aria-pressed="false">{{ t('map_colors_standard') }}</a>
  <a href="#" data-colors="cvd" aria-pressed="false">{{ t('map_colors_cvd') }}</a>
  <a href="#" data-colors="contrast" aria-pressed="false">{{ t('map_colors_contrast') }}</a>
</div>
<div id=cholegend class=muted style="margin:4px 0;font-size:12px"></div>
<div class=rangesw id=rangeswitch style="display:none">
  <span class=muted>{{ t('map_range_filter') }}:</span>
  <a href="#" data-range="30">{{ t('map_range_30') }}</a>
  <a href="#" data-range="90">{{ t('map_range_90') }}</a>
  <a href="#" class="on" data-range="all">{{ t('map_range_all') }}</a>
</div>
<div id=evlegend class=muted style="display:none;margin:4px 0;font-size:12px"></div>
<div id=pinlegend class=muted style="display:none;margin:4px 0;font-size:12px"></div>
{% if member and tag_groups %}
<details id=tagfilterbox style="margin:4px 0 10px">
<summary class=muted style="cursor:pointer;font-size:13px">🏷️ {{ t('map_tag_filter') }} <span id=tagcount></span></summary>
<div class=rangesw id=tagfilter style="margin-top:8px">
  {% for g in tag_groups %}<span class="muted tgrp">{{ g.label }}</span>
  {% for code, label in g.tags %}<a href="#" data-tag="{{ code }}">{{ label }}</a>{% endfor %}{% endfor %}
  <a href="#" id=tagreset style="display:none">✕ {{ t('map_tag_reset') }}</a>
</div>
</details>
{% endif %}
<div id=mapnotice class=muted style="margin:6px 0"></div>
<div class=mapgrid>
  <div id=map style="height:70vh;min-height:420px;background:#0f141a;border:1px solid #21262d;border-radius:10px"></div>
  <div>
    <div class=chartbox style="margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      {% if member %}<span class=muted>✅ {{ t('map_member_on') }}</span><span class=grow></span>
      <a href="/map/logout?lang={{ lang }}">{{ t('map_logout') }}</a>
      {% else %}<a class=btn href="/map/login?lang={{ lang }}" style="width:100%;box-sizing:border-box;text-align:center">{{ t('map_login') }}</a>{% endif %}
    </div>
    <div class=chartbox id=listbox>
      <h4>{{ t('map_list_title') }}</h4>
      <input id=listsearch placeholder="{{ t('map_search') }}" autocomplete=off>
      <div id=maplist class=col-body style="max-height:58vh;margin-top:8px"></div>
    </div>
    <div class=chartbox id=agendabox style="display:none">
      <h4>{{ t('map_agenda_title') }}</h4>
      <div id=agenda class=col-body style="max-height:50vh"></div>
      {% if ics_url %}
      <div id=calsub style="border-top:1px solid #21262d;margin-top:10px;padding-top:10px">
        <h4>📅 {{ t('map_cal_title') }}</h4>
        <a class=btn href="{{ webcal_url }}" style="width:100%;box-sizing:border-box;text-align:center">{{ t('map_cal_subscribe') }}</a>
        <div style="display:flex;gap:6px;margin-top:8px">
          <input id=calurl value="{{ ics_url }}" readonly style="flex:1;min-width:0;font-size:12px" onclick="this.select()">
          <button type=button class=btn id=calcopy data-done="{{ t('map_cal_copied') }}">{{ t('map_cal_copy') }}</button>
        </div>
        <p class=muted style="font-size:12px;margin:8px 0 0">{{ t('map_cal_hint') }}</p>
      </div>
      <script>
      (function () {
        var btn = document.getElementById("calcopy"), inp = document.getElementById("calurl");
        if (!btn || !inp) return;
        var label = btn.textContent;
        function ok() { btn.textContent = btn.getAttribute("data-done") || label;
                        setTimeout(function () { btn.textContent = label; }, 2000); }
        function legacy() {            // Fallback ohne Clipboard-API (z. B. http:// oder ältere Browser)
          var ta = document.createElement("textarea");
          ta.value = inp.value; ta.setAttribute("readonly", "");
          ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.focus(); ta.select();
          ta.setSelectionRange(0, ta.value.length);
          var done = false;
          try { done = document.execCommand("copy"); } catch (e) { done = false; }
          document.body.removeChild(ta);
          if (done) ok(); else { inp.focus(); inp.select(); }   // notfalls markieren -> Strg+C
        }
        btn.addEventListener("click", function (ev) {
          ev.preventDefault();
          if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(inp.value).then(ok, legacy);
          } else { legacy(); }
        });
      })();
      </script>
      {% endif %}
    </div>
  </div>
</div>
<p class=muted style="margin-top:14px;font-size:12px">{{ t('map_attribution')|safe }}</p>
<script>window.MAP_CFG={lang:"{{ lang }}",member:{{ 'true' if member else 'false' }},maxZoom:{{ max_zoom }},
 emptyText:"{{ t('map_list_empty') }}", tagNoMatch:"{{ t('map_tag_nomatch') }}", choroLabel:"{{ t('map_choro_label') }}",
 csrf:"{{ member_csrf }}",
 evtext:{ics:"{{ t('map_ev_ics') }}",go:"{{ t('map_ev_go') }}",going:"{{ t('map_ev_going') }}",
         count:"{{ t('map_ev_count') }}"},
 pinlabels:{exact:"{{ t('map_pin_exact') }}",coarse:"{{ t('map_pin_coarse') }}",coarseNote:"{{ t('map_pin_coarse_note') }}",contact:"{{ t('map_pin_contact') }}",contactBtn:"{{ t('map_contact_btn') }}"},
 evlabels:{fair:"{{ t('map_evtype_fair') }}",meetup:"{{ t('map_evtype_meetup') }}",shop:"{{ t('map_evtype_shop') }}",talk:"{{ t('map_evtype_talk') }}",field:"{{ t('map_evtype_field') }}",other:"{{ t('map_evtype_other') }}"}};</script>
<script src="/static/leaflet.js?v={{ v }}" onerror="document.getElementById('mapnotice').textContent='{{ t('map_assets_missing') }}'"></script>
<script src="/static/leaflet.markercluster.js?v={{ v }}"></script>
<script src="/static/map.js?v={{ v }}"></script>
{% endblock %}"""

ENV = Environment(loader=DictLoader({"base": BASE, "board": BOARD, "submit": SUBMIT,
                                     "detail": DETAIL, "login": LOGIN, "admin": ADMIN,
                                     "edit": EDIT, "statusdetail": STATUSDETAIL,
                                     "stats": STATS, "legal": LEGAL, "map": MAP}),
                  autoescape=select_autoescape(["html", "xml"], default=True))

_ROWQ = ("SELECT s.*, "
         "(SELECT COUNT(*) FROM board_votes v WHERE v.submission_id=s.id) AS upvotes, "
         "(SELECT COUNT(*) FROM board_comments c WHERE c.submission_id=s.id) AS comments "
         "FROM board_submissions s ")


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
                v=VERSION)    # Cache-Busting für statische Dateien (z. B. Favicon); Seiten dürfen überschreiben
    i18n.update(ctx)   # template-spezifischer Kontext (items, cols, …) ergänzt/gewinnt
    html = ENV.get_template(name).render(title=title, flash=flash, admin=_is_admin(req), **i18n)
    return web.Response(text=html, content_type="text/html")


def _ver_key(v: str) -> tuple:
    """Semantischer Versions-Sortierschlüssel: '1.10.0' > '1.9.0'. Leere/fehlende
    Version -> (0,0,0,0), landet damit hinter allen echten Versionen."""
    parts = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    return tuple(parts) + (0,) * (4 - len(parts))


async def _rows(where="", params=()):
    return [dict(r) for r in await board_query(_ROWQ + where, params)]


async def _one(sid):
    r = await board_one(_ROWQ + "WHERE s.id=?", (sid,))
    return dict(r) if r else None


async def _comments(sid):
    rows = await board_query(
        "SELECT * FROM board_comments WHERE submission_id=? ORDER BY id ASC", (sid,))
    return [dict(r) for r in rows]


# ── Status-Dashboard / Health-Checks ──────────────────────────────────────────
def _file_age_seconds(path) -> float | None:
    """Alter der Datei in Sekunden (mtime) oder None, wenn sie fehlt/unlesbar ist."""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _fmt_age(sec: float | None) -> str:
    """Menschlich lesbares Alter, z.B. 'vor 2 h 14 min' / 'vor 3 Tagen'."""
    if sec is None:
        return "unbekannt"
    sec = int(sec)
    if sec < 90:
        return "gerade eben"
    m = sec // 60
    if m < 60:
        return f"vor {m} min"
    h = m // 60
    if h < 24:
        rem = m % 60
        return f"vor {h} h {rem} min" if rem else f"vor {h} h"
    d = h // 24
    return f"vor {d} {'Tag' if d == 1 else 'Tagen'}"


def _gb(nbytes) -> str:
    """Bytes -> GB-String mit einer Nachkommastelle."""
    try:
        return f"{nbytes / 1024 ** 3:.1f}"
    except (TypeError, ValueError, ZeroDivisionError):
        return "?"


def _system_metrics() -> dict:
    """Momentane Systemauslastung (anzeige-only, KEIN Incident-Logging, ohne Einfluss
    auf den Gesamtstatus): CPU als Load-Average (1/5/15 min, relativ zu den Kernen),
    RAM- und SSD-Auslastung. Ampel je Metrik. Einzelne Fehler -> None für die Metrik."""
    out: dict = {}
    try:
        la1, la5, la15 = os.getloadavg()                 # nur Linux/Unix
        cores = psutil.cpu_count() or 1
        ratio = la1 / cores
        out["cpu"] = {"state": "ok" if ratio < 0.7 else ("warn" if ratio < 1.0 else "down"),
                      "load": [round(la1, 2), round(la5, 2), round(la15, 2)], "cores": cores}
    except Exception:                                    # z.B. kein getloadavg (Windows)
        out["cpu"] = None
    try:
        vm = psutil.virtual_memory()
        out["ram"] = {"state": "ok" if vm.percent < 75 else ("warn" if vm.percent < 90 else "down"),
                      "percent": round(vm.percent), "used": vm.used, "total": vm.total}
    except Exception:
        out["ram"] = None
    try:
        du = psutil.disk_usage("/")
        out["disk"] = {"state": "ok" if du.percent < 80 else ("warn" if du.percent < 90 else "down"),
                       "percent": round(du.percent), "used": du.used, "total": du.total}
    except Exception:
        out["disk"] = None
    return out


def _metrics_chip(m: dict) -> str:
    """Kompakte, farbige HTML-Zeile (Load · RAM · SSD) für die Status-Übersicht.
    Wird server-seitig gerendert (No-JS-Fallback) und vom 5-s-Poll aktualisiert."""
    _cls = {"ok": "mok", "warn": "mwarn", "down": "mdown"}
    parts = []
    cpu, ram, disk = m.get("cpu"), m.get("ram"), m.get("disk")
    if cpu:
        lo = cpu["load"]
        title = f"1/5/15 min: {lo[0]:.2f} / {lo[1]:.2f} / {lo[2]:.2f} · {cpu['cores']} Kerne"
        parts.append(f'<span class="{_cls.get(cpu["state"], "")}" title="{title}">Load {lo[0]:.2f}</span>')
    if ram:
        parts.append(f'<span class="{_cls.get(ram["state"], "")}" title="{_gb(ram["used"])}/{_gb(ram["total"])} GB">RAM {ram["percent"]}%</span>')
    if disk:
        parts.append(f'<span class="{_cls.get(disk["state"], "")}" title="{_gb(disk["used"])}/{_gb(disk["total"])} GB">SSD {disk["percent"]}%</span>')
    return "⚙️ " + " · ".join(parts) if parts else ""


_WD_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _loop_next(loop) -> str:
    """Nächster geplanter Lauf eines tasks.loop in Berliner Zeit (MEZ/MESZ).

    ``next_iteration`` liefert discord.py UTC-aware; die Umrechnung erfolgt explizit
    nach Europe/Berlin – unabhängig von der Server-Zeitzone. Das Datum wird nur dann
    mitgezeigt, wenn der nächste Lauf NICHT heute ist (sonst nur Uhrzeit), damit bei
    seltenen Jobs (wöchentlich/…) 'HH:MM' nicht mehrdeutig ist:
      heute   → '19:15 MESZ'
      morgen  → 'morgen 09:00 MESZ'
      später  → 'So, 03.08. 09:00 MESZ'"""
    nxt = getattr(loop, "next_iteration", None)
    if nxt is None:
        return ""
    try:
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        local = nxt.astimezone(BERLIN)
        label = "MESZ" if local.dst() else "MEZ"
        days = (local.date() - datetime.now(BERLIN).date()).days
        if days <= 0:
            return f"{local:%H:%M:%S} {label}"
        if days == 1:
            return f"morgen {local:%H:%M:%S} {label}"
        return f"{_WD_DE[local.weekday()]}, {local:%d.%m.} {local:%H:%M:%S} {label}"
    except Exception:
        return ""


def _loop_interval(loop) -> str:
    """Kurzbeschreibung des Loop-Intervalls, z.B. 'alle 65 min' / 'alle 2 h'.
    Für zeitgesteuerte Loops (fester Uhrzeit-Trigger) ''."""
    try:
        # py-cord speichert diese Werte als float → int, damit z.B. 'alle 5 min'
        # statt 'alle 5.0 min' erscheint.
        h = int(getattr(loop, "hours", 0) or 0)
        m = int(getattr(loop, "minutes", 0) or 0)
        s = int(getattr(loop, "seconds", 0) or 0)
        total_min = h * 60 + m
        if total_min:
            if h and not m:
                if h == 1:
                    return "stündlich"
                if h % 24 == 0:
                    d = h // 24
                    return "täglich" if d == 1 else f"alle {d} Tage"
                return f"alle {h} h"
            return f"alle {total_min} min"
        if s:
            return f"alle {s} s"
    except Exception:
        pass
    return ""


# Registry ALLER In-Bot-Hintergrundjobs (discord.ext.tasks-Loops):
# (Cog-Name, Loop-Attribut, Anzeige-Label, kritisch?, Notiz) – kritisch → 'down' bei
# Ausfall, sonst 'warn'. Notiz = optionaler Zusatz (z.B. wenn der Loop öfter tickt als
# er tatsächlich etwas tut).
_BOT_JOBS = [
    ("Tasks",         "check_availability",       "Verfügbarkeits-Check",              True,  ""),
    ("PriceTracking", "flush_removed_variants",   "Entfallene Varianten (Sammel-DM)",  False, ""),
    ("Digest",        "weekly_digest",            "Wochen-Digest",                     False, "Versand nur montags"),
    ("Tasks",         "sync_shop_ratings",        "Shop-Bewertungen synchronisieren",  False, ""),
    ("Tasks",         "expire_old_notifications", "Alte Benachrichtigungen entfernen", False, ""),
    ("Tasks",         "optimize_db",              "DB-Optimierung (VACUUM)",           False, ""),
    ("Tasks",         "update_bot_status",        "Bot-Statusanzeige aktualisieren",   False, ""),
    ("CommandLog",    "flush_log",                "Command-Log schreiben",             False, ""),
    ("CommandLog",    "cleanup_log",              "Command-Log aufräumen (Retention)", False, ""),
    ("OfferAlerts",   "scan_offers",              "Angebote-Schlagwort-Scanner",       False, ""),
    ("AiChatCog",     "cleanup_loop",             "KI-Chat · Verläufe aufräumen",      False, ""),
    ("AiChatCog",     "shop_data_loop",           "KI-Chat · Shop-Daten-Refresh",      False, ""),
]


def _pipeline_tile(bot) -> dict:
    """Kachel für die stündliche Daten-Pipeline (Shop-Reload → Preis → Arten), die
    im Tasks-Cog als ``reload_shops_task`` läuft. Zeigt nächsten Lauf + je Schritt
    das Ergebnis des letzten Laufs (aus ``TasksCog.pipeline_last``)."""
    name = "Daten-Pipeline (Shop-Reload → Preis → Arten)"
    try:
        cog = bot.get_cog("Tasks") if bot else None
        loop = getattr(cog, "reload_shops_task", None) if cog else None
        if loop is None:
            return dict(name=name, state="down", detail="Cog/Loop nicht geladen")
        if loop.failed():
            return dict(name=name, state="down", detail="fehlerhaft (Exception im Loop)")
        if not loop.is_running():
            return dict(name=name, state="down", detail="gestoppt")
        nxt = _loop_next(loop)
        detail = "läuft · stündlich" + (f" · nächster Lauf {nxt}" if nxt else "")
        state = "ok"
        last = getattr(cog, "pipeline_last", None)
        if last and last.get("steps"):
            marks = " · ".join(f"{lbl} {'✓' if ok else '✗'}" for lbl, ok in last["steps"])
            detail += f" · zuletzt {last.get('at', '')}: {marks}"
            if not all(ok for _, ok in last["steps"]):
                state = "warn"
        return dict(name=name, state=state, detail=detail)
    except Exception as e:
        return dict(name=name, state="warn", detail=str(e)[:80])


def _job_tile(bot, cog_name: str, attr: str, label: str, critical: bool, note: str = "") -> dict:
    """Health-Kachel für einen discord.ext.tasks-Loop: läuft / fehlerhaft / gestoppt?
    Bei laufendem Loop zusätzlich Intervall + nächster Lauf (Berliner Zeit) + optionale Notiz."""
    down = "down" if critical else "warn"
    try:
        cog = bot.get_cog(cog_name) if bot else None
        loop = getattr(cog, attr, None) if cog else None
        if loop is None:
            return dict(name=label, state=down, detail="Cog/Loop nicht geladen")
        if loop.failed():
            return dict(name=label, state="down", detail="fehlerhaft (Exception im Loop)")
        if not loop.is_running():
            return dict(name=label, state=down, detail="gestoppt")
        nxt = _loop_next(loop)
        iv = _loop_interval(loop)
        detail = "läuft" + (f" · {iv}" if iv else "") + (f" · nächster Lauf {nxt}" if nxt else "")
        if note:
            detail += f" · {note}"
        return dict(name=label, state="ok", detail=detail)
    except Exception as e:
        return dict(name=label, state="warn", detail=str(e)[:80])


def _grabber_cron_tile() -> dict:
    """EIN Cronjob (stündlich, als Nutzer 'aam') erzeugt in einem Lauf BEIDE Dateien:
    ``shops_data.json`` (jeder Lauf) und ``price_history.db`` (Preis-Historie).
    Deshalb EINE gemeinsame Kachel statt zweier getrennter (es gibt nicht zwei Jobs).

    Ampel-Status nach ``shops_data.json`` (wird jeden Lauf neu geschrieben → verlässlich).
    Die Preis-Historie wird zwar nur bei echten Preisänderungen fortgeschrieben, der
    Grabber ``touch()``t sie aber nach jedem erfolgreichen Lauf – bleibt sie trotzdem
    deutlich zurück (> 7 Tage), deutet das auf einen Ausfall des Preis-Schritts hin → gelb."""
    name = "Grabber · Shop-Daten + Preis-Historie (stündlich)"
    age = _file_age_seconds(SHOPS_DATA_FILE)
    if age is None:
        return dict(name=name, state="down", detail="shops_data.json fehlt")
    state = "ok" if age < 3 * 3600 else ("warn" if age < 24 * 3600 else "down")
    ph = _file_age_seconds(Path(DATA_DIRECTORY) / "price_history.db")
    if ph is None:
        ph_txt = "Preis-Historie fehlt"
    else:
        ph_txt = f"Preis-Historie {_fmt_age(ph)}"
        if ph > 168 * 3600 and state == "ok":
            state = "warn"
            ph_txt += " ⚠️"
    return dict(name=name, state=state, detail=f"Shop-Daten {_fmt_age(age)} · {ph_txt}")


def _cron_tile(name: str, path, *, warn_h: int, down_h: int, optional: bool = False) -> dict:
    """Health-Kachel für einen EXTERNEN Cronjob (läuft als Nutzer 'aam', nicht im
    Bot-Prozess). Status wird aus dem Alter der erzeugten Datei abgeleitet."""
    age = _file_age_seconds(path)
    if age is None:
        if optional:
            return dict(name=name, state="off", detail="noch nicht erzeugt (optional)")
        return dict(name=name, state="down", detail=f"{Path(path).name} fehlt")
    state = "ok" if age < warn_h * 3600 else ("warn" if age < down_h * 3600 else "down")
    return dict(name=name, state=state, detail=f"aktualisiert {_fmt_age(age)}")


async def _collect_health(app, lang: str = "de"):
    """Sammelt alle Health-Checks in Sektionen (Kern · In-Bot-Jobs · externe Cronjobs).
    Jeder Check ist gekapselt (ein Fehler bricht die Seite nicht ab). state ∈ ok|warn|down|off;
    'off' (grau) = bewusst deaktiviert/optional und zählt NICHT gegen den Gesamtstatus.
    Rückgabe: (overall, sections). Lokalisiert werden Gesamt-Ampel + Sektions-Titel/-Notizen;
    die einzelnen Kachel-Namen/-Details bleiben Deutsch (dienen als stabile Vorfall-Schlüssel)."""
    bot = app.get("bot")

    # ── Sektion 1: Kern (Verbindung, Datenbanken, Feature-Flags) ──────────────
    core: list[dict] = []
    try:
        if bot is None:
            core.append(dict(name="Discord-Bot", state="down", detail="Bot-Objekt nicht verfügbar"))
        elif not bot.is_ready():
            core.append(dict(name="Discord-Bot", state="warn", detail="verbindet …"))
        else:
            lat = bot.latency  # Sekunden; kann inf/nan sein, bevor der erste Heartbeat kam
            if lat != lat or lat in (float("inf"), 0):
                core.append(dict(name="Discord-Bot", state="warn", detail="online · Latenz unbekannt"))
            else:
                ms = round(lat * 1000)
                core.append(dict(name="Discord-Bot", state="ok" if ms < 500 else "warn",
                                 detail=f"online · {ms} ms Latenz"))
    except Exception as e:
        core.append(dict(name="Discord-Bot", state="down", detail=str(e)[:80]))
    try:
        await execute_db(bot, "SELECT 1", fetch=True)
        core.append(dict(name="Hauptdatenbank", state="ok", detail="erreichbar"))
    except Exception as e:
        core.append(dict(name="Hauptdatenbank", state="down", detail=str(e)[:80]))
    try:
        await board_query("SELECT 1")
        core.append(dict(name="Board-Datenbank", state="ok", detail="erreichbar"))
    except Exception as e:
        core.append(dict(name="Board-Datenbank", state="down", detail=str(e)[:80]))
    core.append(dict(name="KI-Chat (öffentlich)", state="ok" if AI_CHAT_PUBLIC else "off",
                     detail="aktiv" if AI_CHAT_PUBLIC else "deaktiviert"))

    # ── Sektion 2: Hintergrund-Jobs IM Bot-Prozess (discord.ext.tasks) ────────
    jobs = [_pipeline_tile(bot)] + [_job_tile(bot, c, a, lbl, crit, note)
                                    for (c, a, lbl, crit, note) in _BOT_JOBS]

    # ── Sektion 3: EXTERNE Cronjobs (laufen als Nutzer 'aam', nicht im Bot) ───
    cron = [
        _grabber_cron_tile(),
        _cron_tile("Artenliste · AntCat-Build (monatlich)", SPECIES_CATALOG_FILE,
                   warn_h=40 * 24, down_h=1000 * 24, optional=True),
    ]

    sections = [
        dict(title=translate(lang, "sec_core"), note=translate(lang, "sec_core_note"), checks=core),
        dict(title=translate(lang, "sec_jobs"), note=translate(lang, "sec_jobs_note"), checks=jobs),
        dict(title=translate(lang, "sec_cron"), note=translate(lang, "sec_cron_note"), checks=cron),
    ]
    states = {c["state"] for sec in sections for c in sec["checks"]}
    if "down" in states:
        overall = ("down", translate(lang, "overall_down"))
    elif "warn" in states:
        overall = ("warn", translate(lang, "overall_warn"))
    else:
        overall = ("ok", translate(lang, "overall_ok"))
    return overall, sections


async def _record_incidents(bot) -> None:
    """Schreibt die Vorfall-Historie fort: pro Check offenen Vorfall öffnen/aktualisieren
    (warn/down) bzw. schließen (ok/off → ended_at). Wird minütlich vom Monitor-Loop
    aufgerufen. 'off' (grau/deaktiviert) zählt wie OK (kein Vorfall)."""
    try:
        _, sections = await _collect_health({"bot": bot})
    except Exception as e:
        logger.warning("⚠️ Incident-Monitor: Health-Erhebung fehlgeschlagen: %s", e)
        return
    for c in (chk for sec in sections for chk in sec["checks"]):
        key, state, detail = c["name"], c["state"], c.get("detail", "")
        try:
            open_row = await board_one(
                "SELECT id, state FROM board_incidents WHERE check_key=? AND ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1", (key,))
            if state in ("warn", "down"):
                if open_row is None:
                    await board_exec(
                        "INSERT INTO board_incidents (check_key, state, detail) VALUES (?,?,?)",
                        (key, state, detail))
                elif open_row["state"] != state:   # warn<->down: Zustand/Detail aktualisieren
                    await board_exec(
                        "UPDATE board_incidents SET state=?, detail=? WHERE id=?",
                        (state, detail, open_row["id"]))
            elif open_row is not None:             # wieder OK -> Vorfall schließen
                await board_exec(
                    "UPDATE board_incidents SET ended_at=datetime('now') WHERE id=?",
                    (open_row["id"],))
        except Exception as e:
            logger.warning("⚠️ Incident-Monitor: Check '%s' fehlgeschlagen: %s", key, e)


# ── Handlers ──────────────────────────────────────────────────────────────────
async def h_board(req):
    lang = pick_lang(req)
    items = await _rows("WHERE status!='pending' ORDER BY id DESC")
    # 'Erledigt'-Karten tragen eine Version -> nach Version absteigend (neueste oben);
    # alle anderen Spalten haben keine Version ((0,0,0,0)) und bleiben so bei id DESC.
    items.sort(key=lambda c: (_ver_key(c.get("version") or ""), c.get("id") or 0), reverse=True)
    overall, sections = await _collect_health(req.app, lang)
    flash = flash_text(lang, req.query.get("m", ""), n=req.query.get("n", ""), s=req.query.get("s", ""))
    resp = _render(req, "board", title=translate(lang, "nav_board"), items=items, cols=PUBLIC_COLS,
                   overall=overall, sections=sections, version=VERSION,
                   generated=now_berlin("%H:%M:%S"), metrics_html=_metrics_chip(_system_metrics()),
                   flash=flash)
    resp.headers["Cache-Control"] = "no-store"   # kein veraltetes HTML aus Proxy/Browser-Cache
    return resp


# Zuordnung Diagramm-Canvas -> i18n-Erklärungsschlüssel (für Hover-Tooltips, via stats.js).
_CHART_EXP = {
    "chCountries": "exp_countries", "chStock": "exp_stock",
    "chGenera": "exp_genera", "chReach": "exp_reach", "chLongtail": "exp_longtail",
    "chShopOffers": "exp_shop_offers", "chShopBreadth": "exp_shop_breadth",
    "chShopExclusive": "exp_shop_exclusive", "chShopScatter": "exp_shop_scatter",
    "chPriceHist": "exp_price_hist", "chPriceGenus": "exp_price_genus", "chPriceSpread": "exp_price_spread",
    "chPriceSpreadSmall": "exp_price_spread_small",
    "chAvGenus": "exp_av_genus", "chAvCountry": "exp_av_country", "chAvShopBest": "exp_av_shop_best",
    "chAvShopWorst": "exp_av_shop_worst", "chAvHardest": "exp_av_hardest",
    "chDqShopUncanon": "exp_dq_shop_uncanon", "chDqShopAdjusted": "exp_dq_shop_adjusted",
    "chDqVariants": "exp_dq_variants",
    "chTrPrice": "exp_tr_price", "chTrChanges": "exp_tr_changes", "chTrDrops": "exp_tr_drops",
    "chTrIncreases": "exp_tr_increases", "chTrAvail": "exp_tr_avail",
}


def _top10(pairs, other_label=None):
    """Aus [(label, wert), …] die Top 10 als (labels, values). Ist *other_label*
    gesetzt und gibt es einen Rest, wird dieser als eine „übrige"-Position summiert."""
    top = pairs[:10]
    labels = [k for k, _ in top]
    values = [v for _, v in top]
    if other_label is not None:
        rest = sum(v for _, v in pairs[10:])
        if rest:
            labels.append(other_label)
            values.append(rest)
    return labels, values


def _stats_l10n(lang: str, data: dict, ts: dict = None) -> dict:
    """Sprachabhängige Beschriftungen für die JS-Diagramme (die Rohzahlen in `data`
    sind sprachneutral). Wird als eigene JSON-Insel `STATS_L` an die Seite gegeben.
    Ranglisten: Top 10, Rest wo sinnvoll als „übrige" gruppiert."""
    ov = data["overview"]
    other = translate(lang, "lbl_other")

    # Block 1: Länder (lokalisierte Namen), Top 10 + übrige
    named = [(country_name(lang, iso), n) for iso, n in ov.get("countries", [])]
    c_labels, c_values = _top10(named, other)
    out = {
        "countries": {
            "title": translate(lang, "ch_countries_title"),
            "axis": translate(lang, "ch_countries_axis"),
            "labels": c_labels, "values": c_values,
        },
        "stock": {
            "title": translate(lang, "ch_stock_title"),
            "labels": [translate(lang, "lbl_instock"), translate(lang, "lbl_outstock")],
            "values": [ov.get("instock_live", 0), ov.get("out_of_stock_live", 0)],
        },
    }

    # ── Block 2: Arten & Gattungen ──────────────────────────────────────────
    sp = data.get("species")
    if sp:
        gtop = sp["genera"][:10]
        grest = sum(n for _, n in sp["genera"][10:])
        gdata = [{"g": g, "v": n} for g, n in gtop]
        if grest:
            gdata.append({"g": other, "v": grest})
        out["genera"] = {"title": translate(lang, "sp_genera_title"), "data": gdata}
        r_labels, r_values = _top10(sp["reach"])           # Arten: kein „übrige"
        out["reach"] = {"title": translate(lang, "sp_reach_title"),
                        "axis": translate(lang, "lbl_shops"),
                        "labels": r_labels, "values": r_values}
        out["longtail"] = {
            "title": translate(lang, "sp_longtail_title"),
            "x": translate(lang, "sp_longtail_x"),
            "y": translate(lang, "sp_longtail_y"),
            "labels": [str(k) for k, _ in sp["longtail"]],
            "values": [n for _, n in sp["longtail"]],
        }

    # ── Block 3: Shop-Vergleich ─────────────────────────────────────────────
    sh = data.get("shops")
    if sh:
        o_labels, o_values = _top10(sh["by_offers"], other)
        out["shop_offers"] = {"title": translate(lang, "sh_offers_title"),
                              "axis": translate(lang, "lbl_offers"),
                              "labels": o_labels, "values": o_values}
        b_labels, b_values = _top10(sh["by_breadth"])      # Breite: kein sinnvoller Summen-Rest
        out["shop_breadth"] = {"title": translate(lang, "sh_breadth_title"),
                               "axis": translate(lang, "lbl_species"),
                               "labels": b_labels, "values": b_values}
        e_labels, e_values = _top10(sh["by_exclusive"], other)
        out["shop_exclusive"] = {"title": translate(lang, "sh_exclusive_title"),
                                 "axis": translate(lang, "lbl_species"),
                                 "labels": e_labels, "values": e_values}
        out["shop_scatter"] = {
            "title": translate(lang, "sh_scatter_title"),
            "x": translate(lang, "sh_scatter_x"),
            "y": translate(lang, "sh_scatter_y"),
            "points": [{"x": p["species"], "y": p["offers"], "label": p["shop"]}
                       for p in sh["scatter"]],
        }

    # ── Block 4: Preise ─────────────────────────────────────────────────────
    pr = data.get("prices")
    if pr and pr.get("stats", {}).get("n"):
        out["price_hist"] = {"title": translate(lang, "pr_hist_title"),
                             "x": translate(lang, "pr_hist_x"),
                             "y": translate(lang, "pr_hist_y"),
                             "labels": pr["hist"]["labels"], "values": pr["hist"]["counts"]}
        out["price_genus"] = {"title": translate(lang, "pr_genus_title"),
                              "axis": translate(lang, "pr_genus_axis"),
                              "labels": [g for g, _ in pr["genus_median"]],
                              "values": [v for _, v in pr["genus_median"]]}
        _shops_lbl = translate(lang, "lbl_shops")
        out["price_spread"] = {"title": translate(lang, "pr_spread_title"),
                               "axis": translate(lang, "pr_spread_axis"),
                               "labels": [f"{s[0]} ({s[4]} {_shops_lbl})" for s in pr["spread"]],
                               "ranges": [[s[1], s[2]] for s in pr["spread"]]}
        out["price_spread_small"] = {"title": translate(lang, "pr_spread_small_title"),
                                     "axis": translate(lang, "pr_spread_axis"),
                                     "labels": [f"{s[0]} ({s[4]} {_shops_lbl})" for s in pr.get("spread_small", [])],
                                     "ranges": [[s[1], s[2]] for s in pr.get("spread_small", [])]}

    # ── Block 5: Verfügbarkeit ──────────────────────────────────────────────
    av = data.get("availability")
    if av:
        rate_axis = translate(lang, "lbl_instock_rate")
        out["av_genus"] = {"title": translate(lang, "av_genus_title"), "axis": rate_axis,
                           "labels": [g for g, _ in av["by_genus"]],
                           "values": [r for _, r in av["by_genus"]]}
        out["av_country"] = {"title": translate(lang, "av_country_title"), "axis": rate_axis,
                             "labels": [country_name(lang, iso) for iso, _, _ in av["by_country"]],
                             "values": [r for _, r, _ in av["by_country"]]}
        out["av_shop_best"] = {"title": translate(lang, "av_shop_best_title"), "axis": rate_axis,
                               "labels": [s for s, _, _ in av["shop_best"]],
                               "values": [r for _, r, _ in av["shop_best"]]}
        out["av_shop_worst"] = {"title": translate(lang, "av_shop_worst_title"), "axis": rate_axis,
                                "labels": [s for s, _, _ in av["shop_worst"]],
                                "values": [r for _, r, _ in av["shop_worst"]]}
        out["av_hardest"] = {"title": translate(lang, "av_hardest_title"),
                             "axis": translate(lang, "lbl_shops"),
                             "labels": [f"{s} ({r}%)" for s, r, sh, of in av["hardest"]],
                             "values": [sh for s, r, sh, of in av["hardest"]]}

    # ── Block 6: Datenqualität ──────────────────────────────────────────────
    q = data.get("quality")
    if q:
        out["dq_shop_uncanon"] = {"title": translate(lang, "dq_shop_uncanon_title"),
                                  "axis": translate(lang, "dq_shop_uncanon_axis"),
                                  "labels": [s for s, _ in q["shop_uncanon"]],
                                  "values": [n for _, n in q["shop_uncanon"]]}
        out["dq_shop_adjusted"] = {"title": translate(lang, "dq_shop_adjusted_title"),
                                   "axis": translate(lang, "lbl_adjusted"),
                                   "labels": [f"{s} ({r}%)" for s, r, ac, cn in q["shop_adjusted"]],
                                   "values": [ac for s, r, ac, cn in q["shop_adjusted"]]}
        out["dq_variants"] = {"title": translate(lang, "dq_variants_title"),
                              "axis": translate(lang, "dq_variants_axis"),
                              "labels": [s for s, _ in q["variants"]],
                              "values": [n for _, n in q["variants"]]}

    # ── Block 7: Zeitverläufe ───────────────────────────────────────────────
    if ts and ts.get("available"):
        pot = ts.get("price_over_time", [])
        out["tr_price"] = {"title": translate(lang, "tr_price_title"),
                           "note": translate(lang, "tr_price_note"),
                           "axis": translate(lang, "tr_price_axis"),
                           "x": translate(lang, "tr_month_axis"),
                           "labels": [m for m, _, _ in pot], "values": [v for _, v, _ in pot]}
        ch = ts.get("changes_per_month", [])
        out["tr_changes"] = {"title": translate(lang, "tr_changes_title"),
                             "x": translate(lang, "tr_month_axis"),
                             "y": translate(lang, "tr_count_axis"),
                             "down_label": translate(lang, "tr_changes_down"),
                             "up_label": translate(lang, "tr_changes_up"),
                             "labels": [m for m, _, _ in ch],
                             "down": [d for _, d, _ in ch], "up": [u for _, _, u in ch]}

        def _dr(items):
            return {"labels": [i[0] for i in items], "values": [i[3] for i in items],
                    "info": [f"{i[1]} € → {i[2]} €" for i in items]}
        out["tr_drops"] = {"title": translate(lang, "tr_drops_title"),
                           "axis": translate(lang, "tr_pct_change"), **_dr(ts.get("price_drops", []))}
        out["tr_increases"] = {"title": translate(lang, "tr_increases_title"),
                               "axis": translate(lang, "tr_pct_change"), **_dr(ts.get("price_increases", []))}
        av = ts.get("avail_over_time") or []
        out["tr_avail"] = {"title": translate(lang, "tr_avail_title"),
                           "axis": translate(lang, "lbl_instock_rate"),
                           "x": translate(lang, "tr_month_axis"),
                           "labels": [m for m, _, _ in av], "values": [r for _, r, _ in av],
                           "empty": not ts.get("has_stock")}

    # Hover-Erklärungen je Diagramm (Canvas-ID -> Text); stats.js hängt das ⓘ an die Überschrift.
    out["exp"] = {cid: translate(lang, key) for cid, key in _CHART_EXP.items()}
    return out


async def h_impressum(req):
    """Impressum (§ 5 DDG) – Inhalt aus legal/impressum(.example).html + Kontaktformular."""
    lang = pick_lang(req)
    body, is_example = _legal_content("impressum")
    q, sig = _make_captcha()
    flash = flash_text(lang, req.query.get("m", ""))
    return _render(req, "legal", title=translate(lang, "nav_impressum"),
                   heading=translate(lang, "nav_impressum"), body=body, is_example=is_example,
                   show_contact=bool(BOARD_OWNER_ID), captcha_q=q, captcha_sig=sig, flash=flash)


async def h_datenschutz(req):
    """Datenschutzerklärung (Art. 13 DSGVO) – Inhalt aus legal/datenschutz(.example).html."""
    lang = pick_lang(req)
    body, is_example = _legal_content("datenschutz")
    return _render(req, "legal", title=translate(lang, "nav_privacy"),
                   heading=translate(lang, "nav_privacy"), body=body, is_example=is_example,
                   show_contact=False)


async def h_impressum_contact(req):
    """Kontaktformular der Impressum-Seite -> Discord-DM an den Owner.
    Schutz: Honeypot, Rate-Limit, Rechenaufgabe (Captcha). Pflicht: Name, E-Mail, Nachricht."""
    lang = pick_lang(req)
    d = await req.post()
    if (d.get("website") or "").strip():                          # Honeypot -> still „ok"
        raise web.HTTPFound(f"/impressum?m=contact_sent&lang={lang}")
    if not _rate("contact:" + _ip(req), 3, 3600):
        raise web.HTTPFound(f"/impressum?m=contact_toomany&lang={lang}")
    if not _captcha_ok(d):
        raise web.HTTPFound(f"/impressum?m=contact_captcha&lang={lang}")
    name = (d.get("name") or "").strip()[:80]
    email = (d.get("email") or "").strip()[:120]
    message = (d.get("message") or "").strip()[:2000]
    if not name or not message or "@" not in email:
        raise web.HTTPFound(f"/impressum?m=contact_empty&lang={lang}")
    ok = await _send_contact_dm(req.app, message, name, email, (d.get("tel") or "").strip()[:40])
    raise web.HTTPFound(f"/impressum?m={'contact_sent' if ok else 'contact_fail'}&lang={lang}")


async def h_stats(req):
    """Öffentliche Statistik-Seite. Aggregiert live aus shops_data.json (15-min-Cache).
    Währungskurse werden zuvor sichergestellt (für die späteren EUR-Preisblöcke)."""
    lang = pick_lang(req)
    range_key = req.query.get("range", "12")
    if range_key not in shop_stats.RANGE_MONTHS:
        range_key = "12"
    data = l10n = ts = None
    try:
        await ensure_rates()                                   # EZB/Frankfurter + Fallback
        data = await asyncio.to_thread(shop_stats.compute)     # Datei-I/O + CPU im Thread
        ts = await asyncio.to_thread(shop_stats.compute_timeseries, range_key)
    except FileNotFoundError:
        logger.warning("📊 Stats: shops_data.json nicht gefunden (%s)", SHOPS_DATA_FILE)
    except Exception as e:
        logger.warning("📊 Stats-Aggregation fehlgeschlagen: %s", e, exc_info=True)
    if data:
        l10n = _stats_l10n(lang, data, ts)
    resp = _render(req, "stats", title=translate(lang, "nav_stats"), data=data, l10n=l10n,
                   ts_available=bool(ts and ts.get("available")), ts_range=range_key, ver=VERSION)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


async def h_status_json(req):
    """Nur die Health-Daten als JSON – fürs 5-Sekunden-Polling des Status-Bereichs
    (der Rest der Seite wird NICHT neu geladen)."""
    overall, sections = await _collect_health(req.app, pick_lang(req))
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
        _, sections = await _collect_health(req.app, pick_lang(req))
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
                   current=current, incidents=incidents, csrf=_csrf_token())


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


async def h_incident_note(req):
    d = await _admin_guard(req)
    cid = int(req.match_info["id"])
    key = (d.get("key") or "").strip()
    await board_exec("UPDATE board_incidents SET admin_note=? WHERE id=?",
                     ((d.get("note") or "").strip()[:500], cid))
    from urllib.parse import quote
    lang = pick_lang(req)
    raise web.HTTPFound(_safe_local_redirect(f"/status/check/{quote(key, safe='')}?lang={lang}") if key else f"/?lang={lang}")


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
        resp.set_cookie(_VOTER_COOKIE, vid, max_age=31536000, httponly=True, samesite="Lax")
    await board_exec("INSERT OR IGNORE INTO board_votes (submission_id, voter_hash) VALUES (?,?)",
                     (sid, _hmac("vote", _ip(req), vid)))
    raise resp


async def h_detail(req):
    sub = await _one(int(req.match_info["id"]))
    if not sub or (sub["status"] == "pending" and not _is_admin(req)):
        raise web.HTTPFound("/")
    comments = await _comments(sub["id"])
    return _render(req, "detail", title=sub["title"], c=sub, comments=comments)


# ── Admin ─────────────────────────────────────────────────────────────────────
async def h_login_form(req):
    return _render(req, "login", title=translate(pick_lang(req), "login_h"))


async def h_login(req):
    lang = pick_lang(req)
    d = await req.post()
    if BOARD_ADMIN_TOKEN and hmac.compare_digest((d.get("token") or ""), BOARD_ADMIN_TOKEN):
        resp = web.HTTPFound(f"/admin?lang={lang}")
        resp.set_cookie(_ADMIN_COOKIE, _hmac("owner", BOARD_ADMIN_TOKEN),
                        max_age=604800, httponly=True, samesite="Lax")
        raise resp
    return _render(req, "login", title=translate(lang, "login_h"),
                   flash=translate(lang, "flash_wrong_token"))


async def h_logout(req):
    resp = web.HTTPFound("/")
    resp.del_cookie(_ADMIN_COOKIE)
    raise resp


async def h_admin(req):
    if not _is_admin(req):
        raise web.HTTPFound("/admin/login")
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
                   items=items, queue=queue, csrf=_csrf_token(),
                   statuses=STATUSES, priorities=PRIORITIES, components=COMPONENTS)


async def _admin_guard(req):
    if not _is_admin(req):
        raise web.HTTPFound("/admin/login")
    d = await req.post()
    if not _csrf_ok(d):
        raise web.HTTPForbidden(text="CSRF-Token ungültig")
    return d


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
                   csrf=_csrf_token(), types=TYPES, statuses=STATUSES,
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


# ════════════════════════════════════════════════════════════════════════════
#  HALTER-KARTE (Map-Feature) – Web-Teil: OAuth-Login, Seite, JSON-APIs
# ════════════════════════════════════════════════════════════════════════════
try:
    from dateutil.rrule import rrulestr as _rrulestr
    from dateutil.parser import isoparse as _isoparse
except Exception:                                   # dateutil optional -> Serien aus
    _rrulestr = None
    def _isoparse(s): return datetime.fromisoformat(s)

_MEMBER_COOKIE = "board_member"
_OAUTH_STATE_COOKIE = "board_oauth_state"
_MEMBER_TTL = 30 * 86400                             # 30 Tage Session
_DISCORD_AUTH = "https://discord.com/oauth2/authorize"
_DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
_DISCORD_ME = "https://discord.com/api/users/@me"
_DACH = ("de", "at", "ch", "li")

# Rate-Limit der Karten-JSON-Endpunkte (pro IP; _ip/_rate wie beim Board, HMAC-IP-basiert).
RATE_MAP_PER_MIN = 60


def _map_rate_ok(req) -> bool:
    return _rate("mapdata:" + _hmac("map", _ip(req)), RATE_MAP_PER_MIN, 60)


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
    return uid


def _member_name(app, uid):
    """Löst den aktuellen Discord-Anzeigenamen live über die User-ID auf (nie gespeichert).
    None, wenn die Person nicht (mehr) Mitglied ist."""
    if not MAP_GUILD_ID:
        return None
    bot = app["bot"]
    g = bot.get_guild(MAP_GUILD_ID)
    if not g:
        return None
    m = g.get_member(int(uid))
    return m.display_name if m else None


def _contact_url(uid) -> str:
    """Discord-Profil-Link (öffnet Profil, von dort PN). Nur numerische IDs zulassen."""
    u = str(uid or "").strip()
    return f"https://discord.com/users/{u}" if u.isdigit() else ""


def _map_lang_redirect(path: str, lang: str) -> web.Response:
    # Redirect-Ziel aus internen Literalen + Whitelist-Sprache (CodeQL url-redirection safe)
    return web.HTTPFound(f"{path}?lang={lang}")


def _map_asset_v() -> str:
    """Cache-Busting für map.js/leaflet.js: Bot-Version + letzte Änderungszeit der Dateien.
    So holt der Browser nach einem Update sofort die neue Datei (statt bis zu 24 h Cache)."""
    try:
        m = max(int((STATIC_DIR / f).stat().st_mtime) for f in ("map.js", "leaflet.js")
                if (STATIC_DIR / f).is_file())
    except ValueError:
        m = 0
    return f"{VERSION}.{m}"


async def h_map(req):
    lang = pick_lang(req)
    if not MAP_ENABLED:
        return _render(req, "map", title=translate(lang, "map_h"),
                       flash=translate(lang, "map_disabled"),
                       v=_map_asset_v(), member=False, max_zoom=MAP_MAX_ZOOM)
    ics_url = _ics_calendar_url(req)
    webcal_url = "webcal://" + ics_url.split("://", 1)[-1]
    tag_groups = [{"label": (g["label"].get(lang) or g["label"].get("de")),
                   "tags": [(c, (l.get(lang) or l.get("de") or c)) for c, l in g["tags"]]}
                  for g in map_tags.TAG_GROUPS]
    return _render(req, "map", title=translate(lang, "map_h"),
                   v=_map_asset_v(), member=bool(_is_member(req)), max_zoom=MAP_MAX_ZOOM,
                   ics_url=ics_url, webcal_url=webcal_url, tag_groups=tag_groups,
                   member_csrf=(_member_csrf(_is_member(req)) if _is_member(req) else ""))


async def h_map_login(req):
    lang = pick_lang(req)
    if not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return _render(req, "map", title=translate(lang, "map_h"),
                       flash=translate(lang, "map_oauth_unconfigured"),
                       v=_map_asset_v(), member=False, max_zoom=MAP_MAX_ZOOM)
    state = secrets.token_urlsafe(24)
    params = urlencode({
        "client_id": BOARD_OAUTH_CLIENT_ID, "response_type": "code",
        "scope": "identify", "redirect_uri": BOARD_OAUTH_REDIRECT_URI,
        "state": state, "prompt": "none",
    })
    resp = web.HTTPFound(f"{_DISCORD_AUTH}?{params}")
    # State signiert im Cookie ablegen (CSRF-Schutz), kurzlebig.
    resp.set_cookie(_OAUTH_STATE_COOKIE, f"{state}.{_hmac('oauthstate', state)}",
                    max_age=600, httponly=True, samesite="Lax")
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
    resp = _map_lang_redirect("/map", lang)
    resp.del_cookie(_OAUTH_STATE_COOKIE)
    resp.set_cookie(_MEMBER_COOKIE, f"{uid}.{exp}.{_member_sign(uid, exp)}",
                    max_age=_MEMBER_TTL, httponly=True, samesite="Lax")
    raise resp


async def h_map_logout(req):
    resp = _map_lang_redirect("/map", pick_lang(req))
    resp.del_cookie(_MEMBER_COOKIE)
    raise resp


async def _map_tags_for(app, uids):
    """tag_codes je user_id (dict uid->list)."""
    if not uids:
        return {}
    rows = await execute_db(app["bot"],
        "SELECT user_id, tag_code FROM map_entry_tags", fetch=True) or []
    out: dict = {}
    for r in rows:
        if r["user_id"] in uids:
            out.setdefault(r["user_id"], []).append(r["tag_code"])
    return out


async def h_map_regions(req):
    """ÖFFENTLICH: nur aggregierte Zahlen (keine Identitäten)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app["bot"]
    # ALLE Opt-in-Einträge zählen (auch U18 anonym) – die Zahlen sind aggregiert und
    # enthalten keine Identitäten. Einzel-Pins/-Liste bleiben getrennt auf show_entry=1.
    rows = await execute_db(bot,
        "SELECT country, region_code, region_name, plz_prefix FROM map_entries",
        fetch=True) or []
    by_state, by_plz, by_country = {}, {}, {}
    for r in rows:
        c = r["country"]
        by_country[c] = by_country.get(c, 0) + 1
        if c in _DACH and r["region_code"]:
            # Codes vereinheitlichen (ISO wie in den Umrissen; auch für ältere Einträge)
            rc, rn = geo.canon_region(c, r["region_code"], r["region_name"] or "")
            if c == "li":
                rn = "Liechtenstein"
            k = f'{c}:{rc}'
            e = by_state.setdefault(k, {"country": c, "region_code": rc,
                                        "region_name": rn, "count": 0})
            e["count"] += 1
        if c in _DACH and r["plz_prefix"]:
            k = f'{c}:{r["plz_prefix"]}'
            e = by_plz.setdefault(k, {"country": c, "plz_prefix": r["plz_prefix"], "count": 0})
            e["count"] += 1
    # PLZ-Ebene als Blasen: Zentroid je PLZ-Gebiet aus dem GeoNames-Datensatz anhängen
    # (keine PLZ-Polygon-Datei nötig). Einträge ohne Zentroid werden vorne weggelassen.
    cents = geo.plz_prefix_centroids()
    plz_out = []
    for e in by_plz.values():
        ll = cents.get((e["country"], e["plz_prefix"]))
        if ll:
            e["lat"], e["lon"] = ll
            plz_out.append(e)
    return web.json_response({
        "bundesland": list(by_state.values()),
        "plz": plz_out,
        "countries": [{"country": k, "count": v} for k, v in sorted(by_country.items())],
    })


async def h_map_pins(req):
    """NUR eingeloggte Mitglieder: gefuzzte DACH-Pins (kleine Menge)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    if not _is_member(req):
        raise web.HTTPForbidden(text="login required")
    bot = req.app["bot"]
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, first_name, show_name, contact_ok, coarse, "
        "lat_fuzzed, lon_fuzzed FROM map_entries WHERE show_entry=1 "
        "AND lat_fuzzed IS NOT NULL LIMIT 2000", fetch=True) or []
    tags = await _map_tags_for(req.app, {r["user_id"] for r in rows})
    lang = pick_lang(req)
    anon = translate(lang, "map_anon")
    out = []
    for r in rows:
        name = _member_name(req.app, r["user_id"])
        if not name:                                 # kein Mitglied mehr -> auslassen
            continue
        if r["show_name"]:
            disp = f'{r["first_name"]} ({name})' if r["first_name"] else name
        else:
            disp = anon                              # Name ausgeblendet (nur anonym)
        out.append({
            "ref": _hmac("mapref", r["user_id"])[:12],   # opak, korreliert Pin ↔ Listenzeile
            "lat": r["lat_fuzzed"], "lon": r["lon_fuzzed"], "name": disp,
            "country": r["country"],
            "region": geo.canon_region(r["country"], r["region_code"] or "", r["region_name"] or "")[1],
            "contact": bool(r["contact_ok"]),
            # Profil-Link NUR bei aktivem Opt-in "Kontakt über Discord" (nur für eingeloggte Mitglieder).
            "contact_url": _contact_url(r["user_id"]) if r["contact_ok"] else "",
            "coarse": bool(r["coarse"]),             # Pin = Mitte des groben PLZ-Gebiets
            "tags": map_tags.labels(tags.get(r["user_id"], []), lang),
            "tag_codes": [c for c in tags.get(r["user_id"], []) if map_tags.is_valid(c)],
        })
    return web.json_response({"pins": out})


async def h_map_list(req):
    """Mitglieder: Einzel-Einträge (DACH + International). Öffentlich: nur Länder-Zahlen."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app["bot"]
    member = bool(_is_member(req))
    if not member:
        # Öffentlich: nur Länder-Zahlen (alle Opt-in-Einträge inkl. U18 anonym).
        rows = await execute_db(bot,
            "SELECT country, COUNT(*) AS n FROM map_entries GROUP BY country",
            fetch=True) or []
        return web.json_response({"member": False,
            "counts": [{"country": r["country"],
                        "country_name": country_name(pick_lang(req), r["country"]),
                        "count": r["n"]} for r in rows]})
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, first_name, show_name, contact_ok "
        "FROM map_entries WHERE show_entry=1 LIMIT 5000", fetch=True) or []
    tags = await _map_tags_for(req.app, {r["user_id"] for r in rows})
    lang = pick_lang(req)
    anon = translate(lang, "map_anon")
    items = []
    for r in rows:
        name = _member_name(req.app, r["user_id"])
        if not name:
            continue
        if r["show_name"]:
            disp = f'{r["first_name"]} ({name})' if r["first_name"] else name
        else:
            disp = anon
        items.append({
            "ref": _hmac("mapref", r["user_id"])[:12],   # opak, korreliert Listenzeile ↔ Pin
            "name": disp, "country": r["country"],
            "country_name": country_name(lang, r["country"]),
            "region": geo.canon_region(r["country"], r["region_code"] or "", r["region_name"] or "")[1],
            "dach": r["country"] in _DACH,
            "contact": bool(r["contact_ok"]),
            # Profil-Link NUR bei aktivem Opt-in "Kontakt über Discord" (nur für eingeloggte Mitglieder).
            "contact_url": _contact_url(r["user_id"]) if r["contact_ok"] else "",
            "tags": map_tags.labels(tags.get(r["user_id"], []), lang),
            "tag_codes": [c for c in tags.get(r["user_id"], []) if map_tags.is_valid(c)],
        })
    items.sort(key=lambda x: (x["country_name"], x["name"].lower()))
    return web.json_response({"member": True, "items": items})


def _event_local(s):
    """Gespeicherte (naive) Berliner Ortszeit -> zeitzonenbewusstes datetime."""
    d = _isoparse(s)
    return d.replace(tzinfo=BERLIN) if d.tzinfo is None else d


def _event_exdates(ev, start):
    """Parst die ausgefallenen Serien-Termine (exdates, ISO/kommagetrennt) zu datetimes."""
    out = []
    raw = ev["exdates"] if "exdates" in ev.keys() else None
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(_event_local(part))
        except Exception:
            continue
    return out


def _event_next(ev) -> datetime | None:
    """Nächstes anstehendes Vorkommen (Serie via RRULE, abzüglich EXDATE), sonst start_at."""
    now = datetime.now(timezone.utc)
    try:
        start = _event_local(ev["start_at"])
    except Exception:
        return None
    rr = ev["rrule"]
    if rr and _rrulestr:
        try:
            rs = _rrulestr(rr, dtstart=start, forceset=True)
            for ex in _event_exdates(ev, start):
                rs.exdate(ex)
            # Laufendes Vorkommen bleibt sichtbar: um die Termindauer zurückversetzt suchen.
            end = _event_end(ev, start)
            dur = (end - start) if end else timedelta(0)
            return rs.after(now - dur, inc=True)
        except Exception:
            pass
    end = _event_end(ev, start)
    return start if (end or start) >= now else None      # laufende Events bleiben sichtbar


def _event_end(ev, start):
    """Ende des Termins (aware) oder None. Ohne Endzeit zählt der ganze letzte Tag."""
    try:
        e = _event_local(ev["end_at"]) if ev["end_at"] else None
    except Exception:
        e = None
    if e is None:
        # Ganztägig ohne Enddatum: gilt bis Tagesende des Starttags.
        return start.replace(hour=23, minute=59, second=59) if ev["all_day"] else None
    if ev["all_day"] or not (e.hour or e.minute):
        e = e.replace(hour=23, minute=59, second=59)
    return e if e >= start else None


async def _approved_events(app):
    rows = await execute_db(app["bot"],
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


def _member_csrf(uid: str) -> str:
    """CSRF-Token für Mitglieder-Aktionen (zusätzlich zu SameSite=Lax am Login-Cookie)."""
    return _hmac("mcsrf", str(uid))[:32]


async def _rsvp_info(app, keys, me):
    """{(event_id, occ): {"count", "going", "names"}} für die angefragten Vorkommen."""
    out = {k: {"count": 0, "going": False, "names": []} for k in keys}
    if not keys:
        return out
    oldest = min(k[1] for k in keys)
    rows = await execute_db(app["bot"],
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
    me = _is_member(req)
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
    uid = _is_member(req)
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
    rows = await execute_db(req.app["bot"],
        "SELECT * FROM map_events WHERE id=? AND status='approved'", (eid,), fetch=True) or []
    nxt = _event_next(rows[0]) if rows else None
    if not nxt:
        raise web.HTTPNotFound()
    occ = _occ_date(nxt)
    bot = req.app["bot"]
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


def _ics_build(req, rows, feed: bool = True) -> bytes:
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
        lines += ["NAME:AAM – Termine", "X-WR-CALNAME:AAM – Termine",
                  f"X-WR-TIMEZONE:{_ICS_TZID}",
                  f"REFRESH-INTERVAL;VALUE=DURATION:{_ICS_REFRESH}",
                  f"X-PUBLISHED-TTL:{_ICS_REFRESH}",
                  f"SOURCE;VALUE=URI:{_ics_calendar_url(req)}"]
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
    rows = await execute_db(req.app["bot"],
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
    rows = await execute_db(req.app["bot"],
        "SELECT * FROM map_events WHERE id=? AND status='approved'", (eid,), fetch=True) or []
    if not rows:
        raise web.HTTPNotFound()
    return web.Response(body=_ics_build(req, rows, feed=False),
                        headers={"Content-Type": "text/calendar; charset=utf-8",
                                 "Content-Disposition": f"attachment; filename=aam-event-{eid}.ics",
                                 "Cache-Control": "public, max-age=3600"})


def build_app(bot) -> web.Application:
    app = web.Application(client_max_size=1024*1024)
    app["bot"] = bot
    app.add_routes([
        web.get("/", h_board), web.get("/favicon.ico", h_favicon),
        web.get("/stats", h_stats), web.get("/static/{name}", h_static),
        web.get("/impressum", h_impressum), web.get("/datenschutz", h_datenschutz),
        web.post("/impressum/contact", h_impressum_contact),
        web.get("/status.json", h_status_json),
        web.get("/status/check/{key}", h_status_detail),
        web.post("/status/incident/{id}/note", h_incident_note),
        web.get("/submit", h_submit_form), web.post("/submit", h_submit),
        web.post("/upvote/{id}", h_upvote), web.get("/submission/{id}", h_detail),
        web.get("/admin/login", h_login_form), web.post("/admin/login", h_login),
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
        web.get("/map/events.ics", h_map_events_ics),
        web.get(r"/map/events/{eid:\d+}.ics", h_map_event_ics),
        web.post(r"/map/events/{eid:\d+}/rsvp", h_map_event_rsvp),
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
            self.runner = web.AppRunner(build_app(self.bot),
                                        access_log_class=_DebugAccessLogger)
            await self.runner.setup()
            await web.TCPSite(self.runner, BOARD_BIND, BOARD_PORT).start()
            logger.info("🌐 Feedback-Board läuft auf http://%s:%d (öffentlich: %s)",
                        BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL or "—")
            self.incident_monitor.start()   # Vorfall-Historie der Status-Kacheln
        except Exception as e:
            logger.error("❌ Board-Start fehlgeschlagen: %s", e, exc_info=True)

    @tasks.loop(minutes=1)
    async def incident_monitor(self):
        """Wertet minütlich die Health-Checks aus und schreibt die Vorfall-Historie fort."""
        await _record_incidents(self.bot)

    @incident_monitor.before_loop
    async def _before_incident_monitor(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        if self.incident_monitor.is_running():
            self.incident_monitor.cancel()
        if self.runner:
            self.bot.loop.create_task(self.runner.cleanup())


def setup(bot: discord.Bot):
    bot.add_cog(BoardCog(bot))
