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
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import discord
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger
from discord.ext import commands, tasks
from jinja2 import Environment, DictLoader, select_autoescape

from config import (BOARD_ENABLED, BOARD_BIND, BOARD_PORT, BOARD_PUBLIC_URL,
                    BOARD_ADMIN_TOKEN, BOARD_OWNER_ID, BOARD_HASH_SALT,
                    SHOPS_DATA_FILE, SPECIES_CATALOG_FILE, DATA_DIRECTORY, AI_CHAT_PUBLIC,
                    VERSION)
from datetime import datetime, timezone
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
# Strikte Allowlist auslieferbarer Dateien -> {Dateiname: Content-Type}. Neue Assets
# hier eintragen (der /static-Handler baut den Pfad nur aus diesen Literalen).
_STATIC_FILES = {
    "chart.umd.js": "application/javascript",
    "chartjs-chart-treemap.min.js": "application/javascript",
    "stats.js": "application/javascript",
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
<title>{{ title }} · AAM-Bot Board</title><link rel="icon" type="image/svg+xml" href="/favicon.ico"><style>
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
 .chartwrap{position:relative;height:320px}
 .raritygrid{columns:2;column-gap:18px;margin-top:8px;font-size:13px;color:#8b949e}
 .raritygrid div{break-inside:avoid;padding:1px 0;font-style:italic}
 @media(max-width:640px){.raritygrid{columns:1}}
</style></head><body>
<header><h1>🐜 {{ t('brand') }}</h1>
 <a href="/{{ qs() }}">{{ t('nav_board') }}</a><a href="/stats{{ qs() }}">{{ t('nav_stats') }}</a><a href="/submit{{ qs() }}">{{ t('nav_submit') }}</a><a href="https://paypal.me/JonasBeier1998" target="_blank" rel="noopener">{{ t('nav_support') }}</a><span class=grow></span>
 <span class="langsw">{% for code in langs %}<a class="{{ 'on' if code==lang }}" href="{{ switch_urls[code] }}" title="{{ flag_title[code] }}">{{ flags[code][0]|safe }} {{ flags[code][1] }}</a>{% endfor %}</span>
 {% if admin %}<span class=muted>{{ t('nav_owner') }}</span> <a href="/admin{{ qs() }}">{{ t('nav_admin') }}</a> <a href="/admin/logout">{{ t('nav_logout') }}</a>
 {% else %}<a href="/admin/login{{ qs() }}">{{ t('nav_login') }}</a>{% endif %}</header>
<div class=wrap>{% if flash %}<div class=flash>{{ flash }}</div>{% endif %}{% block body %}{% endblock %}</div>
<footer style="max-width:1100px;margin:28px auto 12px;padding:14px 20px;border-top:1px solid #30363d;color:#8b949e;font-size:13px;text-align:center;line-height:1.6">
  💖 <strong>{{ t('footer_run') }}</strong>
  <a href="https://paypal.me/JonasBeier1998" target="_blank" rel="noopener" style="color:#58a6ff">paypal.me/JonasBeier1998</a>
  · <a href="https://github.com/JonasVerzockt/Discord-Bot" target="_blank" rel="noopener" style="color:#58a6ff">{{ t('footer_source') }}</a>
</footer>
</body></html>"""

BOARD = """{% extends "base" %}{% block body %}
<details class="status-panel">
 <summary class="status-head">{{ t('status_head') }}
  <span id="hc-badge" class="status-badge s-{{ overall[0] }}">{{ overall[1] }}</span>
  <span id="hc-ver" class="status-ver" title="{{ t('ver_title') }}">v{{ version }}</span>
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
 <h3>{{ t(key) }}</h3>
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
  <div class=chartbox><h4>{{ t('sp_rarities_title') }}</h4>
   <p class=muted style="margin-top:0">{{ t('sp_rarities_count', n=sp.rarities_count) }}</p>
   {% if sp.rarities_sample %}<details><summary style="cursor:pointer;color:#58a6ff">{{ t('sp_rarities_show', n=sp.rarities_sample|length) }}</summary>
    <div class=raritygrid>{% for r in sp.rarities_sample %}<div>{{ r }}</div>{% endfor %}</div></details>{% endif %}
  </div>
 {% elif aid=='shops' %}
  <div class=chartbox><h4>{{ t('sh_offers_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chShopOffers"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sh_breadth_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chShopBreadth"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sh_exclusive_title') }}</h4><div class="chartwrap" style="height:360px"><canvas id="chShopExclusive"></canvas></div></div>
  <div class=chartbox><h4>{{ t('sh_scatter_title') }}</h4><div class="chartwrap" style="height:380px"><canvas id="chShopScatter"></canvas></div></div>
 {% else %}
  <p class=muted>{{ t('st_wip') }}</p>
 {% endif %}
</section>
{% endfor %}
<script src="/static/chart.umd.js"></script>
<script src="/static/chartjs-chart-treemap.min.js"></script>
<script>var STATS = {{ data|tojson }}; var STATS_L = {{ l10n|tojson }};</script>
<script src="/static/stats.js"></script>
{% endif %}
{% endblock %}"""

ENV = Environment(loader=DictLoader({"base": BASE, "board": BOARD, "submit": SUBMIT,
                                     "detail": DETAIL, "login": LOGIN, "admin": ADMIN,
                                     "edit": EDIT, "statusdetail": STATUSDETAIL,
                                     "stats": STATS}),
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
                type_label=(lambda ty: type_label(lang, ty)))
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
                   generated=now_berlin("%H:%M:%S"), flash=flash)
    resp.headers["Cache-Control"] = "no-store"   # kein veraltetes HTML aus Proxy/Browser-Cache
    return resp


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


def _stats_l10n(lang: str, data: dict) -> dict:
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
    return out


async def h_stats(req):
    """Öffentliche Statistik-Seite. Aggregiert live aus shops_data.json (15-min-Cache).
    Währungskurse werden zuvor sichergestellt (für die späteren EUR-Preisblöcke)."""
    lang = pick_lang(req)
    data = l10n = None
    try:
        await ensure_rates()                                   # EZB/Frankfurter + Fallback
        data = await asyncio.to_thread(shop_stats.compute)     # Datei-I/O + CPU im Thread
    except FileNotFoundError:
        logger.warning("📊 Stats: shops_data.json nicht gefunden (%s)", SHOPS_DATA_FILE)
    except Exception as e:
        logger.warning("📊 Stats-Aggregation fehlgeschlagen: %s", e, exc_info=True)
    if data:
        l10n = _stats_l10n(lang, data)
    resp = _render(req, "stats", title=translate(lang, "nav_stats"), data=data, l10n=l10n)
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
         "generated": now_berlin("%H:%M:%S")},
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


# Kleines SVG-Ameisen-Favicon (gezeichnet, keine Emoji-Glyphe -> rendert in allen
# Browsern; Emoji-in-SVG bleibt z.B. in Chrome leer). Verhindert die favicon-404.
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
    return web.Response(text=_FAVICON, content_type="image/svg+xml")


def build_app(bot) -> web.Application:
    app = web.Application(client_max_size=1024*1024)
    app["bot"] = bot
    app.add_routes([
        web.get("/", h_board), web.get("/favicon.ico", h_favicon),
        web.get("/stats", h_stats), web.get("/static/{name}", h_static),
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
