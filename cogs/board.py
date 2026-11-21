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
                    BOARD_OWNER_ID, BOARD_ADMIN_IDS, BOARD_HASH_SALT,
                    SHOPS_DATA_FILE, SPECIES_CATALOG_FILE, DATA_DIRECTORY, AI_CHAT_PUBLIC,
                    VERSION,
                    MAP_ENABLED, MAP_GUILD_ID, MAP_JITTER_METERS, MAP_MAX_ZOOM,
                    BOARD_OAUTH_CLIENT_ID, BOARD_OAUTH_CLIENT_SECRET, BOARD_OAUTH_REDIRECT_URI,
                    DISCORD_INVITE_URL, BOARD_AI_CHAT, BOARD_AI_CHAT_PER_MIN,
                    BOARD_AI_CHAT_PER_HOUR)
from utils import geo, map_tags
from utils import account as ACC
from utils import board_swarm as SWB
from datetime import datetime, timezone, timedelta
from utils.board_db import (board_init, board_query, board_one, board_exec, board_execmany)
from utils.db import execute_db
from utils.timez import BERLIN, now_berlin, berlin_from_utc_naive
from urllib.parse import urlencode
from utils.board_i18n import (LANGS, FLAGS, FLAG_TITLE, pick_lang, translate,
                              type_label, flash_text, country_name)
from utils.board_i18n import T as _BI18N_T
from utils.currency import ensure_rates
from utils import shop_stats

# Vendored statische Assets (Chart.js self-hosted, kein CDN) unter <repo>/static/.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Rechtsseiten-Inhalte unter <repo>/legal/. Echte Datei (z.B. impressum.html) liegt
# NICHT im Git (gitignored) und wird bevorzugt geladen; sonst die .example-Vorlage.
LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"
_LEGAL_PAGES = {"impressum", "datenschutz", "partner", "gewinnspiel"}


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


# ── Partner/Sponsoren (Karte, rechte Spalte) ──────────────────────────────────
# legal/partners.json: [{"name": "...", "url": "https://...", "text": "...", "logo": "datei.png",
#                        "art": "verein" | "shop", "code": "AAM10"}]
#   art "verein": Vereine/Projekte, gegenseitige Verlinkung -> ohne „Anzeige“.
#   art "shop" (Standard): Shops/Hersteller gegen Community-Vorteil -> als „Anzeige“ gekennzeichnet.
#   code: optionaler Community-Rabattcode (nur bei Shops angezeigt).
# Logos liegen SELBST GEHOSTET in legal/partners/ (keine fremden Server -> keine IP-Weitergabe).
# Nur PNG/JPG/WebP (kein SVG: könnte Skripte enthalten). Fehlt die Datei -> Platzhalter.
PARTNER_DIR = LEGAL_DIR / "partners"
_PARTNER_LOGO_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}\.(png|jpe?g|webp)$")
_PARTNER_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


_partner_cache: dict = {"key": None, "data": []}


def _load_partners() -> list[dict]:
    """Geprüfte Partnerliste (max. 6). Ungültige Einträge werden still übersprungen.
    Wird auf jeder Seite gebraucht -> nach Änderungszeit der Datei/des Logo-Ordners gecacht."""
    f = LEGAL_DIR / "partners.json"
    try:
        key = (f.stat().st_mtime_ns, PARTNER_DIR.stat().st_mtime_ns if PARTNER_DIR.is_dir() else 0)
    except OSError:
        return []
    if _partner_cache["key"] == key:
        return _partner_cache["data"]
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()[:60]
        url = str(e.get("url") or "").strip()
        if not name or not url.startswith("https://"):
            continue
        logo = str(e.get("logo") or "").strip()
        if not (_PARTNER_LOGO_RE.match(logo) and (PARTNER_DIR / logo).is_file()):
            logo = ""
        art = "verein" if str(e.get("art") or "").strip().lower() == "verein" else "shop"
        code = str(e.get("code") or "").strip()[:30]
        if art != "shop" or not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", code or ""):
            code = ""
        out.append({"name": name, "url": url[:300],
                    "text": str(e.get("text") or "").strip()[:140], "logo": logo,
                    "ad": art == "shop", "code": code})
    _partner_cache.update(key=key, data=out[:6])
    return out[:6]


async def h_partner_logo(req):
    """Liefert NUR Logos, die in partners.json eingetragen sind (Allowlist)."""
    want = req.match_info.get("name", "")
    for p in _load_partners():
        if p["logo"] and p["logo"] == want:
            path = PARTNER_DIR / p["logo"]          # Name stammt aus der geprüften Liste
            ext = p["logo"].rsplit(".", 1)[-1].lower()
            return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400",
                                                   "Content-Type": _PARTNER_CT[ext],
                                                   "X-Content-Type-Options": "nosniff"})
    raise web.HTTPNotFound()


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


# ── Templates (Dark-Mode-ONLY) ────────────────────────────────────────────────
BASE = """<!doctype html><html lang="{{ lang }}"><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{{ title }} · AAM-Bot Board</title><link rel="icon" id="favicon" type="image/png" href="/static/favicon.png?v={{ v }}"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v={{ v }}"><script>if(/Firefox\\//.test(navigator.userAgent)){var f=document.getElementById("favicon");f.type="image/gif";f.href="/static/favicon.gif?v={{ v }}";}</script><style>
 :root{color-scheme:only dark} html,body{background:#0d1117}
 body{color:#e6edf3;font:15px/1.5 system-ui,Segoe UI,Arial;margin:0}
 option{background:#0d1117;color:#e6edf3} ::placeholder{color:#6e7681;opacity:1}
 a{color:#58a6ff;text-decoration:none} a:hover{text-decoration:underline}
 header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;gap:16px;align-items:center}
 header h1{font-size:18px;margin:0} header>a{white-space:nowrap} .grow{flex:1}
 /* Handy: Kopfzeile umbrechen statt die Seite zu verbreitern */
 @media(max-width:1360px){header{flex-wrap:wrap;gap:8px 14px;padding:10px 14px} header h1{flex-basis:100%} header>.grow{display:none}}
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
 .raritygrid .rshop{font-style:normal;color:#6e7681}
 @media(max-width:640px){.raritygrid{columns:1}}
 .rangesw{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
 .rangesw a{border:1px solid #30363d;border-radius:20px;padding:3px 10px;font-size:13px}
 .rangesw a.on{border-color:#58a6ff;color:#e6edf3;background:#1f6feb22}
 .pstrip{max-width:1100px;margin:28px auto 0;padding:10px 20px;display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;border-top:1px solid #30363d;font-size:13px}
 .pstrip .plbl{color:#8b949e} .pstrip .ad{font-size:10px;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:0 4px;margin-left:4px}
 .pstrip a{display:inline-flex;align-items:center;gap:6px;color:#c9d1d9}
 .pstrip img{width:28px;height:28px;object-fit:contain;border-radius:4px;background:#fff1}
 .pstrip+footer{margin-top:0 !important}
 .legal{max-width:820px} .legal h3{margin:18px 0 6px;font-size:15px;color:#c9d1d9} .legal p{margin:0 0 8px} .legal code{background:#161b22;border:1px solid #30363d;border-radius:4px;padding:1px 5px}
</style></head><body>
<header><h1>🐜 {{ t('brand') }}</h1>
 <a href="/{{ qs() }}">{{ t('nav_board') }}</a><a href="/stats{{ qs() }}">{{ t('nav_stats') }}</a><a href="/shops{{ qs() }}">{{ t('nav_shops') }}</a><a href="/art{{ qs() }}">{{ t('nav_species') }}</a><a href="/map{{ qs() }}">{{ t('nav_map') }}</a><a href="/schwarmflug{{ qs() }}">{{ t('nav_swarm') }}</a><a href="/submit{{ qs() }}">{{ t('nav_submit') }}</a><a href="https://paypal.me/JonasBeier1998" target="_blank" rel="noopener">{{ t('nav_support') }}</a><span class=grow></span>
 <span class="langsw">{% for code in langs %}<a class="{{ 'on' if code==lang }}" href="{{ switch_urls[code] }}" title="{{ flag_title[code] }}">{{ flags[code][0]|safe }} {{ flags[code][1] }}</a>{% endfor %}</span>
 <a href="/hilfe{{ qs() }}">{{ t('nav_help') }}</a>
 {% if nav_member %}<a href="/konto{{ qs() }}">{{ t('nav_konto') }}</a>{% endif %}
 {% if admin %}<span class=muted>{{ t('nav_owner') }}</span> <a href="/admin{{ qs() }}">{{ t('nav_admin') }}</a> <a href="/admin/logout">{{ t('nav_logout') }}</a>
 {% else %}<a href="/admin/login{{ qs() }}">{{ t('nav_login') }}</a>{% endif %}</header>
<div class=wrap>{% if swarm_banner %}<div class=flash style="background:#3fb95018;border-color:#3fb95066">🪽 <b>{{ t('sw_season_open', y=swarm_banner.year) }}</b> {{ t('sw_season_first', label=swarm_banner.label, d=swarm_banner.flight_date|dmy, where=swarm_banner.where) }} <a href="/schwarmflug{{ qs() }}">{{ t('sw_art_more') }} →</a></div>{% endif %}{% if flash %}<div class=flash>{{ flash }}</div>{% endif %}{% block body %}{% endblock %}</div>
{% if partner_strip %}<div class=pstrip aria-label="{{ t('map_partner_h') }}"><span class=plbl>🤝 <a href="/partner?lang={{ lang }}" style="color:inherit">{{ t('map_partner_h') }}</a></span>
{% for p in partner_strip %}<a href="{{ p.url }}" target=_blank rel="{{ 'sponsored ' if p.ad }}noopener noreferrer" title="{{ p.text }}{% if p.code %} · {{ t('map_partner_code') }}: {{ p.code }}{% endif %}">{% if p.logo %}<img src="/partner-logo/{{ p.logo }}?v={{ v }}" alt="" loading=lazy>{% endif %}<span>{{ p.name }}</span>{% if p.ad %}<span class=ad>{{ t('map_partner_ad') }}</span>{% endif %}</a>{% endfor %}</div>{% endif %}
<footer style="max-width:1100px;margin:28px auto 12px;padding:14px 20px;border-top:1px solid #30363d;color:#8b949e;font-size:13px;text-align:center;line-height:1.6">
  💖 <strong>{{ t('footer_run') }}</strong>
  <a href="https://paypal.me/JonasBeier1998" target="_blank" rel="noopener" style="color:#58a6ff">paypal.me/JonasBeier1998</a>
  · <a href="https://github.com/JonasVerzockt/Discord-Bot" target="_blank" rel="noopener" style="color:#58a6ff">{{ t('footer_source') }}</a>
  · <a href="/impressum?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_impressum') }}</a>
  · <a href="/datenschutz?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_privacy') }}</a>
  · <a href="/partner?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_partner') }}</a>
  · <a href="/gewinnspiel?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_giveaway') }}</a>
  · <a href="/rueckblick?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_yearreview') }}</a>
  · <a href="/inat?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_inat') }}</a>
  · <a href="/schwarmflug?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_swarm') }}</a>
  · <a href="/konto?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_konto') }}</a>
  · <a href="/hilfe?lang={{ lang }}" style="color:#58a6ff">{{ t('nav_help') }}</a>
</footer>
{% if chat_ok %}{% include "chatwidget" %}{% endif %}
<script src="/static/intro.js?v={{ intro_v }}" defer></script>
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
<h2>{{ t('login_h') }}</h2>
<p class=muted style="max-width:560px">{{ t('login_discord_note') }}</p>
{% if login_url %}<p><a class=btn href="{{ login_url }}">{{ t('dir_login_btn') }}</a></p>{% endif %}
{% endblock %}"""

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
{% set sections = [('overview','st_sec_overview'),('species','st_sec_species'),('shops','st_sec_shops'),('prices','st_sec_prices'),('availability','st_sec_availability'),('quality','st_sec_quality'),('trends','st_sec_trends'),('usage','st_sec_usage')] %}
{% macro mask(n) %}{% if n is none %}–{% elif 0 < n < 3 %}&lt; 3{% else %}{{ n }}{% endif %}{% endmacro %}
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
   <div class=kpi><div class=v>{{ o.inactive_products|default(0) }}</div><div class=l>{{ t('kpi_inactive') }}</div></div>
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
   {% set rf = sp.rarities_full or [] %}
   {% if rf %}<details id=rarebox><summary style="cursor:pointer;color:#58a6ff">{{ t('sp_rarities_show', n=rf|length) }}</summary>
    <input id=rarefilter type=search placeholder="{{ t('sp_rarities_filter') }}" autocomplete=off style="margin-top:8px;max-width:360px">
    <span id=rarecount class=muted style="font-size:12px;margin-left:8px"></span>
    <div class=raritygrid id=rarelist>{% for art, shop in rf %}<div data-q="{{ (art ~ ' ' ~ shop)|lower }}">{{ art }} <span class=rshop>· {{ shop }}</span></div>{% endfor %}</div>
    <script>
    (function () {
      var inp = document.getElementById("rarefilter"), rows = document.querySelectorAll("#rarelist > div"),
          cnt = document.getElementById("rarecount");
      if (!inp) return;
      function apply() {
        var q = inp.value.trim().toLowerCase(), n = 0;
        rows.forEach(function (r) { var ok = !q || r.getAttribute("data-q").indexOf(q) >= 0; r.style.display = ok ? "" : "none"; if (ok) n++; });
        cnt.textContent = q ? (n + " / " + rows.length) : "";
      }
      inp.addEventListener("input", apply);
    })();
    </script></details>{% endif %}
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
  {% set nw = data.availability.nowhere or [] %}
  <div class=chartbox><h4>{{ t('av_hardest_title') }}</h4>
   {% if nw %}<details id=nowherebox style="margin:0 0 10px"><summary style="cursor:pointer;color:#58a6ff">{{ t('av_nowhere_line', n=nw|length) }}</summary>
    <input id=nowherefilter type=search placeholder="{{ t('av_nowhere_filter') }}" autocomplete=off style="margin-top:8px;max-width:360px">
    <span id=nowherecount class=muted style="font-size:12px;margin-left:8px"></span>
    <div class=raritygrid id=nowherelist>{% for art, nsh in nw %}<div data-q="{{ art|lower }}">{{ art }} <span class=rshop>· {{ nsh }} {{ t('lbl_shops') }}</span></div>{% endfor %}</div>
    <script>
    (function () {
      var inp = document.getElementById("nowherefilter"), rows = document.querySelectorAll("#nowherelist > div"),
          cnt = document.getElementById("nowherecount");
      if (!inp) return;
      inp.addEventListener("input", function () {
        var q = inp.value.trim().toLowerCase(), n = 0;
        rows.forEach(function (r) { var ok = !q || r.getAttribute("data-q").indexOf(q) >= 0; r.style.display = ok ? "" : "none"; if (ok) n++; });
        cnt.textContent = q ? (n + " / " + rows.length) : "";
      });
    })();
    </script></details>{% endif %}
   {% if data.availability.hardest %}<div class="chartwrap" style="height:360px"><canvas id="chAvHardest"></canvas></div>{% else %}<p class=muted>{{ t('st_none') }}</p>{% endif %}
  </div>
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
 {% elif aid=='usage' %}
  {% if not usage %}<p class=muted>{{ t('us_none') }}</p>{% else %}
  {% set k = usage.kpi %}
  <div class=kpigrid>
   <div class=kpi><div class=v>{{ mask(k.calls_month) }}</div><div class=l>{{ t('us_kpi_calls_month') }}</div></div>
   <div class=kpi><div class=v>{{ mask(k.users_month) }}</div><div class=l>{{ t('us_kpi_users_month') }}</div></div>
   <div class=kpi><div class=v>{{ mask(k.calls_total) }}</div><div class=l>{{ t('us_kpi_calls_total', since=usage.since) }}</div></div>
   {% if k.notifications is not none %}<div class=kpi><div class=v>{{ mask(k.notifications) }}</div><div class=l>{{ t('us_kpi_notif') }}</div></div>{% endif %}
   {% if k.price_tracking is not none %}<div class=kpi><div class=v>{{ mask(k.price_tracking) }}</div><div class=l>{{ t('us_kpi_track') }}</div></div>{% endif %}
   {% if k.species_watch is not none %}<div class=kpi><div class=v>{{ mask(k.species_watch) }}</div><div class=l>{{ t('us_kpi_watch') }}</div></div>{% endif %}
  </div>
  <div class=chartbox><h4>{{ t('us_calls_title') }}</h4><div class=chartwrap><canvas id="chUsCalls"></canvas></div></div>
  <div class=chartbox><h4>{{ t('us_content_title') }}</h4><div class=chartwrap><canvas id="chUsContent"></canvas></div></div>
  {% if usage.top %}<div class=chartbox><h4>{{ t('us_top_title') }}</h4><div class="chartwrap" style="height:{{ 60 + usage.top|length * 28 }}px"><canvas id="chUsTop"></canvas></div></div>{% endif %}
  <p class=muted>{{ t('us_note') }}</p>
  <script>
  window.addEventListener("load", function () {
   if (!window.Chart) return;
   var U = {{ usage|tojson }}, g = {color: "#21262d"}, tk = {color: "#8b949e"};
   var base = {maintainAspectRatio: false, plugins: {legend: {labels: tk}}};
   new Chart(document.getElementById("chUsCalls"), {type: "bar", data: {labels: U.labels, datasets: [
     {label: {{ t('us_calls')|tojson }}, data: U.calls, backgroundColor: "#1f6feb99", yAxisID: "y"},
     {type: "line", label: {{ t('us_users')|tojson }}, data: U.users, borderColor: "#3fb950", spanGaps: false, yAxisID: "y2", tension: .2}]},
     options: Object.assign({}, base, {scales: {x: {ticks: tk, grid: g}, y: {beginAtZero: true, ticks: tk, grid: g},
       y2: {position: "right", beginAtZero: true, ticks: tk, grid: {display: false}}}})});
   new Chart(document.getElementById("chUsContent"), {type: "bar", data: {labels: U.labels, datasets: [
     {label: {{ t('us_reviews')|tojson }}, data: U.reviews, backgroundColor: "#3fb95099"},
     {label: {{ t('us_codes')|tojson }}, data: U.codes, backgroundColor: "#d2a8ff99"}]},
     options: Object.assign({}, base, {scales: {x: {ticks: tk, grid: g}, y: {beginAtZero: true, ticks: tk, grid: g}}})});
   var top = document.getElementById("chUsTop");
   if (top) new Chart(top, {type: "bar", data: {labels: U.top.map(function (x) { return "/" + x[0]; }),
     datasets: [{data: U.top.map(function (x) { return x[1]; }), backgroundColor: "#1f6feb99"}]},
     options: {indexAxis: "y", maintainAspectRatio: false, plugins: {legend: {display: false}},
       scales: {x: {beginAtZero: true, ticks: tk, grid: g}, y: {ticks: tk, grid: g}}}});
  });
  </script>
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
<div class="legal" id=kontakt style="margin-top:18px">
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
 /* Statistiken: Desktop unter der Karte (linke Spalte), mobil zwischen Karte und Liste */
 .mapleft{min-width:0}
 #statsbox{margin-top:14px;min-width:0}
 @media(max-width:820px){.mapleft{display:contents} #map{order:1} #statsbox{order:2;margin-top:0} .mapside{order:3}}
 #statsbox>summary{cursor:pointer;font-weight:600;font-size:15px;list-style-position:inside}
 #statsbody{margin-top:10px}
 #joinbox ul{margin:0;padding-left:18px;font-size:13px} #joinbox li{margin:4px 0}
 #joinbox code{background:#0f141a;border:1px solid #30363d;border-radius:4px;padding:0 4px;font-size:12px}
 .partner .ad,#partnerbox h4 .ad{font-size:10px;font-weight:400;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:0 4px;margin-left:6px;vertical-align:middle}
 .partner{display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #21262d}
 .partner img{width:44px;height:44px;object-fit:contain;border-radius:6px;background:#fff1}
 .partner .pt{font-size:12px;color:#8b949e}
 .pslot{border:1px dashed #30363d;border-radius:8px;padding:12px;text-align:center;font-size:13px;color:#8b949e}
 .stk{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:12px}
 .stk div{background:#0f141a;border:1px solid #21262d;border-radius:8px;padding:8px 10px}
 .stk b{display:block;font-size:20px;color:#e6edf3}
 .stk span{font-size:12px;color:#8b949e}
 .stgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
 .stgrid>section{min-width:0}
 .stgrid h5{margin:0 0 6px;font-size:13px;color:#c9d1d9}
 .stnote{font-size:11px;color:#8b949e;margin:-2px 0 6px}
 .sbar{display:grid;grid-template-columns:minmax(0,1fr) 2fr auto;gap:8px;align-items:center;font-size:12px;margin:3px 0}
 .sbar .lb{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .sbar .tr{height:10px;background:#21262d;border-radius:5px;overflow:hidden}
 .sbar .fi{height:100%;background:#58a6ff;border-radius:5px}
 .sbar .vl{color:#8b949e;white-space:nowrap;text-align:right}
 .scol{display:flex;align-items:flex-end;gap:3px;height:80px;border-bottom:1px solid #30363d}
 .scol div{flex:1;background:#58a6ff;border-radius:3px 3px 0 0;min-height:2px;position:relative}
 .scolx{display:flex;gap:3px;font-size:10px;color:#8b949e;margin-top:3px}
 .scolx span{flex:1;text-align:center;overflow:hidden}
 .stlist{font-size:12px;margin:0;padding-left:18px} .stlist li{margin:2px 0}
 .stpriv{font-size:11px;color:#8b949e;border-top:1px solid #21262d;margin-top:12px;padding-top:8px}
 html[data-mapcolors=contrast] .sbar .fi,html[data-mapcolors=contrast] .scol div{background:#ffffff}
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
 .mcity{background:none;border:0;white-space:nowrap}
 .mcity .cdot{position:absolute;left:-2px;top:-2px;width:4px;height:4px;border-radius:50%;background:#c9d1d9;box-shadow:0 0 0 1px #0b0f14}
 .mcity .clbl{position:absolute;left:5px;top:-7px;font-size:11px;line-height:14px;color:#c9d1d9;text-shadow:0 0 3px #0b0f14,0 0 2px #0b0f14,0 0 1px #0b0f14}
 .mcity.big .cdot{width:6px;height:6px;left:-3px;top:-3px}
 .mcity.big .clbl{font-size:12px;font-weight:600;top:-8px;color:#e6edf3}
 html[data-mapcolors=contrast] .mcity .clbl{color:#ffffff;font-weight:600;text-shadow:0 0 3px #000,0 0 3px #000,0 0 2px #000}
 html[data-mapcolors=contrast] .mcity .cdot{background:#ffffff;box-shadow:0 0 0 1.5px #000}
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
  <a href="#" id=citytoggle class=on aria-pressed="true" style="margin-left:8px">🏙️ {{ t('map_cities') }}</a>
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
  <div class=mapleft>
  <div id=map style="height:70vh;min-height:420px;background:#0f141a;border:1px solid #21262d;border-radius:10px"></div>
  <details id=statsbox class=chartbox>
    <summary>{{ t('map_st_title') }}</summary>
    <div id=statsbody aria-live=polite><p class=muted>{{ t('map_st_loading') }}</p></div>
  </details>
  </div>
  <div class=mapside>
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
    <div class=chartbox id=joinbox style="margin-top:14px">
      <h4>🙌 {{ t('map_join_h') }}</h4>
      <ul>
        <li>📍 {{ t('map_join_entry')|safe }}</li>
        <li>🏷️ {{ t('map_join_tags')|safe }}</li>
        <li>📅 {{ t('map_join_event')|safe }}</li>
        <li>🙋 {{ t('map_join_rsvp') }}</li>
      </ul>
      {% if invite_url %}<a class=btn href="{{ invite_url }}" target=_blank rel="noopener noreferrer" style="display:block;margin-top:10px;text-align:center">{{ t('map_join_invite') }}</a>{% endif %}
    </div>
    <div class=chartbox id=partnerbox style="margin-top:14px">
      <h4>🤝 {{ t('map_partner_h') }}</h4>
      {% for p in partners %}
      <a class=partner href="{{ p.url }}" target=_blank rel="{{ 'sponsored ' if p.ad }}noopener noreferrer">
        {% if p.logo %}<img src="/partner-logo/{{ p.logo }}?v={{ v }}" alt="" loading=lazy>{% endif %}
        <span><b>{{ p.name }}</b>{% if p.ad %}<span class=ad>{{ t('map_partner_ad') }}</span>{% endif %}{% if p.text %}<br><span class=pt>{{ p.text }}</span>{% endif %}{% if p.code %}<br><span class=pt>🏷️ {{ t('map_partner_code') }}: <code>{{ p.code }}</code></span>{% endif %}</span>
      </a>
      {% endfor %}
      {% if partners %}<p class=pt style="margin:8px 0 0;font-size:12px"><a href="/partner?lang={{ lang }}">{{ t('map_partner_become') }}</a></p>{% else %}
      <div class=pslot>{{ t('map_partner_empty') }}<br>
        <a href="/partner?lang={{ lang }}">{{ t('map_partner_contact') }}</a></div>
      {% endif %}
    </div>
  </div>
</div>
<p class=muted style="margin-top:14px;font-size:12px">{{ t('map_attribution')|safe }}</p>
<script>window.MAP_CFG={lang:"{{ lang }}",v:"{{ v }}",member:{{ 'true' if member else 'false' }},maxZoom:{{ max_zoom }},
 emptyText:"{{ t('map_list_empty') }}", tagNoMatch:"{{ t('map_tag_nomatch') }}", choroLabel:"{{ t('map_choro_label') }}",
 csrf:"{{ member_csrf }}",
 st:{{ (st_labels or {})|tojson }},
 evtext:{ics:"{{ t('map_ev_ics') }}",go:"{{ t('map_ev_go') }}",going:"{{ t('map_ev_going') }}",
         count:"{{ t('map_ev_count') }}"},
 pinlabels:{exact:"{{ t('map_pin_exact') }}",coarse:"{{ t('map_pin_coarse') }}",coarseNote:"{{ t('map_pin_coarse_note') }}",contact:"{{ t('map_pin_contact') }}",contactBtn:"{{ t('map_contact_btn') }}",also:"{{ t('map_pin_also') }}",alsoHere:"{{ t('map_also_here') }}"},
 evlabels:{fair:"{{ t('map_evtype_fair') }}",meetup:"{{ t('map_evtype_meetup') }}",shop:"{{ t('map_evtype_shop') }}",talk:"{{ t('map_evtype_talk') }}",field:"{{ t('map_evtype_field') }}",other:"{{ t('map_evtype_other') }}"}};</script>
<script src="/static/leaflet.js?v={{ v }}" onerror="document.getElementById('mapnotice').textContent='{{ t('map_assets_missing') }}'"></script>
<script src="/static/leaflet.markercluster.js?v={{ v }}"></script>
<script src="/static/map.js?v={{ v }}"></script>
<script src="/static/map_stats.js?v={{ v }}"></script>
{% endblock %}"""

ART_INDEX = """{% extends "base" %}{% block body %}
<h2 style="margin-bottom:6px">{{ t('dir_art_index_h') }}</h2>
<p class=muted style="margin-top:0">{{ t('dir_art_index_intro', n=items|length) }}</p>
<form method=get action="/art" style="max-width:420px;display:flex;gap:8px">
 <input type=hidden name=lang value="{{ lang }}">
 <input id=dirfilter name=q type=search value="{{ q }}" placeholder="{{ t('dir_art_search') }}" autocomplete=off>
</form>
<details class=chartbox {% if fnd.active %}open{% endif %} style="margin-top:12px"><summary style="cursor:pointer;font-weight:600">🔎 {{ t('fnd_h') }}</summary>
<form method=get action="/art" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:8px">
 <input type=hidden name=lang value="{{ lang }}">
 <div style="flex:1 1 180px"><label>{{ t('fnd_genus') }}</label><select name=g><option value="">{{ t('fnd_all') }}</option>{% for gname in fnd.genera %}<option value="{{ gname }}" {% if gname == fnd.g %}selected{% endif %}>{{ gname }}</option>{% endfor %}</select></div>
 <div style="flex:0 1 110px"><label>{{ t('fnd_pmin') }}</label><input name=pmin inputmode=decimal value="{{ fnd.pmin_raw }}" placeholder="0"></div>
 <div style="flex:0 1 110px"><label>{{ t('fnd_pmax') }}</label><input name=pmax inputmode=decimal value="{{ fnd.pmax_raw }}" placeholder="100"></div>
 <div style="flex:1 1 200px"><label>{{ t('fnd_land') }}</label><select name=land><option value="">{{ t('fnd_all') }}</option><option value="chd" {% if fnd.land == 'chd' %}selected{% endif %}>🇨🇭 {{ t('fnd_chd') }}</option>{% for c, n in fnd.countries %}<option value="{{ c }}" {% if c == fnd.land %}selected{% endif %}>{{ n }}</option>{% endfor %}</select></div>
 <label style="display:flex;gap:6px;align-items:center;margin:0 0 8px;color:#c9d1d9;font-size:14px"><input type=checkbox name=stock value=1 style="width:auto" {% if fnd.stock %}checked{% endif %}> {{ t('fnd_stock') }}</label>
 <div style="margin-bottom:2px"><button class=btn>{{ t('fnd_go') }}</button>{% if fnd.active %} <a class="btn grey" href="/art{{ qs() }}">{{ t('fnd_reset') }}</a>{% endif %}</div>
</form>
<p class=muted style="margin:6px 0 0;font-size:12px">{{ t('fnd_note') }}</p>
</details>
{% if fnd.active %}
<h3 style="margin:16px 0 6px">{{ t('fnd_results', n=fnd.rows|length) }}</h3>
{% if fnd.rows %}<table><tr><th>{{ t('fnd_col_species') }}</th><th>{{ t('fnd_col_shops') }}</th><th>{{ t('fnd_col_from') }}</th><th></th></tr>
{% for r in fnd.rows %}<tr><td><a href="/art/{{ r.name|replace(' ','_')|urlencode }}{{ qs() }}"><i>{{ r.name }}</i></a>{% if r.instock %} <span style="color:#3fb950;font-size:12px">● {{ t('dir_instock_n', n=r.instock) }}</span>{% endif %}</td>
 <td>{{ r.shops }}</td><td style="white-space:nowrap">{{ r.price }}</td>
 <td style="white-space:nowrap">{% if fnd.uid %}{% if r.watched %}<span class=muted title="{{ t('star_watched') }}">★</span>{% else %}<form method=post action="/konto/beobachtung/neu{{ qs() }}" style="display:inline"><input type=hidden name=csrf value="{{ fnd.csrf }}"><input type=hidden name=species value="{{ r.name }}"><input type=hidden name=back value="{{ fnd.back }}"><button class="btn grey small" title="{{ t('star_add') }}">☆</button></form>{% endif %}{% endif %}
 <a class="btn grey small" href="/konto?{{ r.nq }}#benachrichtigungen" title="{{ t('fnd_notify') }}">🔔</a></td></tr>{% endfor %}</table>
{% if fnd.more %}<p class=muted>{{ t('fnd_more', n=fnd.more) }}</p>{% endif %}
{% else %}<div class=flash>{{ t('fnd_none') }}</div>{% endif %}
{% else %}
{% if q and not items %}<div class=flash style="margin-top:12px">{{ t('dir_art_nohit', q=q) }}</div>{% endif %}
{% from "genmac" import genlist with context %}{{ genlist("artgen", groups, total, "dirfilter", q) }}
{% endif %}
{% endblock %}"""

ART = """{% extends "base" %}{% block body %}
<p class=muted style="margin:0"><a href="/art{{ qs() }}">← {{ t('dir_art_index_h') }}</a></p>
<h2 style="margin:6px 0;display:flex;flex-wrap:wrap;gap:10px;align-items:center"><i>{{ sp.name }}</i>
{% if star.uid %}{% if star.watched %}<a class="btn grey small" href="/konto{{ qs() }}#preise" title="{{ t('star_manage') }}">★ {{ t('star_watched') }}</a>
{% else %}<form method=post action="/konto/beobachtung/neu{{ qs() }}" style="display:inline"><input type=hidden name=csrf value="{{ star.csrf }}"><input type=hidden name=species value="{{ sp.name }}"><input type=hidden name=back value="{{ star.back }}"><button class="btn small" title="{{ t('star_hint') }}">☆ {{ t('star_add') }}</button></form>{% endif %}
{% if not star.genus_watched %}<form method=post action="/konto/beobachtung/neu{{ qs() }}" style="display:inline"><input type=hidden name=csrf value="{{ star.csrf }}"><input type=hidden name=species value="{{ sp.genus }}"><input type=hidden name=back value="{{ star.back }}"><button class="btn grey small" title="{{ t('star_hint') }}">☆ {{ t('star_genus', g=sp.genus) }}</button></form>{% endif %}
{% elif star.login %}<a class="btn grey small" href="{{ star.login }}" title="{{ t('star_login') }}">☆ {{ t('star_add') }}</a>{% endif %}</h2>
<p class=muted style="margin-top:0">{{ t('dir_genus') }}: <i>{{ sp.genus }}</i>{% if rare %} · 💎 {{ t('dir_rare') }}{% endif %}</p>
{% if prof.wiki %}<div class=chartbox style="margin-top:0;margin-bottom:12px"><h4>📖 {{ t('dir_about_h') }}{% if prof.wiki.lang != lang %} <span class=muted style="font-weight:400">({{ prof.wiki.lang|upper }})</span>{% endif %}</h4>
 <p style="margin:0 0 8px" lang="{{ prof.wiki.lang }}">{{ prof.wiki.text }}</p>
 <p class=muted style="margin:0;font-size:12px">{{ t('dir_wiki_text') }} <a href="{{ prof.wiki.url }}" target=_blank rel="noopener noreferrer">Wikipedia: „{{ prof.wiki.title }}“</a>
 · <a href="{{ prof.wiki.history }}" target=_blank rel="noopener noreferrer">{{ t('dir_wiki_authors') }}</a>
 · {{ t('dir_wiki_license') }} <a href="https://creativecommons.org/licenses/by-sa/4.0/deed.{{ 'de' if lang=='de' else 'en' }}" target=_blank rel="noopener noreferrer">CC BY-SA 4.0</a>{% if prof.wiki.cut %} · {{ t('dir_wiki_cut') }}{% endif %}</p>
</div>{% endif %}
{% if prof.gbif and prof.gbif.count %}<p style="margin:0 0 12px">🌍 {{ t('dir_gbif_line', n=gbif_count, c=prof.gbif.countries|length) }}{% if gbif_top %} · {{ t('dir_gbif_top') }}: {{ gbif_top|join(', ') }}{% endif %}
 <span class=muted style="font-size:12px">· {{ t('dir_gbif_src') }} <a href="{{ prof.gbif.url }}" target=_blank rel="noopener noreferrer">GBIF.org</a></span></p>{% endif %}
<p class=muted style="margin:0 0 14px">🔗 {{ t('dir_more_at') }} <a href="{{ ext.antwiki }}" target=_blank rel="noopener noreferrer">AntWiki</a>{% if ext.antweb %} · <a href="{{ ext.antweb }}" target=_blank rel="noopener noreferrer">AntWeb</a>{% endif %}{% if prof.gbif %} · <a href="{{ prof.gbif.url }}" target=_blank rel="noopener noreferrer">GBIF</a>{% endif %}</p>
<div class=kpigrid>
 <div class=kpi><div class=v>{{ sp.shops|length }}</div><div class=l>{{ t('dir_kpi_shops') }}</div></div>
 <div class=kpi><div class=v>{{ instock|length }}</div><div class=l>{{ t('dir_kpi_instock') }}</div></div>
 <div class=kpi><div class=v>{{ eur(trend.now) }}</div><div class=l>{{ t('dir_kpi_now') }}</div></div>
 <div class=kpi><div class=v>{{ eur(trend.m3) }}</div><div class=l>{{ t('dir_kpi_m3') }}</div></div>
 <div class=kpi><div class=v>{{ eur(trend.m12) }}</div><div class=l>{{ t('dir_kpi_m12') }}</div></div>
</div>
{% if trend['values']|select|list|length > 1 %}
<div class=chartbox><h4>{{ t('dir_trend_h') }} <span class=info title="{{ t('dir_trend_exp') }}">ⓘ</span></h4><div class=chartwrap style="height:260px"><canvas id=chTrend></canvas></div></div>
<script src="/static/chart.umd.js?v={{ v }}"></script>
<script>
(function(){var el=document.getElementById("chTrend"); if(!el||!window.Chart) return;
 new Chart(el,{type:"line",data:{labels:{{ trend.labels|tojson }},datasets:[{data:{{ trend['values']|tojson }},spanGaps:true,stepped:true,borderColor:"#3fb950",backgroundColor:"#3fb95033",fill:true,pointRadius:2}]},
 options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:"#8b949e"},grid:{color:"#21262d"}},y:{ticks:{color:"#8b949e",callback:function(v){return v+" €"}},grid:{color:"#21262d"}}}}});})();
</script>
{% endif %}
<p style="margin:12px 0 0"><a class=btn href="/art/{{ sp.name|replace(' ','_')|urlencode }}/karte.png{{ qs() }}" download>📤 {{ t('dir_card_btn') }}</a> <span class=muted style="font-size:12px">{{ t('dir_card_hint') }}</span></p>
<div class=chartbox><h4>{{ t('dir_offers_h') }}</h4>
<table><tr><th>{{ t('dir_col_shop') }}</th><th>{{ t('dir_col_price') }}</th><th>{{ t('dir_col_stock') }}</th><th></th></tr>
{% for o in offers %}<tr><td><a href="/shop/{{ o.domain|urlencode }}{{ qs() }}">{{ o.shop }}</a></td><td>{{ price(o) }}{% set sg = sig.get(o.pid) if o.in_stock else none %}{% if sg %} <span title="{{ sig_txt[sg] }}">{{ sig_emoji[sg] }}</span>{% endif %}</td>
<td>{% if o.in_stock %}<span style="color:#3fb950">● {{ t('dir_instock') }}</span>{% else %}<span class=muted>○ {{ t('dir_soldout') }}</span>{% endif %}</td>
<td><a href="{{ o.url }}" target=_blank rel="noopener noreferrer">{{ t('dir_to_offer') }} ↗</a></td></tr>{% endfor %}
</table>{% if sig_any %}<p class=muted style="font-size:12px;margin:8px 0 0">{{ sig_legend }}</p>{% endif %}</div>
{% include "swarm_box" %}
{% if season %}<div class=chartbox><h4>{{ season.h }}</h4><p style="margin:0 0 10px">{{ season.text }}</p>
<div style="display:grid;grid-template-columns:repeat(12,1fr);gap:4px;align-items:end;height:70px">{% for m in season.months %}<div title="{{ season.names[loop.index0] }}{% if m is not none %}: {{ (m * 100)|round|int }} %{% endif %}" style="background:{% if m is none %}#21262d{% elif m >= 0.5 %}#3fb950{% else %}#3fb95055{% endif %};height:{{ 6 if m is none else (6 + m * 64)|int }}px;border-radius:3px"></div>{% endfor %}</div>
<div style="display:grid;grid-template-columns:repeat(12,1fr);gap:4px;font-size:11px;text-align:center" class=muted>{% for n in season.short %}<div>{{ n }}</div>{% endfor %}</div></div>{% endif %}
<p class=muted>{{ t('dir_art_note', d=fetched) }}</p>
{% endblock %}"""

ART_MISSING = """{% extends "base" %}{% block body %}
<p class=muted style="margin:0"><a href="/art{{ qs() }}">← {{ t('dir_art_index_h') }}</a></p>
<h2><i>{{ art_name }}</i></h2>
<div class=flash>{{ t('dir_art_missing' if known else 'dir_art_unknown') }}</div>
{% if known %}<p class=muted>🔗 {{ t('dir_more_at') }} <a href="{{ ext.antwiki }}" target=_blank rel="noopener noreferrer">AntWiki</a>{% if ext.antweb %} · <a href="{{ ext.antweb }}" target=_blank rel="noopener noreferrer">AntWeb</a>{% endif %}</p>{% endif %}
{% endblock %}"""

SHOPS = """{% extends "base" %}{% block body %}
<h2 style="margin-bottom:6px">{{ t('dir_shops_h') }}</h2>
<p class=muted style="margin-top:0">{{ t('dir_shops_intro') }}</p>
<input id=dirfilter type=search placeholder="{{ t('dir_shops_search') }}" autocomplete=off style="max-width:420px">
<table id=dirlist style="margin-top:10px"><tr><th>{{ t('dir_col_shop') }}</th><th>{{ t('dir_col_rating') }}</th><th>{{ t('dir_col_species') }}</th><th>{{ t('dir_col_instock') }}</th></tr>
{% for s in items %}<tr data-q="{{ (s.name ~ ' ' ~ s.domain)|lower }}"><td><a href="/shop/{{ s.domain|urlencode }}{{ qs() }}">{{ s.name }}</a>{% if s.partner %} 🤝{% endif %}<div class=muted style="font-size:12px">{{ s.domain }}</div></td>
<td>{% if s.avg is not none %}⭐ {{ '%.2f'|format(s.avg) }} <span class=muted>({{ s.n }})</span>{% else %}<span class=muted>–</span>{% endif %}</td>
<td>{{ s.species or '–' }}</td><td>{{ s.instock or '–' }}</td></tr>{% endfor %}
</table>
{{ filter_js|safe }}
{% endblock %}"""

SHOP = """{% extends "base" %}{% block body %}
<p class=muted style="margin:0"><a href="/shops{{ qs() }}">← {{ t('dir_shops_h') }}</a></p>
<h2 style="margin:6px 0">{{ sh.name }}{% if partner %} <span class=tag style="color:#d2a8ff;border-color:#d2a8ff">🤝 {{ t('dir_partner') }}</span>{% endif %}</h2>
<p class=muted style="margin-top:0"><a href="{{ sh.url }}" target=_blank rel="noopener noreferrer">{{ sh.domain }} ↗</a>{% if sh.country %} · {{ country }}{% endif %}</p>
<div class=kpigrid>
 <div class=kpi><div class=v>{% if rv and rv.avg is not none %}⭐ {{ '%.2f'|format(rv.avg) }}{% else %}–{% endif %}</div><div class=l>{{ t('dir_kpi_rating') }}</div></div>
 <div class=kpi><div class=v>{{ rv.count if rv else 0 }}</div><div class=l>{{ t('dir_kpi_reviews') }}</div></div>
 <div class=kpi><div class=v>{{ rv.last if rv else '–' }}</div><div class=l>{{ t('dir_kpi_last') }}</div></div>
 {% if delivery %}<div class=kpi><div class=v>{{ ('%.1f'|format(delivery.avg)).replace('.', ',') if lang=='de' else '%.1f'|format(delivery.avg) }} {{ t('dir_days') }}</div><div class=l>{{ t('dir_kpi_delivery', n=delivery.n) }} <span class=info title="{{ t('dir_delivery_exp', m=delivery.median) }}">ⓘ</span></div></div>{% endif %}
 <div class=kpi><div class=v>{{ sh.species_count }}</div><div class=l>{{ t('dir_kpi_species') }}</div></div>
 <div class=kpi><div class=v>{{ sh.instock_count }} / {{ sh.offers_count }}</div><div class=l>{{ t('dir_kpi_instock_of') }}</div></div>
 <div class=kpi><div class=v>{{ sh.exclusive|length }}</div><div class=l>{{ t('dir_kpi_exclusive') }}</div></div>
 <div class=kpi><div class=v>{% if sh.price_level is none %}–{% elif sh.price_level > 0 %}+{{ sh.price_level }} %{% else %}{{ sh.price_level }} %{% endif %}</div><div class=l>{{ t('dir_kpi_level', n=sh.price_level_n) }} <span class=info title="{{ t('dir_level_exp') }}">ⓘ</span></div></div>
</div>
{% if codes %}<div class=chartbox><h4>🏷️ {{ t('dir_codes_h') }}</h4>
{% for c in codes %}<div><code>{{ c.code }}</code>{% if c.discount %} · {{ c.discount }}{% endif %}{% if c.min_order %} · {{ t('dir_min_order', m=c.min_order) }}{% endif %} · <span class=muted>{{ t('dir_perm') if c.permanent else (t('dir_until', d=c.until) if c.until else t('dir_open')) }}</span></div>{% endfor %}
</div>{% endif %}

{% if member %}
 {% if warns %}<div class=chartbox style="border-color:#d2992266"><h4>⚠️ {{ t('dir_warn_h') }}</h4>
 {% for w in warns %}<div>{{ w.emoji }} {{ w.text }}{% if w.date %} <span class=muted>({{ w.date }})</span>{% endif %}</div>{% endfor %}
 <p class=muted style="margin-bottom:0">{{ t('dir_warn_note') }} <a href="/impressum{{ qs() }}#kontakt">{{ t('contact_h') }}</a></p></div>{% endif %}
 {% if rv and rv.years %}<div class=chartbox><h4>{{ t('dir_years_h') }}</h4><div class=chartwrap style="height:240px"><canvas id=chYears></canvas></div></div>
 <div class=chartbox><h4>{{ t('dir_dist_h') }}</h4><div class=chartwrap style="height:220px"><canvas id=chDist></canvas></div></div>
 <script src="/static/chart.umd.js?v={{ v }}"></script>
 <script>
 (function(){ if(!window.Chart) return; var g={color:"#21262d"}, tk={color:"#8b949e"};
  new Chart(document.getElementById("chYears"),{type:"bar",data:{labels:{{ years_labels|tojson }},datasets:[
   {type:"line",label:"{{ t('dir_avg') }}",data:{{ years_avg|tojson }},borderColor:"#3fb950",yAxisID:"y",tension:.2},
   {label:"{{ t('dir_kpi_reviews') }}",data:{{ years_n|tojson }},backgroundColor:"#1f6feb66",yAxisID:"y2"}]},
   options:{maintainAspectRatio:false,plugins:{legend:{labels:tk}},scales:{x:{ticks:tk,grid:g},y:{min:0,max:10,ticks:tk,grid:g},y2:{position:"right",ticks:tk,grid:{display:false},beginAtZero:true}}}});
  new Chart(document.getElementById("chDist"),{type:"bar",data:{labels:{{ dist_labels|tojson }},datasets:[{data:{{ dist_n|tojson }},backgroundColor:"#3fb95099"}]},
   options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:tk,grid:g},y:{ticks:tk,grid:g,beginAtZero:true}}}});
 })();
 </script>{% endif %}
 {% if rv and rv.recent %}<div class=chartbox><h4>{{ t('dir_recent_h') }}</h4>
 {% for r in rv.recent %}<div class=card><div class=muted>{{ r.date }}{% if r.rating is not none %} · ⭐ {{ r.rating|round(1) }}{% endif %}</div>
 {% if r.pos %}<div>👍 {{ r.pos }}</div>{% endif %}{% if r.neg %}<div>👎 {{ r.neg }}</div>{% endif %}</div>{% endfor %}
 <p class=muted style="margin-bottom:0">{{ t('dir_recent_note') }}</p></div>{% endif %}
{% elif login_url %}
 <div class=flash>🔒 {{ t('dir_login_more') }} <a href="{{ login_url }}">{{ t('dir_login_btn') }}</a></div>
{% endif %}

{% if sh.exclusive %}<div class=chartbox><h4>💎 {{ t('dir_excl_h') }}</h4>
<div class=raritygrid>{% for n in sh.exclusive %}<div><a href="/art/{{ n|replace(' ','_')|urlencode }}{{ qs() }}">{{ n }}</a></div>{% endfor %}</div></div>{% endif %}
{% if sh.species %}<div class=chartbox><h4>{{ t('dir_species_h', n=sh.species|length) }}</h4>
<input id=shopfilter type=search placeholder="{{ t('dir_art_search') }}" autocomplete=off style="max-width:360px">
{% from "genmac" import genlist with context %}{{ genlist("shopgen", groups, sh.species|length, "shopfilter") }}{% if sig_legend %}<p class=muted style="font-size:12px;margin:8px 0 0">{{ sig_legend }}</p>{% endif %}</div>{% endif %}
<p class=muted>{{ t('dir_shop_note') }}</p>
{% endblock %}"""

RUECKBLICK = """{% extends "base" %}{% block body %}
{% macro m(n) %}{% if 0 < n < 3 %}&lt; 3{% else %}{{ n }}{% endif %}{% endmacro %}
<h2 style="margin-bottom:6px">🎆 {{ t('yr_page_h', y=d.year) }}</h2>
<p class=muted style="margin-top:0">{{ t('yr_page_intro', y=d.year, n=d.year + 1) }}</p>
{% if years|length > 1 %}<div class=kpigrid style="grid-template-columns:repeat(auto-fill,minmax(110px,1fr));margin-bottom:12px">
 {% for y in years %}<a class=kpi href="/rueckblick/{{ y }}{{ qs() }}" style="text-decoration:none;{% if y == d.year %}border-color:#58a6ff;background:#1f6feb22{% endif %}"><div class=v>{{ y }}</div><div class=l>{% if y == d.year %}{{ t('yr_tile_now') }}{% else %}{{ t('yr_tile_open') }}{% endif %}</div></a>{% endfor %}
</div>{% endif %}
{% if me %}<div class=chartbox style="border-color:#58a6ff">
 <h3 style="margin-top:0">{{ tp('yrp_title', year=d.year, name=me.name) }} <span class=muted style="font-size:12px;font-weight:normal">· 🔒 {{ t('yr_me_private') }}</span></h3>
 {% if not me.has %}<p>{{ tp('yrp_empty', year=d.year) }}</p>{% else %}
 <div class=kpigrid>
  <div class=kpi><div class=v>{{ me.reviews }}</div><div class=l>⭐ {{ t('yr_me_k_reviews') }}</div></div>
  {% if me.reviews_avg is not none %}<div class=kpi><div class=v>⭐ {{ ('%.1f'|format(me.reviews_avg)).replace('.', ',') if lang == 'de' else '%.1f'|format(me.reviews_avg) }}</div><div class=l>{{ t('yr_me_k_avg') }}</div></div>{% endif %}
  <div class=kpi><div class=v>{{ me.commands }}</div><div class=l>🤖 {{ t('yr_me_k_cmds') }}</div></div>
  <div class=kpi><div class=v>{{ me.achievements|length }}</div><div class=l>🏅 {{ t('yr_me_k_ach') }}</div></div>
  {% if me.inat and me.inat.obs %}<div class=kpi><div class=v>{{ me.inat.species }}</div><div class=l>🔬 {{ t('yr_me_k_inat') }}</div></div>{% endif %}
 </div>
 {% if me.reviews %}<h4>{{ tp('yrp_h_reviews') }}</h4>
  <p style="margin:2px 0">{{ tp('yrp_reviews', n=me.reviews) }}</p>
  {% if me.reviews_top %}<p style="margin:2px 0">{{ tp('yrp_reviews_top', list='') }}{% for s, n in me.reviews_top %}<a href="/shop/{{ s|urlencode }}{{ qs() }}">{{ names.get(s, s) }}</a> ({{ n }}){% if not loop.last %}, {% endif %}{% endfor %}</p>{% endif %}
  {% if me.reviews_first %}<p style="margin:2px 0">{{ tp('yrp_reviews_first', date=me.reviews_first) }}</p>{% endif %}
 {% endif %}
 <h4>{{ tp('yrp_h_bot') }}</h4>
  <p style="margin:2px 0">{{ tp('yrp_commands', n=me.commands) }}</p>
  {% if me.top_commands %}<p style="margin:2px 0">{{ tp('yrp_top', list='') }}{% for c, n in me.top_commands %}<code>/{{ c }}</code> ({{ n }}){% if not loop.last %}, {% endif %}{% endfor %}</p>{% endif %}
  {% if me.first %}<p style="margin:2px 0">{{ tp('yrp_first', date=me.first) }}</p>{% endif %}
 {% if me.notifications or me.watches or me.tracked or me.rsvps or (me.inat and me.inat.obs) or me.swarm %}<h4>{{ tp('yrp_h_ants') }}</h4>{% endif %}
  {% set ants = [] %}{% if me.notifications %}{% set _ = ants.append(tp('yrp_notifications', n=me.notifications)) %}{% endif %}{% if me.watches %}{% set _ = ants.append(tp('yrp_watches', n=me.watches)) %}{% endif %}{% if me.tracked %}{% set _ = ants.append(tp('yrp_tracked', n=me.tracked)) %}{% endif %}{% if me.rsvps %}{% set _ = ants.append(tp('yrp_rsvps', n=me.rsvps)) %}{% endif %}
  {% if ants %}<p style="margin:2px 0">{{ ants|join(' · ') }}</p>{% endif %}
  {% if me.inat and me.inat.obs %}<p style="margin:2px 0">{{ tp('yrp_inat', obs=me.inat.obs, species=me.inat.species) }}</p>{% endif %}
  {% if me.swarm %}<p style="margin:2px 0">{{ tp('yrp_swarm', n=me.swarm, taxa=me.swarm_taxa) }}</p>{% endif %}
 {% if me.ach %}<h4>{{ tp('yrp_h_ach', n=me.ach|length) }}</h4>
  <p style="margin:2px 0">{% for e, n in me.ach %}{{ e }} {{ n }}{% if not loop.last %} · {% endif %}{% endfor %}</p>{% endif %}
 {% endif %}
</div>
{% elif login_url %}<div class=flash>✨ {{ t('yr_me_login') }} <a href="{{ login_url }}">{{ t('dir_login_btn') }}</a></div>{% endif %}
{% if me or login_url %}<h3>👥 {{ t('yr_community_h') }}</h3>{% endif %}
<div class=kpigrid>
 <div class=kpi><div class=v>{{ d.reviews.count }}</div><div class=l>{{ t('yr_k_reviews') }}</div></div>
 {% if d.reviews.avg is not none %}<div class=kpi><div class=v>⭐ {{ '%.2f'|format(d.reviews.avg) }}</div><div class=l>{{ t('yr_k_avg') }}</div></div>{% endif %}
 <div class=kpi><div class=v>{{ d.new_species }}</div><div class=l>{{ t('yr_k_species') }}</div></div>
 <div class=kpi><div class=v>{{ d.new_shops|length }}</div><div class=l>{{ t('yr_k_shops') }}</div></div>
 <div class=kpi><div class=v>{{ d.codes }}</div><div class=l>{{ t('yr_k_codes') }}</div></div>
 <div class=kpi><div class=v>{{ m(d.keepers) }}</div><div class=l>{{ t('yr_k_keepers') }}</div></div>
 <div class=kpi><div class=v>{{ d.events }}</div><div class=l>{{ t('yr_k_events') }}</div></div>
 <div class=kpi><div class=v>{{ m(d.achievements) }}</div><div class=l>{{ t('yr_k_ach') }}</div></div>
 <div class=kpi><div class=v>{{ m(d.commands) }}</div><div class=l>{{ t('yr_k_cmds') }}</div></div>
 {% if d.inat and d.inat.obs %}<div class=kpi><div class=v>{{ d.inat.obs }}</div><div class=l>🔬 {{ t('yr_k_inat_obs') }}</div></div>
 <div class=kpi><div class=v>{{ d.inat.species }}</div><div class=l>🔬 {{ t('yr_k_inat_species') }}</div></div>{% endif %}
</div>
{% if d.inat and d.inat.obs %}<div class=chartbox><h4>🔬 {{ t('yr_inat_h') }} <a href="/inat{{ qs() }}" style="font-size:12px;font-weight:400">{{ t('inat_all_link') }}</a></h4>
 <p style="margin:2px 0">👥 {{ m(d.inat.people) }} {{ t('yr_inat_people') }}</p>
 {% if d.inat.new %}<p style="margin:2px 0">🌟 {{ d.inat.new }} {{ t('yr_inat_new') }}</p>{% endif %}
 {% if d.inat.top %}<p style="margin:2px 0">🏆 {{ t('yr_inat_top') }}: <a href="/art/{{ d.inat.top[0]|replace(' ','_')|urlencode }}{{ qs() }}"><i>{{ d.inat.top[0] }}</i></a> ({{ d.inat.top[1] }}×)</p>{% endif %}
</div>{% endif %}
{% if d.swarm and d.swarm.n %}<div class=chartbox><h4>🪽 {{ t('yr_swarm_h') }} <a href="/schwarmflug{{ qs() }}" style="font-size:12px;font-weight:400">{{ t('sw_art_more') }} →</a></h4>
 <p style="margin:2px 0">{{ t('yr_swarm_n', n=d.swarm.n, taxa=d.swarm.taxa) }} · 👥 {{ m(d.swarm.people) }} {{ t('yr_swarm_people') }}</p>
 {% if d.swarm.start %}<p style="margin:2px 0">🎉 {{ t('yr_swarm_start') }}: <i>{{ d.swarm.start[0] }}</i> ({{ d.swarm.start[1]|dmy }})</p>{% endif %}
 {% if d.swarm.top %}<p style="margin:2px 0">🏆 {{ t('yr_swarm_top') }}: <i>{{ d.swarm.top[0] }}</i> ({{ d.swarm.top[1] }}×)</p>{% endif %}
 {% if d.swarm.peak %}<p style="margin:2px 0">📈 {{ t('yr_swarm_peak') }}: {{ d.swarm.peak }}</p>{% endif %}
 {% if d.swarm.abroad %}<p style="margin:2px 0">🌍 {{ t('yr_swarm_abroad', n=d.swarm.abroad, c=d.swarm.abroad_countries) }}</p>{% endif %}
</div>{% endif %}
{% if d.reviews.most or d.reviews.best or d.fastest %}<div class=chartbox><h4>⭐ {{ t('yr_k_reviews') }}</h4>
 {% if d.reviews.most %}<p>{{ t('yr_most_l') }}: {% for s, n in d.reviews.most %}<a href="/shop/{{ s|urlencode }}{{ qs() }}">{{ names.get(s, s) }}</a> ({{ n }}){% if not loop.last %}, {% endif %}{% endfor %}</p>{% endif %}
 {% if d.reviews.best %}<p>{{ t('yr_best_l') }}: {% for s, a, n in d.reviews.best %}<a href="/shop/{{ s|urlencode }}{{ qs() }}">{{ names.get(s, s) }}</a> (⭐ {{ '%.2f'|format(a) }}){% if not loop.last %}, {% endif %}{% endfor %}</p>{% endif %}
 {% if d.fastest %}<p>🚚 {{ t('yr_fast_l') }}: <a href="/shop/{{ d.fastest[0]|urlencode }}{{ qs() }}">{{ names.get(d.fastest[0], d.fastest[0]) }}</a> (Ø {{ d.fastest[1] }} {{ t('dir_days') }})</p>{% endif %}
</div>{% endif %}
{% if d.drops %}<div class=chartbox><h4>📉 {{ t('yr_drops_h') }}</h4>
 {% for sp, shop, a, b, p in d.drops %}<div><a href="/art/{{ sp|replace(' ','_')|urlencode }}{{ qs() }}"><i>{{ sp }}</i></a> · {{ shop }}: {{ '%.2f'|format(a) }} € → {{ '%.2f'|format(b) }} € (−{{ p }} %)</div>{% endfor %}
</div>{% endif %}
{% if d.new_shops %}<div class=chartbox><h4>🏪 {{ t('yr_k_shops') }}</h4><p style="margin:0">{{ d.new_shops|join(', ') }}</p></div>{% endif %}
<p class=muted>{{ t('yr_page_note') }}</p>
{% endblock %}"""

CHATWIDGET = r"""<style>
#aic-btn{position:fixed;right:18px;bottom:18px;z-index:50;width:56px;height:56px;border-radius:50%;border:1px solid #30363d;background:#238636;color:#fff;font-size:26px;cursor:pointer;box-shadow:0 4px 14px #0008}
#aic-box{position:fixed;right:18px;bottom:84px;z-index:50;width:min(400px,calc(100vw - 36px));height:min(560px,calc(100vh - 110px));display:none;flex-direction:column;background:#0d1117;border:1px solid #30363d;border-radius:12px;box-shadow:0 8px 30px #000a;overflow:hidden}
#aic-box.open{display:flex}
#aic-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #21262d;background:#161b22}
#aic-head b{flex:1}
#aic-head a,#aic-head button{color:#8b949e;font-size:13px;background:none;border:0;cursor:pointer}
#aic-model{width:100%;margin:0;padding:6px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;font-size:13px}
#aic-msgs{flex:1;overflow-y:auto;padding:10px 12px;font-size:14px;line-height:1.45}
.aic-m{margin:0 0 10px;padding:8px 10px;border-radius:10px;max-width:90%;word-wrap:break-word;white-space:pre-wrap}
.aic-u{background:#1f6feb33;margin-left:auto}
.aic-a{background:#161b22;border:1px solid #21262d}
.aic-a code{background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:0 4px}
.aic-s{display:block;color:#8b949e;font-size:12px}
.aic-f{display:block;color:#8b949e;font-size:11px;margin-top:6px;white-space:normal}
#aic-form{display:flex;gap:6px;padding:8px;border-top:1px solid #21262d}
#aic-in{flex:1;resize:none;height:54px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:6px 8px;font:inherit;font-size:14px}
#aic-send{background:#238636;color:#fff;border:0;border-radius:8px;padding:0 14px;cursor:pointer}
#aic-send:disabled{opacity:.5;cursor:default}
#aic-note{font-size:11px;color:#8b949e;padding:0 10px 8px}
</style>
<button id=aic-btn type=button title="{{ t('chat_title') }}" aria-label="{{ t('chat_title') }}">💬</button>
<div id=aic-box role=dialog aria-label="{{ t('chat_title') }}">
 <div id=aic-head><b>🤖 {{ t('chat_title') }}</b><button type=button id=aic-new>{{ t('chat_new') }}</button><button type=button id=aic-close aria-label="close">✕</button></div>
 <div style="padding:8px 12px 0"><select id=aic-model aria-label="{{ t('chat_model') }}"></select></div>
 <div id=aic-msgs><div class="aic-m aic-a">{{ t('chat_hello') }}</div></div>
 <form id=aic-form><textarea id=aic-in maxlength=1500 placeholder="{{ t('chat_placeholder') }}"></textarea><button id=aic-send type=submit>➤</button></form>
 <div id=aic-note>{{ t('chat_note', h=chat_ttl) }}</div>
</div>
<script>(function(){
 var CSRF={{ chat_csrf|tojson }}, LANG={{ lang|tojson }}, L={think:{{ t('chat_thinking')|tojson }},err:{{ t('chat_error')|tojson }}};
 var box=document.getElementById("aic-box"),msgs=document.getElementById("aic-msgs"),inp=document.getElementById("aic-in"),
     send=document.getElementById("aic-send"),sel=document.getElementById("aic-model"),conv=null,loaded=false;
 try{conv=sessionStorage.getItem("aic-conv")}catch(e){}
 function esc(t){return t.replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
 function md(t){t=esc(t);
  t=t.replace(/```([\s\S]*?)```/g,function(_,c){return "<code>"+c+"</code>"});
  t=t.replace(/`([^`\n]+)`/g,"<code>$1</code>");
  t=t.replace(/\*\*([^*\n]+)\*\*/g,"<b>$1</b>").replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<i>$2</i>");
  t=t.replace(/\[([^\]\n]+)\]\(&lt;?(https?:\/\/[^\s)&]+(?:&amp;[^\s)&]+)*)&gt;?\)/g,'<a href="$2" target=_blank rel="noopener noreferrer">$1</a>');
  t=t.replace(/&lt;(https?:\/\/[^\s<>]+?)&gt;/g,'<a href="$1" target=_blank rel="noopener noreferrer">$1</a>');
  t=t.replace(/^-# (.+)$/gm,'<span class=aic-s>$1</span>');
  t=t.replace(/^#{1,3} (.+)$/gm,"<b>$1</b>");
  return t}
 var hist=[];try{hist=JSON.parse(sessionStorage.getItem("aic-hist")||"[]")}catch(e){}
 function keep(){try{sessionStorage.setItem("aic-hist",JSON.stringify(hist.slice(-30)))}catch(e){}}
 function add(cls,html){var d=document.createElement("div");d.className="aic-m "+cls;d.innerHTML=html;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d}
 hist.forEach(function(h){add(h[0],h[1])});
 function loadModels(){if(loaded)return;loaded=true;
  fetch("/chat/models?lang="+LANG,{credentials:"same-origin"}).then(function(r){return r.json()}).then(function(j){
   (j.models||[]).forEach(function(m){var o=document.createElement("option");o.value=m.id;o.textContent=m.emoji+" "+m.label+" · "+m.tier+(m.rec?" · 👍":"");o.title=m.desc;if(m.id===j.preselect)o.selected=true;sel.appendChild(o)})})}
 document.getElementById("aic-btn").onclick=function(){box.classList.toggle("open");if(box.classList.contains("open")){loadModels();inp.focus()}};
 document.getElementById("aic-close").onclick=function(){box.classList.remove("open")};
 document.getElementById("aic-new").onclick=function(){conv=null;try{sessionStorage.removeItem("aic-conv")}catch(e){}
  hist=[];keep();msgs.innerHTML="";add("aic-a",esc({{ t('chat_hello')|tojson }}))};
 inp.addEventListener("keydown",function(e){if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();document.getElementById("aic-form").requestSubmit()}});
 document.getElementById("aic-form").onsubmit=function(e){e.preventDefault();var q=inp.value.trim();if(!q||send.disabled)return;
  add("aic-u",esc(q));hist.push(["aic-u",esc(q)]);keep();inp.value="";send.disabled=true;var w=add("aic-a","<i>"+esc(L.think)+"</i>");
  fetch("/chat/send?lang="+LANG,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF":CSRF},
   body:JSON.stringify({msg:q,conv:conv,model:sel.value||null})})
  .then(function(r){return r.json().catch(function(){return {answer:L.err}})})
  .then(function(j){w.innerHTML=md(j.answer||L.err)+(j.footer?'<span class=aic-f>'+esc(j.footer)+'</span>':"");
   hist.push(["aic-a",w.innerHTML]);keep();
   if(j.conv){conv=String(j.conv);try{sessionStorage.setItem("aic-conv",conv)}catch(e){}}
   msgs.scrollTop=msgs.scrollHeight})
  .catch(function(){w.textContent=L.err}).finally(function(){send.disabled=false;inp.focus()})};
})();</script>"""

INAT_LIST = """{% extends "base" %}{% block body %}
{% macro m(n) %}{% if 0 < n < 3 %}&lt; 3{% else %}{{ n }}{% endif %}{% endmacro %}
<h2 style="margin-bottom:6px">🔬 {{ t('inat_page_h') }}</h2>
<p class=muted style="margin-top:0">{{ t('inat_page_intro') }}</p>
<div class=kpigrid>
 <div class=kpi><div class=v>{{ d.taxa|length }}</div><div class=l>{{ t('inat_k_species') }}</div></div>
 <div class=kpi><div class=v>{{ d.obs }}</div><div class=l>{{ t('inat_k_obs') }}</div></div>
 <div class=kpi><div class=v>{{ m(d.people) }}</div><div class=l>{{ t('inat_k_people') }}</div></div>
 {% if d.years %}<div class=kpi><div class=v>{{ d.years[0] }}</div><div class=l>{{ t('inat_k_since') }}</div></div>{% endif %}
</div>
{% if d.taxa %}
<form onsubmit="return false" style="max-width:420px;margin-top:14px"><input id=inatfilter type=search placeholder="{{ t('dir_art_search') }}" autocomplete=off></form>
{% from "genmac" import genlist with context %}{{ genlist("inatgen", groups, d.taxa|length, "inatfilter") }}
{% else %}<div class=flash style="margin-top:12px">{{ t('inat_page_empty') }}</div>{% endif %}
<p class=muted>{{ t('inat_page_note') }}</p>
{% endblock %}"""

RUECKBLICK_WAIT = """{% extends "base" %}{% block body %}
<div style="max-width:680px;margin:30px auto;text-align:center">
<div style="font-size:54px">🐜💤</div>
<h2 style="margin:8px 0">{{ t('yr_wait_h') }}</h2>
<p class=muted>{{ t('yr_wait_text', d=opens, y=ry) }}</p>
<div class=chartbox style="text-align:left;margin-top:22px">
 <h4>🐜 {{ t('yr_wait_fact_h') }}</h4>
 <p id=fact style="font-size:16px;line-height:1.5;margin:6px 0 10px">{{ facts[start].text }}</p>
 <p class=muted style="font-size:12px;margin:0">{{ t('yr_wait_src') }}: <a id=factsrc href="{{ facts[start].url }}" target=_blank rel="noopener noreferrer">{{ facts[start].src }}</a></p>
 <p style="margin:12px 0 0"><a href="#" id=factnext class=btn>{{ t('yr_wait_more') }}</a></p>
</div>
{% if years %}<h3 style="margin-top:28px">📚 {{ t('yr_wait_past_h') }}</h3>
<div class=kpigrid style="grid-template-columns:repeat(auto-fill,minmax(110px,1fr))">
 {% for y in years %}<a class=kpi href="/rueckblick/{{ y }}{{ qs() }}" style="text-decoration:none"><div class=v>{{ y }}</div><div class=l>{{ t('yr_tile_open') }}</div></a>{% endfor %}
</div>{% endif %}
</div>
<script>(function(){var F={{ facts|tojson }},i={{ start }},
 p=document.getElementById("fact"),s=document.getElementById("factsrc");
 document.getElementById("factnext").addEventListener("click",function(e){e.preventDefault();
  i=(i+1+Math.floor(Math.random()*(F.length-1)))%F.length;
  p.textContent=F[i].text;s.textContent=F[i].src;s.href=F[i].url;});})();</script>
{% endblock %}"""

GENMAC = """{% macro genlist(gid, groups, total, filter_id, q='') %}
<p style="margin:10px 0 4px"><a href="#" data-gall="{{ gid }}" data-open=1>{{ t('dir_expand_all') }}</a> · <a href="#" data-gall="{{ gid }}" data-open=0>{{ t('dir_collapse_all') }}</a> <span id="{{ gid }}-cnt" class=muted style="font-size:12px;margin-left:8px"></span></p>
<div id="{{ gid }}" data-genlist data-filter="{{ filter_id }}" data-total="{{ total }}">{% for genus, rows in groups %}
<details class=gen style="border-bottom:1px solid #21262d;padding:4px 0"{% if q %} open{% endif %}>
 <summary style="cursor:pointer"><i>{{ genus }}</i> <span class=muted style="font-size:12px">· {{ t('dir_species_count', n=rows|length) }}{% set gi = rows|selectattr('instock')|list|length %}{% if gi %} · {{ t('dir_instock_species', n=gi) }}{% endif %}</span></summary>
 <div class=raritygrid style="font-style:normal;margin:4px 0 6px 14px">{% for r in rows %}<div data-q="{{ r.name|lower }}"><a href="/art/{{ r.name|replace(' ','_')|urlencode }}{{ qs() }}"><i>{{ r.name }}</i></a>{% if r.note %} <span class=rshop>· {{ r.note }}</span>{% endif %}</div>{% endfor %}</div>
</details>{% endfor %}</div>
<script>(function(){
 var box=document.getElementById("{{ gid }}"); if(!box) return;
 var inp=document.getElementById(box.getAttribute("data-filter")), cnt=document.getElementById("{{ gid }}-cnt"),
     gens=box.querySelectorAll("details.gen"), total=box.getAttribute("data-total");
 function apply(){
  var q=(inp?inp.value:"").trim().toLowerCase(), shown=0;
  gens.forEach(function(d){
   var hit=0; d.querySelectorAll("[data-q]").forEach(function(r){
    var ok=!q||r.getAttribute("data-q").indexOf(q)>=0; r.style.display=ok?"":"none"; if(ok) hit++;});
   d.style.display=hit?"":"none"; if(q) d.open=hit>0; shown+=hit;});
  if(cnt) cnt.textContent=q?shown+" / "+total:"";
 }
 document.querySelectorAll('[data-gall="{{ gid }}"]').forEach(function(a){a.addEventListener("click",function(e){
  e.preventDefault(); var o=a.getAttribute("data-open")==="1";
  gens.forEach(function(d){if(d.style.display!=="none") d.open=o;});});});
 if(inp){inp.addEventListener("input",apply); apply();}
})();</script>
{% endmacro %}"""

_FILTER_JS = """<script>(function(){var inp=document.getElementById("dirfilter"),
 rows=document.querySelectorAll("#dirlist [data-q]"); if(!inp) return;
 function apply(){var q=inp.value.trim().toLowerCase();
  rows.forEach(function(r){r.style.display=(!q||r.getAttribute("data-q").indexOf(q)>=0)?"":"none";});}
 inp.addEventListener("input",apply); apply();})();</script>"""

ENV = Environment(loader=DictLoader({"base": BASE, "board": BOARD, "submit": SUBMIT,
                                     "detail": DETAIL, "login": LOGIN, "admin": ADMIN,
                                     "edit": EDIT, "statusdetail": STATUSDETAIL,
                                     "stats": STATS, "legal": LEGAL, "map": MAP,
                                     "art_index": ART_INDEX, "art": ART, "art_missing": ART_MISSING,
                                     "shops": SHOPS, "shop": SHOP, "genmac": GENMAC,
                                     "rueckblick": RUECKBLICK, "rueckblick_wait": RUECKBLICK_WAIT, "inat_list": INAT_LIST,
                                     "chatwidget": CHATWIDGET}),
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
                v=VERSION,    # Cache-Busting für statische Dateien (z. B. Favicon); Seiten dürfen überschreiben
                # Partner-Leiste über dem Footer – nur wenn Partner eingetragen sind und nicht
                # auf der Karte (dort steht der Partner-Kasten in der rechten Spalte).
                partner_strip=(_load_partners() if name != "map" else []))
    i18n["nav_member"] = bool(_aam_member(req))
    i18n["intro_v"] = _intro_asset_v()
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


def _chat_ttl() -> int:
    import config as cfg
    return cfg.AI_CHAT_CONVERSATION_TTL_HOURS


def _chat_uid(req) -> str | None:
    """Discord-ID, wenn der Web-KI-Chat für diese Anfrage angeboten wird."""
    if not BOARD_AI_CHAT:
        return None
    try:
        if "AiChatCog" not in req.app["bot"].cogs:
            return None
    except Exception:
        return None
    return _aam_member(req)


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


def _sheet_tile() -> dict:
    """Google Sheet: wann zuletzt Warnhinweise erfolgreich abgeglichen wurden (stündliche
    Pipeline). Nutzt den Merker aus utils.sheet – kein zusätzlicher Abruf."""
    name = "Google Sheet"
    try:
        from utils.sheet import SYNC_STATUS
    except Exception:
        return dict(name=name, state="off", detail="nicht verfügbar")
    now = time.time()
    # Jeder erfolgreiche Zugriff zählt: Bewertungen beim Start, Warnhinweise stündlich,
    # Shop-Bewertungen alle 48 h.
    labels = {"reviews": "Bewertungen geladen", "warnings": "Warnhinweise abgeglichen",
              "ratings": "Shop-Bewertungen abgeglichen"}
    ok_kind = max(SYNC_STATUS["ok"], key=SYNC_STATUS["ok"].get, default=None)
    ok = SYNC_STATUS["ok"].get(ok_kind) if ok_kind else None
    fail = max(SYNC_STATUS["fail"].values(), default=None)
    if ok is None:
        if now - SYNC_STATUS.get("started", now) < 2 * 3600:
            return dict(name=name, state="ok", detail="noch kein Abgleich seit dem Start")
        return dict(name=name, state="down" if fail else "warn",
                    detail="kein erfolgreicher Abgleich seit dem Start")
    age = now - ok
    state = "ok" if age < 3 * 3600 else ("warn" if age < 24 * 3600 else "down")
    detail = f"{labels.get(ok_kind, 'Zugriff')} {_fmt_age(age)}"
    if fail and fail > ok:
        state = "warn" if state == "ok" else state
        detail += f" · letzter Versuch fehlgeschlagen ({_fmt_age(now - fail)})"
    return dict(name=name, state=state, detail=detail)


async def _ai_cost_tile(bot) -> dict:
    """KI-Kosten heute (UTC) gegen Tagesbudget und 14-Tage-Schnitt."""
    name = "KI-Kosten heute"
    try:
        from config import AI_CHAT_DAILY_BUDGET_USD as budget
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = await execute_db(
            bot, "SELECT date, cost_usd FROM ai_chat_budget WHERE user_id=0 "
                 "AND date >= date('now', '-14 days')", fetch=True) or []
    except Exception as e:
        return dict(name=name, state="off", detail=str(e)[:80])
    cost = sum(r["cost_usd"] for r in rows if r["date"] == today)
    avg = sum(r["cost_usd"] for r in rows if r["date"] != today) / 14
    state, why = "ok", ""
    if budget and cost >= budget:
        state, why = "down", " · Tagesbudget erreicht"
    elif budget and cost >= 0.8 * budget:
        state, why = "warn", " · über 80 % des Tagesbudgets"
    elif avg > 0 and cost >= 3 * avg and cost >= 0.10:
        state, why = "warn", " · deutlich über dem Schnitt"
    eur = lambda v: f"{v:.2f}".replace(".", ",")
    detail = f"{eur(cost)} $ von {eur(budget)} $ · Ø 14 Tage {eur(avg)} $/Tag{why}"
    return dict(name=name, state=state, detail=detail)


def _inat_tile() -> dict:
    """iNaturalist-API: letzter Erfolg / Fehler aus utils.inat.API_STATUS."""
    from utils import inat as I
    ok, err = I.API_STATUS.get("ok_at"), I.API_STATUS.get("err_at")
    if not ok and not err:
        return dict(name="iNaturalist-API", state="off", detail="seit dem Start noch nicht abgefragt")
    if err and (not ok or err > ok):
        return dict(name="iNaturalist-API", state="warn",
                    detail=f"letzter Abruf fehlgeschlagen ({I.API_STATUS.get('err')}) · "
                           f"{berlin_from_utc_naive(err, '%Y-%m-%d %H:%M:%S')}")
    return dict(name="iNaturalist-API", state="ok",
                detail=f"letzter erfolgreicher Abruf {berlin_from_utc_naive(ok, '%Y-%m-%d %H:%M:%S')}")


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
    core.append(dict(name="KI-Chat (Discord-Kanal)", state="ok" if AI_CHAT_PUBLIC else "off",
                     detail="aktiv" if AI_CHAT_PUBLIC else "deaktiviert"))
    chat_cog = bot is not None and "AiChatCog" in getattr(bot, "cogs", {})
    core.append(dict(name="KI-Chat (Board)", state="ok" if (BOARD_AI_CHAT and chat_cog) else "off",
                     detail="aktiv für eingeloggte Mitglieder" if (BOARD_AI_CHAT and chat_cog) else "deaktiviert"))
    core.append(_inat_tile())
    core.append(_sheet_tile())
    core.append(await _ai_cost_tile(bot))

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


# ── Alarm-DMs an den Owner ────────────────────────────────────────────────────
_ALERT_AFTER_MIN = 10      # erst melden, wenn ein Vorfall so lange offen ist
_ALERT_GAP_H = 3           # je Prüfung höchstens eine neue Meldung in diesem Zeitraum
_ALERT_ICON = {"warn": "🟡", "down": "🔴"}


def _parse_utc(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _dur(sec: float) -> str:
    m = int(sec // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} Tagen"


def _status_link(key: str) -> str:
    from urllib.parse import quote as _q
    return f"{BOARD_PUBLIC_URL}/status/check/{_q(key, safe='')}" if BOARD_PUBLIC_URL else ""


async def _owner_dm(bot, text: str) -> bool:
    """DM an BOARD_OWNER_ID. False, wenn (noch) nicht zustellbar → nächste Minute erneut."""
    if not BOARD_OWNER_ID or bot is None or not bot.is_ready():
        return False
    try:
        user = bot.get_user(BOARD_OWNER_ID) or await bot.fetch_user(BOARD_OWNER_ID)
        await user.send(text[:1900])
        return True
    except discord.Forbidden:
        logger.warning("🔔 Alarm-DM blockiert (DMs geschlossen?)")
        return True          # nicht endlos wiederholen
    except Exception as e:
        logger.warning("🔔 Alarm-DM fehlgeschlagen: %s", e)
        return False


async def _process_alerts(bot) -> None:
    """Meldet länger offene Vorfälle per DM, eskaliert gelb→rot und meldet Entwarnung."""
    if not BOARD_OWNER_ID:
        return
    now = datetime.now(timezone.utc)
    rows = await board_query(
        "SELECT i.id, i.check_key, i.state, i.detail, i.started_at, a.notified, a.state_sent "
        "FROM board_incidents i LEFT JOIN board_alerts a ON a.incident_id = i.id "
        "WHERE i.ended_at IS NULL AND i.started_at <= datetime('now', ?)",
        (f"-{_ALERT_AFTER_MIN} minutes",))
    for r in rows:
        key, state = r["check_key"], r["state"]
        since = _parse_utc(r["started_at"])
        dur = _dur((now - since).total_seconds()) if since else "?"
        link = _status_link(key)
        if r["notified"] is None:
            recent = await board_one(
                "SELECT 1 FROM board_alerts WHERE check_key=? AND notified=1 "
                "AND sent_at >= datetime('now', ?)", (key, f"-{_ALERT_GAP_H} hours"))
            if recent:
                await board_exec("INSERT OR IGNORE INTO board_alerts (incident_id, check_key, notified, "
                                 "state_sent) VALUES (?,?,0,?)", (r["id"], key, state))
                continue
            text = (f"{_ALERT_ICON.get(state, '⚠️')} **{key}** ist seit {dur} "
                    f"{'rot' if state == 'down' else 'gelb'}\n{r['detail'] or ''}"
                    + (f"\n{link}" if link else ""))
            if await _owner_dm(bot, text):
                await board_exec("INSERT OR IGNORE INTO board_alerts (incident_id, check_key, notified, "
                                 "state_sent) VALUES (?,?,1,?)", (r["id"], key, state))
        elif r["notified"] == 1 and r["state_sent"] == "warn" and state == "down":
            text = f"🔴 **{key}** ist jetzt rot (seit {dur} gestört)\n{r['detail'] or ''}" + (
                f"\n{link}" if link else "")
            if await _owner_dm(bot, text):
                await board_exec("UPDATE board_alerts SET state_sent='down' WHERE incident_id=?", (r["id"],))
    done = await board_query(
        "SELECT i.id, i.check_key, i.started_at, i.ended_at FROM board_incidents i "
        "JOIN board_alerts a ON a.incident_id = i.id "
        "WHERE a.notified = 1 AND a.recovered_sent = 0 AND i.ended_at IS NOT NULL")
    for r in done:
        a, b = _parse_utc(r["started_at"]), _parse_utc(r["ended_at"])
        dur = _dur((b - a).total_seconds()) if a and b else "?"
        if await _owner_dm(bot, f"✅ **{r['check_key']}** wieder OK nach {dur}"):
            await board_exec("UPDATE board_alerts SET recovered_sent=1 WHERE incident_id=?", (r["id"],))


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
        _shops_lbl2 = translate(lang, "lbl_shops")
        out["av_hardest"] = {"title": translate(lang, "av_hardest_title"), "axis": rate_axis,
                             "labels": [f"{s} ({sh} {_shops_lbl2})" for s, r, sh, of in av["hardest"]],
                             "values": [r for s, r, sh, of in av["hardest"]]}

    # ── Block 6: Datenqualität ──────────────────────────────────────────────
    q = data.get("quality")
    if q:
        out["dq_shop_uncanon"] = {"title": translate(lang, "dq_shop_uncanon_title"),
                                  "axis": translate(lang, "dq_shop_uncanon_axis"),
                                  "labels": [s for s, _ in q["shop_uncanon"]],
                                  "values": [n for _, n in q["shop_uncanon"]]}
        out["dq_shop_adjusted"] = {"title": translate(lang, "dq_shop_adjusted_title"),
                                   "axis": translate(lang, "dq_shop_adjusted_axis"),
                                   "labels": [f"{s} ({ac})" for s, r, ac, cn in q["shop_adjusted"]],
                                   "values": [r for s, r, ac, cn in q["shop_adjusted"]]}
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


async def h_partner_page(req):
    """„Partner werden“ – Inhalt aus legal/partner(.example).html."""
    lang = pick_lang(req)
    body, is_example = _legal_content("partner")
    return _render(req, "legal", title=translate(lang, "nav_partner"),
                   heading=translate(lang, "nav_partner"), body=body, is_example=is_example,
                   show_contact=False)


async def h_gewinnspiel(req):
    """Teilnahmebedingungen für Gewinnspiele – Inhalt aus legal/gewinnspiel(.example).html."""
    lang = pick_lang(req)
    body, is_example = _legal_content("gewinnspiel")
    return _render(req, "legal", title=translate(lang, "nav_giveaway"),
                   heading=translate(lang, "nav_giveaway"), body=body, is_example=is_example,
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


def _stats_asset_v() -> str:
    """Cache-Busting für stats.js: Bot-Version + Änderungszeit (sonst bis zu 24 h alter Stand)."""
    try:
        return f"{VERSION}.{int((STATIC_DIR / 'stats.js').stat().st_mtime)}"
    except OSError:
        return VERSION


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
    usage = None
    try:
        from utils import usage_stats
        usage = await usage_stats.collect(req.app["bot"])
    except Exception as e:
        logger.warning("📈 Nutzungsstatistik nicht verfügbar: %s", e)
    resp = _render(req, "stats", title=translate(lang, "nav_stats"), data=data, l10n=l10n, usage=usage,
                   ts_available=bool(ts and ts.get("available")), ts_range=range_key, ver=_stats_asset_v())
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Artensteckbrief & Händlerseiten ───────────────────────────────────────────
def _eur_fmt(v) -> str:
    return (f"{v:.2f} €").replace(".", ",") if isinstance(v, (int, float)) else "–"


def _aam_member(req) -> str | None:
    """Discord-ID, wenn eingeloggt UND aktuell Mitglied des AAM-Servers, sonst None."""
    uid = _is_member(req)
    return uid if (uid and _member_name(req.app, uid)) else None


def _login_url(req) -> str:
    if not (MAP_ENABLED and BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return ""
    return "/map/login?" + urlencode({"next": req.path, "lang": pick_lang(req)})


async def h_art_index(req):
    from utils import directory as dirx
    lang = pick_lang(req)
    q = (req.query.get("q") or "").strip()[:80]
    await ensure_rates()
    try:
        idx = await asyncio.to_thread(dirx.index)
    except Exception as e:
        logger.warning("🐜 Arten-Index fehlgeschlagen: %s", e)
        idx = {"species": {}}
    if q:
        hit = await asyncio.to_thread(dirx.find_species, q)
        if hit:
            raise web.HTTPFound(f"/art/{dirx.slug(hit['name'])}?lang={lang}")
    ql = q.lower()
    items = sorted(((sp["name"], len(sp["shops"]), sum(1 for o in sp["offers"] if o["in_stock"]))
                    for sp in idx["species"].values() if not ql or ql in sp["key"]),
                   key=lambda x: x[0].lower())
    groups: dict = {}
    for name, n, inst in items:                            # nach Gattung gruppiert (eingeklappt)
        note = translate(lang, "dir_shops_n", n=n) + (
            " · " + translate(lang, "dir_instock_n", n=inst) if inst else "")
        groups.setdefault(name.split()[0], []).append({"name": name, "note": note, "instock": inst > 0})
    fnd = await _finder(req, idx, lang)
    resp = _render(req, "art_index", title=translate(lang, "dir_art_index_h"), items=items, q=q,
                   groups=sorted(groups.items(), key=lambda g: g[0].lower()),
                   total=len(idx["species"]), fnd=fnd, flash=_konto_flash(req))
    if fnd["uid"]:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ── Arten-Finder (/art?g=…&pmin=…&pmax=…&land=…&stock=1) & Stern (= Arten-Beobachtung) ──
_BACK_RE = re.compile(r"^/art(?:/[A-Za-z0-9_.%\-]{1,120})?(?:\?[A-Za-z0-9_.%&=+\-]{0,300})?$")


def _konto_flash(req) -> str:
    m = req.query.get("m", "")
    return translate(pick_lang(req), "konto_m_" + m, n=req.query.get("n", "")) \
        if m and ("konto_m_" + m) in _BI18N_T else ""


async def _my_watches(bot, uid) -> set[str]:
    rows = await execute_db(bot, "SELECT species FROM user_species_watch WHERE user_id=?", (str(uid),), fetch=True) or []
    return {str(r["species"]).lower() for r in rows}


async def _star_ctx(req, name: str) -> dict:
    from utils.availability import normalize_species_name
    uid = _aam_member(req)
    back = req.path
    if not uid:
        return {"uid": None, "login": _login_url(req), "back": back}
    w = await _my_watches(req.app["bot"], uid)
    genus = name.split()[0]
    return {"uid": uid, "csrf": _member_csrf(uid), "back": back,
            "watched": normalize_species_name(name).lower() in w,
            "genus_watched": normalize_species_name(genus).lower() in w}


def _fnum(raw):
    try:
        v = float(str(raw).replace(",", ".").strip())
        return v if 0 <= v < 100000 else None
    except (TypeError, ValueError):
        return None


async def _finder(req, idx: dict, lang: str) -> dict:
    """Filtert den Arten-Index nach Gattung, Preis (EUR), Shop-Land/CH-Lieferung und Lagerstand."""
    from utils import directory as dirx
    from utils.availability import normalize_species_name
    q = req.query
    g = (q.get("g") or "").strip()[:60]
    land = (q.get("land") or "").strip().lower()[:3]
    stock = q.get("stock") == "1"
    pmin_raw, pmax_raw = (q.get("pmin") or "").strip()[:10], (q.get("pmax") or "").strip()[:10]
    pmin, pmax = _fnum(pmin_raw), _fnum(pmax_raw)
    shops = idx.get("shops", {})
    genera = sorted({sp["genus"] for sp in idx["species"].values()}, key=str.lower)
    countries = sorted({sh.get("country") for sh in shops.values() if sh.get("country")})
    out = {"active": bool(g or land or stock or pmin is not None or pmax is not None),
           "g": g, "land": land, "stock": stock, "pmin_raw": pmin_raw, "pmax_raw": pmax_raw,
           "genera": genera, "countries": [(c, f"{country_name(lang, c)} ({c.upper()})") for c in countries],
           "rows": [], "more": 0, "uid": None}
    uid = _aam_member(req)
    if uid:
        out.update(uid=uid, csrf=_member_csrf(uid), back=req.path_qs if _BACK_RE.match(req.path_qs) else "/art")
    if not out["active"]:
        return out
    ch_ids = set()
    if land == "chd":
        ch_ids = {str(r["shop_id"]) for r in await execute_db(req.app["bot"],
                  "SELECT shop_id FROM ch_delivery_shops", fetch=True) or []}
    watched = await _my_watches(req.app["bot"], uid) if uid else set()

    def shop_ok(dom):
        if not land:
            return True
        sh = shops.get(dom) or {}
        if land == "chd":
            return sh.get("country") == "ch" or sh.get("id") in ch_ids
        return sh.get("country") == land

    rows = []
    for sp in idx["species"].values():
        if g and sp["genus"].lower() != g.lower():
            continue
        offs = [o for o in sp["offers"] if (not stock or o["in_stock"]) and shop_ok(o["domain"])
                and (pmin is None or (o["price"] is not None and o["price"] >= pmin))
                and (pmax is None or (o["price"] is not None and o["price"] <= pmax))]
        if not offs:
            continue
        prices = [o["price"] for o in offs if o["price"] is not None]
        nq = {"lang": lang, "nf": sp["name"]}
        if land == "chd":
            nq["ns"] = "1"
        elif land:
            nq["nr"] = land
        rows.append({"name": sp["name"], "shops": len({o["domain"] for o in offs}),
                     "instock": sum(1 for o in offs if o["in_stock"]),
                     "min": min(prices) if prices else None,
                     "price": (translate(lang, "fnd_from") + " " + _eur_fmt(min(prices))) if prices else "–",
                     "watched": normalize_species_name(sp["name"]).lower() in watched,
                     "nq": urlencode(nq)})
    rows.sort(key=lambda r: (r["min"] is None, r["min"] or 0, r["name"].lower()))
    out["more"] = max(0, len(rows) - 300)
    out["rows"] = rows[:300]
    return out


async def h_inat(req):
    """Öffentliche Artenliste der iNat-Statistik (nur Summen, keine Namen)."""
    from utils import inat as I
    lang = pick_lang(req)
    d = await I.community_species(req.app["bot"])
    groups: dict = {}
    for x in d["taxa"]:
        note = translate(lang, "inat_row_note", n=x["obs"], y=x["first"]) if x["first"] else ""
        groups.setdefault(x["name"].split()[0], []).append({"name": x["name"], "note": note, "instock": False})
    return _render(req, "inat_list", title=translate(lang, "inat_page_h"), d=d,
                   groups=sorted(groups.items(), key=lambda g: g[0].lower()))


async def h_art_card(req):
    """Teilbare Artenkarte als PNG (Download-Button auf der Artseite)."""
    from utils import directory as dirx, species_card, season as SEA
    lang = pick_lang(req)
    name = dirx.unslug(req.match_info.get("name", ""))[:120]
    if not _rate("card:" + _hmac("card", _ip(req)), 20, 60):
        raise web.HTTPTooManyRequests()
    await ensure_rates()
    sp = await asyncio.to_thread(dirx.find_species, name)
    if not sp:
        raise web.HTTPNotFound()
    trend = await asyncio.to_thread(dirx.price_trend, sp, 12)
    prof = await SEA.profile(req.app["bot"], sp["key"])
    url = (BOARD_PUBLIC_URL.split("://", 1)[-1] + f"/art/{dirx.slug(sp['name'])}") if BOARD_PUBLIC_URL else ""
    png = await asyncio.to_thread(species_card.render, sp, trend, lang, prof,
                                  SEA.MONTHS_SHORT.get(lang, SEA.MONTHS_SHORT["de"]), url)
    fn = dirx.slug(sp["name"]) + ".png"
    return web.Response(body=png, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600",
                                 "Content-Disposition": f'inline; filename="{fn}"'})


async def h_art(req):
    from utils import directory as dirx
    lang = pick_lang(req)
    name = dirx.unslug(req.match_info.get("name", ""))[:120]
    await ensure_rates()
    sp = await asyncio.to_thread(dirx.find_species, name)
    if not sp:
        canon = await asyncio.to_thread(dirx.canonical_name, name)
        from utils import species_profile
        resp = _render(req, "art_missing", title=name, art_name=canon or name, known=bool(canon),
                       ext=species_profile.links(canon or name))
        resp.set_status(404)
        return resp
    if dirx.unslug(sp["name"]) != name:                       # Synonym/Schreibweise → kanonische URL
        raise web.HTTPFound(f"/art/{dirx.slug(sp['name'])}?lang={lang}")
    trend = await asyncio.to_thread(dirx.price_trend, sp, 12)
    offers = sorted(sp["offers"], key=lambda o: (not o["in_stock"], o["price"] is None, o["price"] or 0))
    instock = [o for o in offers if o["in_stock"]]
    fetched = shop_stats._fmt_ts(dirx.index().get("fetched_at"))
    from utils import species_profile
    try:
        prof = species_profile.view(await species_profile.get(sp["name"]), lang)
    except Exception as e:
        logger.info("🐜 Steckbrief %s nicht verfügbar: %s", sp["name"], e)
        prof = {"wiki": None, "gbif": None}
    gb = prof.get("gbif") or {}
    gbif_top = [country_name(lang, c) for c, _ in (gb.get("countries") or [])[:5] if c]
    gbif_count = f"{gb.get('count', 0):,}".replace(",", ".") if lang == "de" else f"{gb.get('count', 0):,}"
    from utils import price_signal as PS, season as SEA
    from utils.localization import l10n
    sig = await asyncio.to_thread(PS.signals, [o["pid"] for o in instock])
    seas = None
    sprof = await SEA.profile(req.app["bot"], sp["key"])
    if sprof:
        seas = {"h": l10n.get("season_h", lang), "text": SEA.describe(sprof, lang),
                "months": sprof["months"], "names": SEA.MONTHS.get(lang, SEA.MONTHS["de"]),
                "short": SEA.MONTHS_SHORT.get(lang, SEA.MONTHS_SHORT["de"])}
    star = await _star_ctx(req, sp["name"])
    swarm = await SWB.art_box(req.app["bot"], sp["name"], lang)
    resp = _render(req, "art", title=sp["name"], sp=sp, offers=offers, instock=instock, star=star,
                   swarm=swarm, sw_short=[m[:3] for m in _SW_MONTHS.get(lang, _SW_MONTHS["de"])],
                   flash=_konto_flash(req),
                   trend=trend, rare=len(sp["shops"]) == 1, eur=_eur_fmt, price=dirx.fmt_price,
                   fetched=fetched, season=seas,
                   sig=sig, sig_any=any(sig.get(o["pid"]) for o in instock), sig_emoji=PS.EMOJI,
                   sig_txt={k: l10n.get(f"price_sig_{k}", lang) for k in PS.EMOJI},
                   sig_legend=l10n.get("price_sig_legend", lang),
                   prof=prof, ext=species_profile.links(sp["name"]),
                   gbif_top=gbif_top, gbif_count=gbif_count)
    if star["uid"]:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


async def h_shops(req):
    from utils import directory as dirx
    lang = pick_lang(req)
    await ensure_rates()
    reviews = await asyncio.to_thread(dirx.loaded_reviews)
    shops = await asyncio.to_thread(dirx.all_shops, reviews)
    partners = {dirx.domain_of(p["url"]) for p in _load_partners()}
    items = []
    for dom, sh in shops.items():
        rv = reviews.get(dom) or {}
        if not sh["offers_count"] and not rv:
            continue
        items.append({"name": sh["name"], "domain": dom, "avg": rv.get("avg"), "n": rv.get("count", 0),
                      "species": sh["species_count"], "instock": sh["instock_count"],
                      "partner": dom in partners})
    items.sort(key=lambda x: x["name"].lower())
    return _render(req, "shops", title=translate(lang, "dir_shops_h"), items=items, filter_js=_FILTER_JS)


async def h_shop(req):
    from utils import directory as dirx
    from utils.sheet import warn_emoji
    lang = pick_lang(req)
    raw = req.match_info.get("domain", "")[:120]
    await ensure_rates()
    reviews = await asyncio.to_thread(dirx.loaded_reviews)
    sh = await asyncio.to_thread(dirx.find_shop, raw, reviews)
    if not sh:
        raise web.HTTPNotFound(text=translate(lang, "dir_shop_404"))
    dom = sh["domain"]
    if raw != dom:
        raise web.HTTPFound(f"/shop/{dom}?lang={lang}")
    rv = reviews.get(dom)
    member = _aam_member(req)
    ctx = dict(sh=sh, rv=rv, member=bool(member), partner=dirx.partner_of(dom),
               delivery=await dirx.delivery_of(req.app["bot"], dom),
               codes=await dirx.active_codes(req.app["bot"], dom),
               country=country_name(lang, sh["country"]) if sh.get("country") else "",
               login_url="" if member else _login_url(req), filter_js=_FILTER_JS,
               warns=[], years_labels=[], years_avg=[], years_n=[], dist_labels=[], dist_n=[])
    if member:
        ctx["warns"] = [dict(w, emoji=warn_emoji(w.get("level"))) for w in dirx.warnings_of(dom)]
        if rv:
            ctx["years_labels"] = [str(y) for y in rv["years"]]
            ctx["years_avg"] = [a for _, a in rv["years"].values()]
            ctx["years_n"] = [n for n, _ in rv["years"].values()]
            ctx["dist_labels"] = [str(k) for k in range(1, 11)]
            ctx["dist_n"] = [rv["dist"].get(k, 0) for k in range(1, 11)]
    instock_sp = {o["species"].lower() for o in sh["offers"] if o["in_stock"]}
    # Preis-Ampel je Art: günstigstes lagerndes Angebot dieses Shops gegen seine eigene Historie
    from utils import price_signal as PS
    from utils.localization import l10n
    sig = await asyncio.to_thread(PS.signals, [o["pid"] for o in sh["offers"] if o["in_stock"]])
    best: dict = {}
    for o in sh["offers"]:
        if o["in_stock"] and o["price"] is not None:
            k = o["species"].lower()
            if k not in best or o["price"] < best[k]["price"]:
                best[k] = o
    gmap: dict = {}
    any_sig = False
    for n in sh["species"]:
        st = n.lower() in instock_sp
        mark = PS.EMOJI.get(sig.get(best[n.lower()]["pid"]), "") if n.lower() in best else ""
        any_sig = any_sig or bool(mark)
        gmap.setdefault(n.split()[0], []).append(
            {"name": n, "instock": st,
             "note": (translate(lang, "dir_instock") + (" " + mark if mark else "")) if st else ""})
    ctx["sig_legend"] = l10n.get("price_sig_legend", lang) if any_sig else ""
    ctx["groups"] = sorted(gmap.items(), key=lambda g: g[0].lower())
    resp = _render(req, "shop", title=sh["name"], **ctx)
    if member:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


_YR_CACHE: dict = {}


async def h_rueckblick(req):
    """Öffentlicher Jahresrückblick (nur Summen). 30-min-Cache je Jahr."""
    from utils import year_review as YR
    from utils import directory as dirx
    from utils.localization import l10n
    lang = pick_lang(req)
    ry = YR.review_year()
    fy = await YR.first_year(req.app["bot"])
    years = list(range(ry, fy - 1, -1))
    raw_year = req.match_info.get("year")
    if not YR.is_open() and not raw_year:
        # Außerhalb 1. Nov bis 31. Jan: Hinweisseite mit Ameisen-Fakten (Quellen verlinkt).
        # Abgeschlossene Jahre bleiben über die Kacheln abrufbar.
        from utils import ant_facts
        facts = ant_facts.all_for(lang)
        start = ant_facts.pick(lang)["index"]
        resp = _render(req, "rueckblick_wait", title=translate(lang, "yr_wait_h"),
                       facts=facts, start=start, opens=YR.next_open().strftime("%d.%m.%Y"),
                       ry=YR.next_open().year, years=years)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    year = int(raw_year or ry)
    if year < fy or year > ry:
        raise web.HTTPFound(f"/rueckblick?lang={lang}" if not YR.is_open()
                            else f"/rueckblick/{ry}?lang={lang}")
    hit = _YR_CACHE.get(year)
    if hit and time.time() - hit[0] < 1800:
        d = hit[1]
    else:
        d = await YR.community(req.app["bot"], year)
        _YR_CACHE[year] = (time.time(), d)
    names = {dom: sh["name"] for dom, sh in (await asyncio.to_thread(dirx.all_shops)).items()}
    # Persönlicher Teil: nur für eingeloggte AAM-Mitglieder, nie gecacht, nur eigene Daten.
    me = None
    uid = _aam_member(req)
    if uid:
        me = await YR.personal(req.app["bot"], uid, year)
        me["name"] = _member_name(req.app, uid) or ""
        me["has"] = bool(me["commands"] or me["achievements"] or me["reviews"]
                         or me["notifications"] or me["watches"] or me["tracked"]
                         or me.get("inat", {}).get("obs") or me.get("swarm"))
        try:
            from utils.achievements import ACHIEVEMENTS
            emo = {a.id: a.emoji for a in ACHIEVEMENTS}
        except Exception:
            emo = {}
        me["ach"] = [(emo.get(a, "🏅"), l10n.get(f"ach_{a}_t", lang)) for a in me["achievements"]]
        for k in ("first", "reviews_first"):
            try:
                me[k] = datetime.fromisoformat(me[k]).strftime("%d.%m.%Y") if me[k] else None
            except ValueError:
                me[k] = None
    tp = lambda key, **kw: l10n.get(key, lang, **kw)
    resp = _render(req, "rueckblick", title=translate(lang, "yr_page_h", y=year), d=d, names=names,
                   me=me, tp=tp, login_url="" if uid else _login_url(req),
                   years=years)
    if uid:
        resp.headers["Cache-Control"] = "private, no-store"   # persönliche Daten nie zwischenspeichern
    return resp


# ── KI-Chat im Board (schwebendes Fenster, nur eingeloggte AAM-Mitglieder) ──────
_WEB_CHAT_CHANNEL = -1          # Kennung für Web-Verläufe in ai_chat_history


def _chat_models(bot, uid: str, lang: str) -> tuple[list, str]:
    import config as cfg
    from cogs.ai_chat import AI_MODELS, _resolve_model_meta
    from utils.ai_chat import get_user_model
    from utils.localization import l10n
    cog = bot.cogs.get("AiChatCog")
    avail = getattr(cog, "_available_models", None)
    shown = [m for m in AI_MODELS if not avail or m["id"] in avail] or AI_MODELS
    pre = get_user_model(int(uid)) or cfg.AI_CHAT_MODEL
    pre = _resolve_model_meta(pre)["id"]
    if pre not in {m["id"] for m in shown}:
        pre = shown[0]["id"]
    out = [{"id": m["id"], "label": m["label"], "emoji": m["emoji"],
            "tier": l10n.get(m["tier_key"], lang), "desc": l10n.get(m["desc_key"], lang),
            "rec": m["id"] == cfg.AI_CHAT_RECOMMENDED_MODEL} for m in shown]
    return out, pre


async def h_chat_models(req):
    uid = _chat_uid(req)
    if not uid:
        return web.json_response({"error": "login"}, status=403)
    models, pre = await asyncio.to_thread(_chat_models, req.app["bot"], uid, pick_lang(req))
    return web.json_response({"models": models, "preselect": pre},
                             headers={"Cache-Control": "private, no-store"})


def _owned_conv(uid: str, conv) -> int | None:
    """Nur eigene, nicht abgelaufene Web-Verläufe fortsetzen."""
    from utils.ai_chat import _db
    try:
        cid = int(conv)
    except (TypeError, ValueError):
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _db() as con:
        row = con.execute("SELECT 1 FROM ai_chat_history WHERE message_id=? AND user_id=? "
                          "AND channel_id=? AND expires_at > ?",
                          (cid, int(uid), _WEB_CHAT_CHANNEL, now)).fetchone()
    return cid if row else None


async def h_chat_send(req):
    import config as cfg
    from utils.ai_chat import chat, save_conversation, set_user_model
    from utils.localization import l10n
    lang = pick_lang(req)
    uid = _chat_uid(req)
    if not uid:
        return web.json_response({"answer": translate(lang, "chat_login")}, status=403)
    if not hmac.compare_digest(req.headers.get("X-CSRF", ""), _member_csrf(uid)):
        return web.json_response({"answer": translate(lang, "chat_error")}, status=403)
    if not (_rate("aichat-m:" + uid, BOARD_AI_CHAT_PER_MIN, 60)
            and _rate("aichat-h:" + uid, BOARD_AI_CHAT_PER_HOUR, 3600)):
        return web.json_response({"answer": translate(lang, "chat_rate")}, status=429)
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"answer": translate(lang, "chat_error")}, status=400)
    msg = str(body.get("msg") or "").strip()
    if not msg:
        return web.json_response({"answer": l10n.get("ai_err_empty", lang)})
    if len(msg) > cfg.AI_CHAT_MAX_INPUT_CHARS:
        return web.json_response({"answer": l10n.get(
            "ai_msg_too_long", lang, length=f"{len(msg):,}", max=f"{cfg.AI_CHAT_MAX_INPUT_CHARS:,}")})
    models, pre = await asyncio.to_thread(_chat_models, req.app["bot"], uid, lang)
    prev = await asyncio.to_thread(_owned_conv, uid, body.get("conv"))
    model = body.get("model") if body.get("model") in {m["id"] for m in models} else pre
    if prev:                                       # Fortsetzung: Modell des Verlaufs
        from utils.ai_chat import load_conversation_model
        model = load_conversation_model(prev) or model
    try:
        set_user_model(int(uid), model)
    except Exception:
        pass
    result = await chat(user_id=int(uid), user_message=msg, prev_bot_message_id=prev,
                        channel_id=_WEB_CHAT_CHANNEL, user_lang=lang, model=model)
    used = result.get("model") or model
    out = {"answer": result.get("answer") or translate(lang, "chat_error"), "conv": None}
    if result.get("budget_exceeded"):
        from cogs.ai_chat import _model_price_overview
        out["answer"] += "\n\n" + _model_price_overview(lang) + "\n\n" + l10n.get("ai_budget_retry_hint", lang)
    else:
        from cogs.ai_chat import _model_footer
        out["footer"] = _model_footer(used, lang) + (f" · 💰 ${result['cost']:.5f}" if result.get("cost") else "")
    if result.get("ok") and result.get("history"):
        new_id = -secrets.randbits(52) - 1        # negativ: kollidiert nie mit Discord-IDs
        await asyncio.to_thread(save_conversation, new_id, int(uid), _WEB_CHAT_CHANNEL,
                                result["history"], used)
        out["conv"] = str(new_id)
    logger.info("🤖 Web-KI-Chat: Nutzer %s, Modell %s, $%.5f", uid, used, result.get("cost") or 0)
    return web.json_response(out, headers={"Cache-Control": "private, no-store"})


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
                   current=current, incidents=incidents, csrf=_csrf_token(req))


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
def _admin_login_url(lang: str) -> str:
    if not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return ""
    return "/map/login?" + urlencode({"next": "/admin", "lang": lang})


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
        m = max(int((STATIC_DIR / f).stat().st_mtime) for f in ("map.js", "map_stats.js", "leaflet.js", "map_cities.json")
                if (STATIC_DIR / f).is_file())
    except ValueError:
        m = 0
    return f"{VERSION}.{m}"


# Texte für die Statistik-Box (map_stats.js), Platzhalter {n}/{km}/… füllt das Front-End.
_MAP_ST_KEYS = ('map_st_title', 'map_st_loading', 'map_st_error', 'map_st_privacy', 'map_st_keepers', 'map_st_countries', 'map_st_regions', 'map_st_events', 'map_st_intl', 'map_st_top', 'map_st_growth', 'map_st_growth_note', 'map_st_ctry', 'map_st_evtypes', 'map_st_next', 'map_st_none', 'map_st_login', 'map_st_members', 'map_st_tags', 'map_st_tags_note', 'map_st_near', 'map_st_near_note', 'map_st_near_none', 'map_st_within', 'map_st_band', 'map_st_contact', 'map_st_contact_all', 'map_st_contact_reg', 'map_st_topev', 'map_st_myev', 'map_st_myev_none', 'map_st_empty', 'map_st_empty_note', 'map_st_empty_none')


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
                   v=_map_asset_v(), member=bool(_aam_member(req)), max_zoom=MAP_MAX_ZOOM,
                   ics_url=ics_url, webcal_url=webcal_url, tag_groups=tag_groups,
                   st_labels={k: translate(lang, k) for k in _MAP_ST_KEYS},
                   partners=_load_partners(),
                   invite_url=(DISCORD_INVITE_URL if DISCORD_INVITE_URL.startswith("https://") else ""),
                   member_csrf=(_member_csrf(_aam_member(req)) if _aam_member(req) else ""))


async def h_map_login(req):
    lang = pick_lang(req)
    if not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
        return _render(req, "map", title=translate(lang, "map_h"),
                       flash=translate(lang, "map_oauth_unconfigured"),
                       v=_map_asset_v(), member=False, max_zoom=MAP_MAX_ZOOM)
    state = secrets.token_urlsafe(24)
    nxt = _login_next(req.query.get("next", ""))
    params = urlencode({
        "client_id": BOARD_OAUTH_CLIENT_ID, "response_type": "code",
        "scope": "identify", "redirect_uri": BOARD_OAUTH_REDIRECT_URI,
        "state": state, "prompt": "none",
    })
    resp = web.HTTPFound(f"{_DISCORD_AUTH}?{params}")
    # State signiert im Cookie ablegen (CSRF-Schutz), kurzlebig.
    resp.set_cookie(_OAUTH_STATE_COOKIE, f"{state}.{_hmac('oauthstate', state)}",
                    max_age=600, httponly=True, samesite="Lax")
    if nxt:
        resp.set_cookie(_LOGIN_NEXT_COOKIE, nxt, max_age=600, httponly=True, samesite="Lax")
    else:
        resp.del_cookie(_LOGIN_NEXT_COOKIE)
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
    nxt = _login_next(req.cookies.get(_LOGIN_NEXT_COOKIE, ""))
    resp = (web.HTTPFound(_safe_local_redirect(f"{nxt}?lang={lang}", "/map"))
            if nxt else _map_lang_redirect("/map", lang))
    resp.del_cookie(_OAUTH_STATE_COOKIE)
    resp.del_cookie(_LOGIN_NEXT_COOKIE)
    if uid in _admin_ids():                    # Owner/Admin: zusätzlich Admin-Sitzung (7 Tage)
        aexp = int(time.time()) + _ADMIN_TTL
        resp.set_cookie(_ADMIN_COOKIE, f"{uid}.{aexp}.{_admin_sign(uid, aexp)}",
                        max_age=_ADMIN_TTL, httponly=True, samesite="Lax")
        logger.info("🔑 Admin-Login über Discord: %s", uid)
    elif nxt == "/admin":
        resp = web.HTTPFound(f"/admin/login?denied=1&lang={lang}")
        resp.del_cookie(_OAUTH_STATE_COOKIE)
        resp.del_cookie(_LOGIN_NEXT_COOKIE)
    resp.set_cookie(_MEMBER_COOKIE, f"{uid}.{exp}.{_member_sign(uid, exp)}",
                    max_age=_MEMBER_TTL, httponly=True, samesite="Lax")
    raise resp


async def h_map_logout(req):
    resp = _map_lang_redirect("/map", pick_lang(req))
    resp.del_cookie(_MEMBER_COOKIE)
    raise resp


async def _map_extras_for(app, uids=None):
    """„Auch aktiv in“-Regionen: {user_id: [(Land, Regionscode, Name), …]} (nur Mitglieder-Ansichten)."""
    rows = await execute_db(app["bot"],
        "SELECT user_id, country, region_code FROM map_entry_regions", fetch=True) or []
    allr = geo.all_regions()
    out: dict = {}
    for r in rows:
        if uids is not None and r["user_id"] not in uids:
            continue
        name = allr.get(r["country"], {}).get(r["region_code"])
        if name:
            out.setdefault(r["user_id"], []).append((r["country"], r["region_code"], name))
    return out


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


# Datenschutz: Öffentlich (ohne Login) werden kleine Zahlen nicht exakt gezeigt, damit man
# in dünn besiedelten Gegenden niemanden zurückverfolgen kann. 1–2 -> "< 3".
MAP_PUBLIC_MIN = 3


def _mask_entry(e: dict) -> dict:
    """Öffentliche Ausgabe: count < MAP_PUBLIC_MIN -> count=1 + few=True (gleiche Farbe/Größe)."""
    if 0 < e.get("count", 0) < MAP_PUBLIC_MIN:
        e["count"], e["few"] = 1, True
    return e


def _mask_num(n: int, member: bool):
    """Zahl für die Anzeige: Mitglieder exakt, öffentlich 1–2 als "< 3"."""
    return n if member or n == 0 or n >= MAP_PUBLIC_MIN else f"< {MAP_PUBLIC_MIN}"


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
    member = bool(_aam_member(req))
    if member:
        # „Auch aktiv in“ (nur Mitglieder): zusätzlich aktive Halter je Region. Zählt NICHT
        # in count/Färbung (Wohnort bleibt maßgeblich, keine Doppelzählung).
        vis = {r["user_id"] for r in (await execute_db(bot,
            "SELECT user_id FROM map_entries WHERE show_entry=1", fetch=True) or [])}
        for uid, lst in (await _map_extras_for(req.app, vis)).items():
            if not _member_name(req.app, uid):
                continue
            for c, rc, rn in lst:
                e = by_state.setdefault(f"{c}:{rc}", {"country": c, "region_code": rc,
                                                      "region_name": rn, "count": 0})
                e["also"] = e.get("also", 0) + 1
    states = list(by_state.values())
    countries = [{"country": k, "count": v} for k, v in sorted(by_country.items())]
    if not member:                                 # öffentlich: kleine Zahlen maskieren
        for e in states + plz_out + countries:
            _mask_entry(e)
    return web.json_response({
        "bundesland": states,
        "plz": plz_out,
        "countries": countries,
    }, headers={"Cache-Control": "no-store", "Vary": "Cookie"})


async def h_map_pins(req):
    """NUR eingeloggte Mitglieder: gefuzzte DACH-Pins (kleine Menge)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    if not _aam_member(req):
        raise web.HTTPForbidden(text="login required")
    bot = req.app["bot"]
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, first_name, show_name, contact_ok, coarse, "
        "lat_fuzzed, lon_fuzzed FROM map_entries WHERE show_entry=1 "
        "AND lat_fuzzed IS NOT NULL LIMIT 2000", fetch=True) or []
    tags = await _map_tags_for(req.app, {r["user_id"] for r in rows})
    extras = await _map_extras_for(req.app, {r["user_id"] for r in rows})
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
            "also": [n for _c, _k, n in extras.get(r["user_id"], [])],   # „Auch aktiv in“
            "tags": map_tags.labels(tags.get(r["user_id"], []), lang),
            "tag_codes": [c for c in tags.get(r["user_id"], []) if map_tags.is_valid(c)],
        })
    return web.json_response({"pins": out})


async def h_map_list(req):
    """Mitglieder: Einzel-Einträge (DACH + International). Öffentlich: nur Länder-Zahlen."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app["bot"]
    member = bool(_aam_member(req))
    if not member:
        # Öffentlich: nur Länder-Zahlen (alle Opt-in-Einträge inkl. U18 anonym).
        rows = await execute_db(bot,
            "SELECT country, COUNT(*) AS n FROM map_entries GROUP BY country",
            fetch=True) or []
        return web.json_response({"member": False,
            "counts": [{"country": r["country"],
                        "country_name": country_name(pick_lang(req), r["country"]),
                        "count": _mask_num(r["n"], False)} for r in rows]},
            headers={"Cache-Control": "no-store", "Vary": "Cookie"})
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, first_name, show_name, contact_ok "
        "FROM map_entries WHERE show_entry=1 LIMIT 5000", fetch=True) or []
    tags = await _map_tags_for(req.app, {r["user_id"] for r in rows})
    extras = await _map_extras_for(req.app, {r["user_id"] for r in rows})
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
            "also": [n for _c, _k, n in extras.get(r["user_id"], [])],   # „Auch aktiv in“
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


def _km(lat1, lon1, lat2, lon2) -> float:
    """Luftlinie in km (Haversine)."""
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


async def h_map_stats(req):
    """Statistiken zur Karte. ÖFFENTLICH: nur Summen (kleine Zahlen maskiert).
    MITGLIEDER zusätzlich: Tags, Umkreis, Kontakt, Teilnahme, Regionen ohne Halter.
    Unter-18-Einträge (show_entry=0) zählen nur in den Gesamtsummen mit."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app["bot"]
    lang = pick_lang(req)
    me = _aam_member(req)
    member = bool(me)
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, lat_fuzzed, lon_fuzzed, "
        "show_entry, contact_ok, consent_at FROM map_entries", fetch=True) or []

    # ── Kennzahlen ──
    by_country: dict = {}
    by_state: dict = {}
    for r in rows:
        c = r["country"] or "??"
        by_country[c] = by_country.get(c, 0) + 1
        if c in _DACH and r["region_code"]:
            rc, rn = geo.canon_region(c, r["region_code"], r["region_name"] or "")
            if c == "li":
                rc, rn = "LI", "Liechtenstein"
            k = (c, rc)
            e = by_state.setdefault(k, {"country": c, "code": rc, "name": rn, "count": 0})
            e["count"] += 1
    evs = await _approved_events(req.app)
    total = len(rows)
    out = {
        "member": member,
        "min": MAP_PUBLIC_MIN,
        "totals": {"keepers": _mask_num(total, member),
                   "countries": len([c for c in by_country if c != "??"]),
                   "regions": len(by_state), "events": len(evs)},
    }

    # ── Top-Regionen (5) ──
    top = sorted(by_state.values(), key=lambda e: (-e["count"], e["name"]))[:5]
    out["top_regions"] = [{"name": e["name"], "country": e["country"],
                           "count": _mask_num(e["count"], member),
                           "n": e["count"] if (member or e["count"] >= MAP_PUBLIC_MIN) else 0}
                          for e in top]

    # ── Wachstum: aktuelle Einträge nach Eintragsmonat (letzte 12 Monate) ──
    now = datetime.now(timezone.utc)
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    per = {k: 0 for k in months}
    for r in rows:
        k = str(r["consent_at"] or "")[:7]
        if k in per:
            per[k] += 1
    out["growth"] = [{"month": k, "count": per[k]} for k in months]

    # ── Ländervergleich DACH (+ International) ──
    comp = []
    for c in _DACH:
        n = by_country.get(c, 0)
        show = member or n == 0 or n >= MAP_PUBLIC_MIN
        comp.append({"country": c, "name": country_name(lang, c),
                     "count": _mask_num(n, member),
                     "share": round(100 * n / total) if (total and show) else None})
    intl = sum(v for k, v in by_country.items() if k not in _DACH)
    show = member or intl == 0 or intl >= MAP_PUBLIC_MIN
    comp.append({"country": "intl", "name": "International",
                 "count": _mask_num(intl, member),
                 "share": round(100 * intl / total) if (total and show) else None})
    out["countries"] = comp

    # ── Termine: kommende je Art + nächster Termin ──
    by_type: dict = {}
    for _n, r in evs:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    out["events_by_type"] = [{"type": k, "count": v}
                             for k, v in sorted(by_type.items(), key=lambda t: -t[1])]
    if evs:
        nxt, r = evs[0]
        out["next_event"] = {"id": r["id"], "title": r["title"], "type": r["type"],
                             "next": nxt.isoformat(), "all_day": bool(r["all_day"]),
                             "venue": r["venue"] or ""}

    if not member:
        return web.json_response(out, headers={"Cache-Control": "no-store", "Vary": "Cookie"})

    # ════ Ab hier nur Mitglieder ════
    # Sichtbare Einträge (18+, show_entry=1) von aktuellen Server-Mitgliedern.
    vis = [r for r in rows if r["show_entry"] and _member_name(req.app, r["user_id"])]
    nvis = len(vis)
    out["visible"] = nvis

    # Beliebteste Tags (Anteil der sichtbaren Einträge)
    tagrows = await execute_db(bot, "SELECT user_id, tag_code FROM map_entry_tags",
                               fetch=True) or []
    vis_ids = {r["user_id"] for r in vis}
    tcount: dict = {}
    for t in tagrows:
        if t["user_id"] in vis_ids and map_tags.is_valid(t["tag_code"]):
            tcount[t["tag_code"]] = tcount.get(t["tag_code"], 0) + 1
    out["tags"] = [{"code": c, "label": map_tags.label(c, lang), "count": n,
                    "pct": round(100 * n / nvis) if nvis else 0}
                   for c, n in sorted(tcount.items(), key=lambda t: -t[1])[:10]]

    # Kontaktbereitschaft (gesamt + eigene Region)
    own = next((r for r in rows if str(r["user_id"]) == str(me)), None)
    contact_total = sum(1 for r in vis if r["contact_ok"])
    out["contact"] = {"count": contact_total,
                      "pct": round(100 * contact_total / nvis) if nvis else 0}
    if own and own["country"] in _DACH and own["region_code"]:
        orc, orn = geo.canon_region(own["country"], own["region_code"], own["region_name"] or "")
        same = [r for r in vis if r["country"] == own["country"] and str(r["user_id"]) != str(me)
                and geo.canon_region(r["country"], r["region_code"] or "", "")[0] == orc]
        out["contact"]["region"] = {"name": "Liechtenstein" if own["country"] == "li" else orn,
                                    "keepers": len(same),
                                    "count": sum(1 for r in same if r["contact_ok"])}

    # In deiner Nähe (Luftlinie zwischen den verschobenen Pins, ohne dich selbst).
    # Entfernungsringe statt kumuliert: jeder Halter zählt genau einmal (0–25, 25–50, 50–100 km).
    if own and own["lat_fuzzed"] is not None and own["lon_fuzzed"] is not None:
        bands = [(0, 25), (25, 50), (50, 100)]
        near = [0] * len(bands)
        for r in vis:
            if str(r["user_id"]) == str(me) or r["lat_fuzzed"] is None:
                continue
            d = _km(own["lat_fuzzed"], own["lon_fuzzed"], r["lat_fuzzed"], r["lon_fuzzed"])
            for i, (a, b) in enumerate(bands):
                if (d > a or a == 0) and d <= b:
                    near[i] += 1
                    break
        out["nearby"] = [{"from": a, "to": b, "count": n} for (a, b), n in zip(bands, near)]
    else:
        out["nearby"] = None                        # kein eigener Eintrag -> Hinweis /map_join

    # Teilnahme: beliebteste kommende Termine + eigene Zusagen
    rsvp = await _rsvp_info(req.app, [(r["id"], _occ_date(n)) for n, r in evs], me)
    ranked = []
    mine = []
    for n, r in evs:
        info = rsvp[(r["id"], _occ_date(n))]
        if info["count"]:
            ranked.append({"id": r["id"], "title": r["title"], "next": n.isoformat(),
                           "all_day": bool(r["all_day"]), "count": info["count"]})
        if info["going"]:
            mine.append({"id": r["id"], "title": r["title"], "next": n.isoformat(),
                         "all_day": bool(r["all_day"])})
    ranked.sort(key=lambda e: (-e["count"], e["next"]))
    out["top_events"] = ranked[:5]
    out["my_events"] = mine

    # Weiße Flecken: DACH-Regionen ohne Halter
    have = set(by_state.keys())
    empty = []
    for c, table in geo.all_regions().items():
        for code, name in table.items():
            if (c, code) not in have:
                empty.append({"country": c, "name": name})
    out["empty_regions"] = sorted(empty, key=lambda e: (_DACH.index(e["country"]), e["name"]))
    return web.json_response(out, headers={"Cache-Control": "no-store", "Vary": "Cookie"})


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


# ══ Hilfeseite (/hilfe) ═══════════════════════════════════════════════════════
HILFE = """{% extends "base" %}{% block body %}
<style>.hp-cmd{padding:7px 0;border-bottom:1px solid #161b22} .hp-ex{color:#8b949e;font-size:13px;margin-top:2px}
.hp-ex code,.hp-cmd code{background:#161b22;border:1px solid #30363d;border-radius:4px;padding:0 5px}
.hp-card{background:#0f141a;border:1px solid #21262d;border-radius:10px;padding:12px 14px}
.hp-card h4{margin:0 0 6px} .hp-card p{margin:0 0 6px;font-size:14px}
.hp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
details.hp-faq{border-bottom:1px solid #21262d;padding:8px 0} details.hp-faq summary{cursor:pointer;font-weight:600}</style>
<h2 style="margin-bottom:6px">❓ {{ t('hp_h') }}</h2>
<p class=muted style="margin-top:0">{{ t('hp_intro') }}</p>
<input id=hpq type=search placeholder="{{ t('hp_search') }}" autocomplete=off style="max-width:420px">
<nav class=secnav style="margin-top:10px"><a href="#befehle">{{ t('hp_s_cmds') }}</a><a href="#board">{{ t('hp_s_board') }}</a><a href="#faq">{{ t('hp_s_faq') }}</a>{% if admin_groups %}<a href="#admin">{{ t('hp_s_admin') }}</a>{% endif %}</nav>
<section class=statsec id=befehle><h3>💬 {{ t('hp_s_cmds') }}</h3>
<p class=muted style="margin-top:0">{{ t('hp_cmds_note') }}</p>
{% for head, items in groups %}<div class=hp-grp><h4 style="margin:14px 0 4px">{{ head }}</h4>
{% for it in items %}<div class=hp-cmd data-q="{{ it.q }}">{{ it.html }}{% if it.ex %}<div class=hp-ex>{{ t('hp_example') }}: <code>{{ it.ex }}</code></div>{% endif %}</div>{% endfor %}</div>{% endfor %}
</section>
<section class=statsec id=board><h3>🌐 {{ t('hp_s_board') }}</h3><div class=hp-grid>
{% for k, link in board %}<div class=hp-card data-q="{{ (t('hp_b_' ~ k ~ '_t') ~ ' ' ~ t('hp_b_' ~ k ~ '_d'))|lower }}"><h4>{{ t('hp_b_' ~ k ~ '_t') }}</h4><p>{{ t('hp_b_' ~ k ~ '_d') }}</p>{% if link %}<a href="{{ link }}{{ qs() }}">{{ t('hp_open') }} →</a>{% endif %}</div>{% endfor %}
</div></section>
<section class=statsec id=faq><h3>🙋 {{ t('hp_s_faq') }}</h3>
{% for k in faq %}<details class=hp-faq data-q="{{ (t('hp_f_' ~ k ~ '_q') ~ ' ' ~ t('hp_f_' ~ k ~ '_a'))|lower }}"><summary>{{ t('hp_f_' ~ k ~ '_q') }}</summary><p style="margin:6px 0 0">{{ t('hp_f_' ~ k ~ '_a') }}</p></details>{% endfor %}
</section>
{% if admin_groups %}<section class=statsec id=admin><h3>🔒 {{ t('hp_s_admin') }}</h3>
{% for head, items in admin_groups %}<div class=hp-grp><h4 style="margin:14px 0 4px">{{ head }}</h4>{% for it in items %}<div class=hp-cmd data-q="{{ it.q }}">{{ it.html }}</div>{% endfor %}</div>{% endfor %}
</section>{% endif %}
<script>(function(){var q=document.getElementById("hpq");if(!q)return;
function f(){var v=q.value.trim().toLowerCase();document.querySelectorAll("[data-q]").forEach(function(e){
 var ok=!v||e.getAttribute("data-q").indexOf(v)>=0;e.style.display=ok?"":"none";if(ok&&v&&e.tagName==="DETAILS")e.open=true;});
 document.querySelectorAll(".hp-grp").forEach(function(g){var any=[].some.call(g.querySelectorAll("[data-q]"),function(e){return e.style.display!=="none";});g.style.display=any?"":"none";});}
q.addEventListener("input",f);})();</script>
{% endblock %}"""
ENV.loader.mapping["hilfe"] = HILFE

_HP_EXAMPLES = {
    "help_notification": "/notification species:Messor barbarus regions:de,at",
    "help_delete": "/delete_notifications ids:12,15",
    "help_offer_alert": "/offer_alert add schlagwort:Lasius niger, Formicarium",
    "help_digest": "/digest action:aktivieren",
    "help_sells": "/sells species:Lasius niger country:de",
    "help_offers": "/offers shop:antstore.net",
    "help_art": "/art name:Camponotus nicobarensis",
    "help_shop": "/shop name:antstore.net",
    "help_bewerten": "/bewerten shop:antstore.net",
    "help_track_price": "/track_price species:Lasius niger",
    "help_set_target": "/set_target mode:zusätzlich target_price:24.90",
    "help_codes": "/codes show_expired:True",
    "help_inat": "/inat_verknuepfen login:mein_inat_name",
    "help_rueckblick": "/rueckblick jahr:2026",
    "help_usersetting": "/usersetting language language:de",
}
_HP_BOARD = [("finder", "/art"), ("star", "/art"), ("card", "/art"), ("shops", "/shops"),
             ("konto", "/konto"), ("map", "/map"), ("swarm", "/schwarmflug"), ("yearreview", "/rueckblick"), ("inat", "/inat"),
             ("stats", "/stats"), ("chat", ""), ("intros", ""), ("board", "/")]
_HP_FAQ = ["nodm", "channel", "data", "ampel", "source", "review", "quiet", "intro"]
_MD_B = re.compile(r"\*\*(.+?)\*\*")
_MD_C = re.compile(r"`([^`]+)`")
_MD_I = re.compile(r"(?<![\w*])\*([^*]+)\*(?![\w*])")


def _md_html(text: str):
    """Discord-Markdown der Hilfetexte (fett, kursiv, Code) → sicheres HTML."""
    from markupsafe import Markup, escape
    h = str(escape(text))
    h = _MD_C.sub(r"<code>\1</code>", h)
    h = _MD_B.sub(r"<b>\1</b>", h)
    h = _MD_I.sub(r"<i>\1</i>", h)
    return Markup(h)


def _hp_groups(groups, lang, examples=True):
    from utils.localization import l10n
    out = []
    for head_key, keys in groups:
        head = re.sub(r"\*\*", "", l10n.get(head_key, lang))
        items = []
        for k in keys:
            txt = l10n.get(k, lang)
            if txt == k:
                continue
            items.append({"html": _md_html(txt), "q": re.sub(r"[*`_]", "", txt).lower(),
                          "ex": _HP_EXAMPLES.get(k, "") if examples else ""})
        if items:
            out.append((head, items))
    return out


async def h_hilfe(req):
    lang = pick_lang(req)
    try:
        from cogs.stats import StatsCog
        from config import AI_CHAT_PUBLIC
        groups = _hp_groups(StatsCog._HELP_USER_GROUPS, lang)
        if AI_CHAT_PUBLIC:
            groups += _hp_groups([("help_grp_ai", StatsCog._HELP_AI_KEYS)], lang)
        admin_groups = _hp_groups(StatsCog._HELP_ADMIN_GROUPS, lang, examples=False) if _is_admin(req) else []
    except Exception as e:
        logger.warning("❓ Hilfe: Befehle nicht geladen: %s", e)
        groups, admin_groups = [], []
    resp = _render(req, "hilfe", title=translate(lang, "hp_h"), groups=groups, admin_groups=admin_groups,
                   board=_HP_BOARD, faq=_HP_FAQ)
    if admin_groups:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ══ Anlass-Intros (/intro.json, Darstellung in static/intro.js) ════════════════
_INTRO_COOKIE = "board_intro"
_INTRO_TTL = 365 * 86400


def _intro_asset_v() -> str:
    try:
        return f"{VERSION}.{int((STATIC_DIR / 'intro.js').stat().st_mtime)}"
    except OSError:
        return VERSION


def _member_joined(app, uid):
    """Beitrittsdatum zum AAM-Server (live von Discord, nicht gespeichert)."""
    try:
        g = app["bot"].get_guild(MAP_GUILD_ID) if MAP_GUILD_ID else None
        m = g.get_member(int(uid)) if g else None
        ja = getattr(m, "joined_at", None)
        return ja.astimezone(BERLIN).date() if ja else None
    except Exception:
        return None


def _intro_payload(lang: str, c: dict) -> dict:
    p = c.get("p") or {}
    sc = c["scene"]
    link = c.get("link") or ""
    if link:
        path, _, frag = link.partition("#")
        link = f"{path}?lang={lang}" + (f"#{frag}" if frag else "")
    tk = c.get("tk") or sc                      # eigener Text bei gleicher Szene (z. B. Saisonstart)
    key_s = f"intro_{tk}_s1" if sc in ("botbday", "aam", "join") and p.get("n") == 1 else f"intro_{tk}_s"
    title_k = f"intro_{tk}_t1" if sc in ("botbday", "aam", "join") and p.get("n") == 1 else f"intro_{tk}_t"
    if sc == "ach" and p.get("n", 1) > 1:
        key_s = "intro_ach_s_more"
    return {"show": True, "scene": sc,
            "title": translate(lang, title_k, **p), "sub": translate(lang, key_s, **p),
            "link": link, "link_label": translate(lang, f"intro_{tk}_link") if link else "",
            "skip": translate(lang, "intro_skip"), "p": p}


async def h_intro(req):
    """Welches Intro (wenn überhaupt) gerade dran ist. Höchstens eins pro Tag, jeder Anlass
    einmal je Jahr (Cookie board_intro). Admins können mit ?intro=<szene> jede Szene testen."""
    from utils import intro as IN
    lang = pick_lang(req)
    hdr = {"Cache-Control": "private, no-store"}
    none = web.json_response({"show": False}, headers=hdr)
    today = datetime.now(BERLIN).date()
    forced = (req.query.get("intro") or "")[:20]
    if forced and _is_admin(req):
        c = IN.demo(forced, today)
        return web.json_response(_intro_payload(lang, c) if c else {"show": False}, headers=hdr)
    if not _rate("intro:" + _hmac("intro", _ip(req)), 60, 60):
        return none
    uid = _aam_member(req)
    prefs = ACC.prefs(uid) if uid else None
    if prefs and not prefs["intros"]:
        return none
    tday = today.strftime("%Y%m%d")
    day, seen = IN.parse_cookie(req.cookies.get(_INTRO_COOKIE, ""))
    if day == tday:
        return none                                  # heute schon ein Intro gesehen
    bot = req.app["bot"]
    cands: list[dict] = []
    if uid:
        try:
            pers = await IN.personal(bot, uid, today, _member_joined(req.app, uid), prefs, lang)
        except Exception as e:
            logger.debug("intro personal: %s", e)
            pers = []
        for c in pers:
            if c.get("silent"):
                await ACC.set_prefs(bot, uid, **c["commit"])   # erster Besuch: Ist-Stand merken
        cands += [c for c in pers if not c.get("silent")]
    cands += IN.seasonal(today, SWB.season_info(lang))
    pick = next((c for c in cands if c["key"] not in seen), None)
    if not pick:
        return none
    if uid and pick.get("commit"):
        await ACC.set_prefs(bot, uid, **pick["commit"])
    resp = web.json_response(_intro_payload(lang, pick), headers=hdr)
    resp.set_cookie(_INTRO_COOKIE, IN.build_cookie(tday, seen, pick["key"]), max_age=_INTRO_TTL,
                    httponly=True, samesite="Lax", secure=req.secure)
    return resp


# ── Template „Mein Konto“ ──
KONTO = """{% extends "base" %}{% block body %}
<style>
 .kt td,.kt th{font-size:14px} .kt th{color:#8b949e;font-weight:600;font-size:12px}
 .kform{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin:10px 0}
 .kform label{margin:0 0 3px} .kform>div{min-width:150px;flex:1}
 .kform .chk{display:flex;gap:6px;align-items:center;flex:0 0 auto;min-width:0;color:#c9d1d9;font-size:14px;margin:0}
 .kform .chk input,.kchk input{width:auto}
 .kchk{display:inline-flex;gap:6px;align-items:center;margin:2px 14px 2px 0;color:#c9d1d9;font-size:14px}
 .inl{display:inline} .inl button{margin:1px 0}
 .pill{display:inline-block;font-size:12px;padding:1px 8px;border-radius:20px;border:1px solid #30363d}
 .p-active{color:#3fb950;border-color:#3fb95066} .p-paused{color:#d29922;border-color:#d2992266} .p-done{color:#8b949e}
 .bar{height:6px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:4px} .bar i{display:block;height:100%;background:#238636}
 .achgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}
 .ach{background:#0f141a;border:1px solid #21262d;border-radius:8px;padding:8px 10px} .ach.on{border-color:#3fb95066}
 .ach .n{font-weight:600} .ach .d{color:#8b949e;font-size:12px}
 .hist div{padding:5px 0;border-bottom:1px solid #161b22;font-size:14px} .hist .w{color:#8b949e;font-size:12px;margin-right:8px;white-space:nowrap}
 .codebox{font:600 20px ui-monospace,monospace;background:#0d1117;border:1px dashed #58a6ff;border-radius:8px;padding:8px 14px;display:inline-block;letter-spacing:1px}
 .danger{border-color:#8b2b2b !important}
 .kdanger{display:none !important} .kunlocked .kdanger{display:inline-block !important} .kunlocked .kdanger.kb{display:block !important}
 .kunlocked .klocked{display:none !important}
 .kunlock{display:flex;gap:10px;align-items:flex-start;background:#8b2b2b22;border:1px solid #8b2b2b;border-radius:8px;padding:10px 12px;margin:10px 0 4px;cursor:pointer}
 .kunlock input{width:auto;margin-top:4px} .kunlock b{display:block}
 .kt input,.kt select{padding:4px 6px}
 @media(max-width:700px){.kt .hm{display:none}}
</style>
{% if former %}
<div style="max-width:640px;margin:30px auto;text-align:center"><div style="font-size:50px">👋</div>
<h2 style="margin:6px 0">{{ t('konto_left_h') }}</h2><p class=muted>{{ t('konto_left_text') }}</p>
{% if invite_url %}<div class=chartbox style="text-align:left"><h4>🐜 {{ t('konto_left_join_h') }}</h4><p style="margin:0 0 10px">{{ t('konto_left_join_text') }}</p>
 <a class=btn href="{{ invite_url }}" target=_blank rel="noopener noreferrer">{{ t('konto_left_join_btn') }}</a></div>{% endif %}
<div class="chartbox danger" style="text-align:left"><h4>🗑️ {{ t('konto_left_del_h') }}</h4><p style="margin:0 0 10px">{{ t('konto_left_del_text') }}</p>
 <form method=post action="/konto/loeschen{{ qs() }}" onsubmit="return confirm({{ t('konto_confirm_all')|tojson }})"><input type=hidden name=csrf value="{{ csrf }}"><input type=hidden name=area value=all>
  <span class=kchk><input type=checkbox name=sure value=1 required> {{ t('konto_delete_all_sure') }}</span>
  <p style="margin:10px 0 0"><button class="btn red">{{ t('konto_delete_all') }}</button></p></form>
 <p class=muted style="margin:10px 0 0">{{ t('konto_delete_note', d=log_days) }}</p></div>
<p class=muted><a href="/map/logout{{ qs() }}">{{ t('konto_left_logout') }}</a></p>
</div>
{% elif not uid %}
<div style="max-width:620px;margin:30px auto;text-align:center"><div style="font-size:50px">👤</div>
<h2 style="margin:6px 0">{{ t('konto_h') }}</h2><p class=muted>{{ t('konto_login_text') }}</p>
{% if login_url %}<p><a class=btn href="{{ login_url }}">{{ t('dir_login_btn') }}</a></p>{% else %}<div class=flash>{{ t('konto_login_off') }}</div>{% endif %}
</div>
{% else %}
{% macro cs() %}<input type=hidden name=csrf value="{{ csrf }}">{% endmacro %}
{% macro ul() %}<input type=hidden name=unlock value="" class=kul>{% endmacro %}
{% macro act(a) %}/konto/{{ a }}{{ qs() }}{% endmacro %}
<h2 style="margin-bottom:4px">👤 {{ t('konto_h') }}{% if member_name %} · {{ member_name }}{% endif %}</h2>
<p class=muted style="margin-top:0">🔒 {{ t('konto_intro') }}</p>
<label class=kunlock for=kunlock><input type=checkbox id=kunlock autocomplete=off><span><b>🗑️ {{ t('konto_unlock_t') }}</b><span class=muted>{{ t('konto_unlock_s') }}</span></span></label>
<script>(function(){var c=document.getElementById("kunlock");c.checked=false;function u(){document.body.classList.toggle("kunlocked",c.checked);document.querySelectorAll(".kul").forEach(function(i){i.value=c.checked?"1":"";});}c.addEventListener("change",u);window.addEventListener("pageshow",function(){c.checked=false;u();});u();})();</script>
<nav class=secnav>
 <a href="#uebersicht">{{ t('konto_s_overview') }}</a><a href="#benachrichtigungen">{{ t('konto_s_notif') }}</a><a href="#preise">{{ t('konto_s_prices') }}</a><a href="#einstellungen">{{ t('konto_s_settings') }}</a><a href="#schwarmflug">{{ t('konto_s_swarm') }}</a><a href="#termine">{{ t('konto_s_events') }}</a><a href="#karte">{{ t('konto_s_map') }}</a><a href="#inat">{{ t('konto_s_inat') }}</a><a href="#erfolge">{{ t('konto_s_ach') }}</a><a href="#bewertungen">{{ t('konto_s_reviews') }}</a><a href="#verlauf">{{ t('konto_s_history') }}</a><a href="#daten">{{ t('konto_s_data') }}</a>
</nav>

<section class=statsec id=uebersicht><h3>📋 {{ t('konto_s_overview') }}</h3>
<div class=kpigrid>
 <a class=kpi href="#benachrichtigungen" style="text-decoration:none"><div class=v>{{ n_active }}</div><div class=l>🔔 {{ t('konto_k_notif') }}</div></a>
 <a class=kpi href="#preise" style="text-decoration:none"><div class=v>{{ d.tracked|length }}</div><div class=l>📈 {{ t('konto_k_tracked') }}</div></a>
 <a class=kpi href="#preise" style="text-decoration:none"><div class=v>{{ d.watches|length }}</div><div class=l>🔭 {{ t('konto_k_watches') }}</div></a>
 <a class=kpi href="#einstellungen" style="text-decoration:none"><div class=v>{{ d.keywords|length }}</div><div class=l>🛒 {{ t('konto_k_keywords') }}</div></a>
 <a class=kpi href="#termine" style="text-decoration:none"><div class=v>{{ d.rsvps|length }}</div><div class=l>📅 {{ t('konto_k_events') }}</div></a>
 {% if d.rank %}<a class=kpi href="#erfolge" style="text-decoration:none"><div class=v>{{ d.rank.emoji }} {{ d.rank.n }}/{{ d.rank.total }}</div><div class=l>🏅 {{ d.rank.name }}</div></a>{% endif %}
 <a class=kpi href="#inat" style="text-decoration:none"><div class=v>{{ d.inat_year.species }}</div><div class=l>🔬 {{ t('konto_k_inat', y=year) }}</div></a>
 <a class=kpi href="#bewertungen" style="text-decoration:none"><div class=v>{{ n_reviews_year }}</div><div class=l>⭐ {{ t('konto_k_reviews', y=year) }}</div></a>
</div>
{% if d.ai %}<div class=chartbox><h4>🤖 {{ t('konto_ai_h') }}</h4>
 <p style="margin:2px 0">{{ t('konto_ai_text', p=d.ai.pct) }}</p>
 <div class=bar style="max-width:420px"><i style="width:{{ d.ai.pct }}%;{% if d.ai.pct >= 90 %}background:#d29922{% endif %}"></i></div>
 <p class=muted style="margin:6px 0 0">{{ t('konto_ai_reset', r=d.ai.reset) }}{% if d.ai.global_pct >= 90 %} · {{ t('konto_ai_global', p=d.ai.global_pct) }}{% endif %}</p>
</div>{% endif %}
{% if quiet_now %}<div class=flash style="margin-top:12px">🌙 {{ t('konto_quiet_now', s=d.prefs.quiet_start, e=d.prefs.quiet_end) }}</div>{% endif %}
</section>

<section class=statsec id=benachrichtigungen><h3>🔔 {{ t('konto_s_notif') }}</h3>
<p class=muted style="margin-top:0">{{ t('konto_notif_intro') }}</p>
{% if d.notifications %}<table class=kt><tr><th>{{ t('konto_col_species') }}</th><th>{{ t('konto_col_regions') }}</th><th>{{ t('konto_col_status') }}</th><th class=hm>{{ t('konto_col_since') }}</th><th></th></tr>
{% for n in d.notifications %}<tr>
 <td><i>{{ n.species }}</i>{% if n.excluded_species %}<div class=muted>{{ t('konto_excl') }}: {{ n.excluded_species }}</div>{% endif %}</td>
 <td>{{ n.regions|replace(',', ', ')|upper }}</td>
 <td><span class="pill {{ 'p-active' if n.status == 'active' else ('p-paused' if n.status == 'paused' else 'p-done') }}">{{ t('konto_st_' ~ (n.status if n.status in sts else 'other')) }}</span>{% if n.notified_at %}<div class=muted>{{ t('konto_notified') }} {{ n.notified_at|dmy }}</div>{% endif %}</td>
 <td class=hm>{{ n.created_at|dmy }}</td>
 <td style="white-space:nowrap">
  <form class=inl method=post action="{{ act('benachrichtigung') }}">{{ cs() }}{{ ul() }}<input type=hidden name=id value="{{ n.id }}">
  {% if n.status == 'active' %}<button class="btn grey small" name=do value=pause>⏸️ {{ t('konto_pause') }}</button>
  {% else %}<button class="btn small" name=do value=resume>▶️ {{ t('konto_resume') }}</button>{% endif %}
  <button class="btn red small kdanger" name=do value=delete onclick="return confirm({{ t('konto_confirm_del')|tojson }})">🗑️</button></form>
 </td></tr>{% endfor %}</table>
{% else %}<p>{{ t('konto_notif_none') }}</p>{% endif %}
<details class=chartbox {% if open_form == 'notif' %}open{% endif %}><summary style="cursor:pointer;font-weight:600">➕ {{ t('konto_notif_new') }}</summary>
<form method=post action="{{ act('benachrichtigung/neu') }}">{{ cs() }}
 <div class=kform>
  <div style="flex:0 0 auto;min-width:0"><label>{{ t('konto_kind') }}</label>
   <span class=kchk><input type=radio name=kind value=species {% if form.kind != 'genus' %}checked{% endif %}> {{ t('konto_kind_species') }}</span>
   <span class=kchk><input type=radio name=kind value=genus {% if form.kind == 'genus' %}checked{% endif %}> {{ t('konto_kind_genus') }}</span></div>
  <div style="flex:2"><label>{{ t('konto_name') }}</label><input name=name list=kspecies required maxlength=80 value="{{ form.name or '' }}" placeholder="Messor barbarus"></div>
  <div style="flex:2"><label>{{ t('konto_exclude') }}</label><input name=exclude maxlength=300 value="{{ form.exclude or '' }}" placeholder="Messor structor, …"></div>
 </div>
 <label>{{ t('konto_regions') }}</label>
 <div>{% for code, label in regions %}<span class=kchk><input type=checkbox name=region value="{{ code }}" {% if code in form.regions %}checked{% endif %}> {{ label }}</span>{% endfor %}</div>
 <div class=kform>
  <span class=chk><input type=checkbox name=swiss value=1 {% if form.swiss %}checked{% endif %}> 🇨🇭 {{ t('konto_swiss') }}</span>
  <span class=chk><input type=checkbox name=force value=1> {{ t('konto_force') }}</span>
  <button class=btn>{{ t('konto_create') }}</button>
 </div>
 <p class=muted style="margin:0">{{ t('konto_notif_hint') }}</p>
</form></details>
</section>

<section class=statsec id=preise><h3>📈 {{ t('konto_s_prices') }}</h3>
<h4 style="margin:4px 0 6px">{{ t('konto_tracked_h') }}</h4>
{% if d.tracked %}<table class=kt><tr><th>{{ t('konto_col_product') }}</th><th class=hm>{{ t('konto_col_shop') }}</th><th>{{ t('konto_col_price') }}</th><th>{{ t('konto_col_target') }}</th><th></th></tr>
{% for p in d.tracked %}<tr>
 <td>{% if p.product_url %}<a href="{{ p.product_url }}" target=_blank rel="noopener noreferrer">{{ p.product_title or p.species }}</a>{% else %}{{ p.product_title or p.species }}{% endif %}{% if p.variant_title %}<div class=muted>{{ p.variant_title }}</div>{% endif %}</td>
 <td class=hm>{{ p.shop_name }}</td>
 <td style="white-space:nowrap">{% if p.last_notified_min is not none %}{{ '%.2f'|format(p.last_notified_min) }}{% if p.last_notified_max and p.last_notified_max != p.last_notified_min %}–{{ '%.2f'|format(p.last_notified_max) }}{% endif %} {{ p.currency_iso }}{% else %}–{% endif %}</td>
 <td><form class=inl method=post action="{{ act('zielpreis') }}">{{ cs() }}<input type=hidden name=pid value="{{ p.product_id }}"><input type=hidden name=vid value="{{ p.variant_id }}">
  <input name=price inputmode=decimal style="width:80px" value="{{ ('%.2f'|format(p.target_price)) if p.target_price is not none else '' }}" placeholder="{{ p.currency_iso }}">
  <select name=mode style="width:auto"><option value=additional {% if p.target_mode != 'replace' %}selected{% endif %}>{{ t('konto_mode_add') }}</option><option value=replace {% if p.target_mode == 'replace' %}selected{% endif %}>{{ t('konto_mode_replace') }}</option></select>
  <button class="btn grey small">🎯</button></form></td>
 <td><form class="inl kdanger" method=post action="{{ act('preis/entfernen') }}">{{ cs() }}{{ ul() }}<input type=hidden name=pid value="{{ p.product_id }}"><input type=hidden name=vid value="{{ p.variant_id }}"><button class="btn red small" onclick="return confirm({{ t('konto_confirm_del')|tojson }})">🗑️</button></form></td>
</tr>{% endfor %}</table>
<p class=muted>{{ t('konto_target_hint') }}</p>
{% else %}<p>{{ t('konto_tracked_none') }}</p>{% endif %}

<div class=chartbox id=produktsuche><h4>➕ {{ t('konto_track_new') }}</h4>
<form method=get action="/konto#produktsuche" class=kform><input type=hidden name=lang value="{{ lang }}">
 <div style="flex:3"><label>{{ t('konto_name') }}</label><input name=ps list=kspecies required maxlength=80 value="{{ ps or '' }}" placeholder="Lasius niger"></div>
 <button class="btn grey">🔎 {{ t('konto_search') }}</button></form>
{% if ps %}{% if ps_results %}
<form method=post action="{{ act('preis/neu') }}">{{ cs() }}<input type=hidden name=species value="{{ ps }}">
<table class=kt><tr><th></th><th>{{ t('konto_col_product') }}</th><th class=hm>{{ t('konto_col_shop') }}</th><th>{{ t('konto_col_price') }}</th></tr>
{% for r in ps_results %}<tr><td><input type=checkbox name=sel value="{{ r.key }}" style="width:auto" {% if r.tracked %}checked disabled{% endif %}></td>
 <td>{{ r.title }}{% if r.tracked %} <span class=muted>({{ t('konto_already') }})</span>{% endif %}</td><td class=hm>{{ r.shop }}</td>
 <td style="white-space:nowrap">{{ r.price }} {% if r.stock %}✅{% else %}❌{% endif %}</td></tr>{% endfor %}</table>
{% if ps_more %}<p class=muted>{{ t('konto_ps_more', n=ps_more) }}</p>{% endif %}
<p><button class=btn>{{ t('konto_track_btn') }}</button> <span class=muted>{{ t('konto_track_variants') }}</span></p></form>
{% else %}<p>{{ t('konto_ps_none', q=ps) }}</p>{% endif %}{% endif %}
</div>

<h4 style="margin:16px 0 6px">{{ t('konto_watches_h') }}</h4>
<p class=muted style="margin-top:0">{{ t('konto_watches_intro') }}</p>
{% if d.watches %}<div>{% for w in d.watches %}<form class=inl method=post action="{{ act('beobachtung/entfernen') }}">{{ cs() }}{{ ul() }}<input type=hidden name=species value="{{ w.species }}"><span class=tag style="font-size:13px;padding:3px 8px"><i>{{ w.species|capitalize }}</i>{% if w.is_genus %} ({{ t('konto_kind_genus') }}){% endif %} <button class="btn red small kdanger" style="padding:0 5px" title="{{ t('konto_remove') }}">✕</button></span></form> {% endfor %}</div>
{% else %}<p>{{ t('konto_watches_none') }}</p>{% endif %}
<form method=post action="{{ act('beobachtung/neu') }}" class=kform>{{ cs() }}
 <div style="flex:3"><label>{{ t('konto_watch_new') }}</label><input name=species list=kspecies required maxlength=80 placeholder="Camponotus"></div>
 <button class="btn grey">🔭 {{ t('konto_watch_btn') }}</button></form>
</section>

<section class=statsec id=einstellungen><h3>⚙️ {{ t('konto_s_settings') }}</h3>
<form method=post action="{{ act('einstellungen') }}">{{ cs() }}
 <div class=kform>
  <div style="flex:0 0 220px"><label>{{ t('konto_lang') }}</label><select name=language>
   <option value="" {% if not d.language %}selected{% endif %}>{{ t('konto_lang_server') }}</option>
   {% for c, n in [('de','Deutsch'),('en','English'),('eo','Esperanto')] %}<option value="{{ c }}" {% if d.language == c %}selected{% endif %}>{{ n }}</option>{% endfor %}</select></div>
 </div>
 <div><span class=kchk><input type=checkbox name=digest value=1 {% if d.digest %}checked{% endif %}> 📰 {{ t('konto_digest') }}</span></div>
 <div><span class=kchk><input type=checkbox name=rsvp value=1 {% if d.prefs.rsvp_reminder %}checked{% endif %}> 📅 {{ t('konto_rsvp') }}</span></div>
 <div><span class=kchk><input type=checkbox name=intros value=1 {% if d.prefs.intros %}checked{% endif %}> 🎬 {{ t('konto_intros') }}</span></div>
 <div class=kform>
  <span class=chk><input type=checkbox name=quiet value=1 {% if d.prefs.quiet_start is not none %}checked{% endif %}> 🌙 {{ t('konto_quiet') }}</span>
  <div style="flex:0 0 110px"><label>{{ t('konto_quiet_from') }}</label><select name=qs>{% for h in range(24) %}<option value="{{ h }}" {% if h == (d.prefs.quiet_start if d.prefs.quiet_start is not none else 22) %}selected{% endif %}>{{ '%02d'|format(h) }}:00</option>{% endfor %}</select></div>
  <div style="flex:0 0 110px"><label>{{ t('konto_quiet_to') }}</label><select name=qe>{% for h in range(24) %}<option value="{{ h }}" {% if h == (d.prefs.quiet_end if d.prefs.quiet_end is not none else 7) %}selected{% endif %}>{{ '%02d'|format(h) }}:00</option>{% endfor %}</select></div>
 </div>
 <p class=muted style="margin:0 0 8px">{{ t('konto_quiet_hint') }}</p>
 <button class=btn>{{ t('konto_save') }}</button>
</form>

<h4 style="margin:18px 0 6px">🚫 {{ t('konto_bl_h') }}</h4>
<p class=muted style="margin-top:0">{{ t('konto_bl_intro') }}</p>
{% if blacklist %}<div>{% for sid, sname in blacklist %}<form class=inl method=post action="{{ act('blacklist/entfernen') }}">{{ cs() }}{{ ul() }}<input type=hidden name=shop value="{{ sid }}"><span class=tag style="font-size:13px;padding:3px 8px">{{ sname }} <button class="btn red small kdanger" style="padding:0 5px" title="{{ t('konto_remove') }}">✕</button></span></form> {% endfor %}</div>{% else %}<p>{{ t('konto_bl_none') }}</p>{% endif %}
<form method=post action="{{ act('blacklist/neu') }}" class=kform>{{ cs() }}
 <div style="flex:3"><label>{{ t('konto_bl_add') }}</label><select name=shop required><option value="">…</option>{% for sid, sname in shops %}<option value="{{ sid }}">{{ sname }}</option>{% endfor %}</select></div>
 <button class="btn grey">{{ t('konto_add') }}</button></form>

<h4 style="margin:18px 0 6px">🛒 {{ t('konto_kw_h') }}</h4>
<p class=muted style="margin-top:0">{{ t('konto_kw_intro', n=kw_max) }}</p>
{% if d.keywords %}<div>{% for k in d.keywords %}<form class=inl method=post action="{{ act('schlagwort/entfernen') }}">{{ cs() }}{{ ul() }}<input type=hidden name=kw value="{{ k.keyword }}"><span class=tag style="font-size:13px;padding:3px 8px">{{ k.keyword_raw or k.keyword }} <button class="btn red small kdanger" style="padding:0 5px" title="{{ t('konto_remove') }}">✕</button></span></form> {% endfor %}</div>{% else %}<p>{{ t('konto_kw_none') }}</p>{% endif %}
{% if kw_enabled %}<form method=post action="{{ act('schlagwort/neu') }}" class=kform>{{ cs() }}
 <div style="flex:3"><label>{{ t('konto_kw_add') }}</label><input name=kw required maxlength=400 placeholder="Lasius niger, Formicarium"></div>
 <button class="btn grey">{{ t('konto_add') }}</button></form>{% endif %}
</section>

<section class=statsec id=schwarmflug><h3>🪽 {{ t('konto_s_swarm') }}</h3>
<p class=muted style="margin-top:0">{{ t('konto_sw_intro') }} <a href="/schwarmflug{{ qs() }}">{{ t('nav_swarm') }} →</a></p>
{% if d.swarm_alerts %}<div>{% for a in d.swarm_alerts %}<form class=inl method=post action="{{ act('schwarm/entfernen') }}">{{ cs() }}{{ ul() }}<input type=hidden name=taxon value="{{ a.taxon }}"><input type=hidden name=country value="{{ a.country }}"><input type=hidden name=region value="{{ a.region }}"><span class=tag style="font-size:13px;padding:3px 8px">{% if a.taxon == '*' %}🎉 {{ t('konto_sw_start') }}{% else %}<i>{{ a.taxon }}</i> · {{ sw_where(a.country, a.region) }}{% endif %} <button class="btn red small kdanger" style="padding:0 5px" title="{{ t('konto_remove') }}">✕</button></span></form> {% endfor %}</div>{% else %}<p>{{ t('konto_sw_none') }}</p>{% endif %}
{% if sw_enabled and not sw_start_on %}<p style="margin:10px 0 0"><form method=post action="{{ act('schwarm/neu') }}" class=inl>{{ cs() }}<input type=hidden name=taxon value="*"><input type=hidden name=where value=""><button class="btn grey small">🎉 {{ t('konto_sw_start_btn') }}</button></form> <span class=muted style="font-size:13px">{{ t('konto_sw_start_d') }}</span></p>{% endif %}
{% if sw_enabled and fc_enabled %}<form method=post action="{{ act('schwarm/prognose') }}" class=kform>{{ cs() }}
 <div style="flex:3"><label>🔮 {{ t('konto_fc_h') }}</label><select name=level>{% for v in ('off', 'rare', 'normal', 'often') %}<option value="{{ v }}" {{ 'selected' if d.swarm_fc == v }}>{{ t('konto_fc_' ~ v) }}</option>{% endfor %}</select></div>
 <button class="btn grey">{{ t('konto_fc_save') }}</button></form>{% endif %}
{% if sw_enabled %}<form method=post action="{{ act('schwarm/neu') }}" class=kform>{{ cs() }}
 <div style="flex:2"><label>{{ t('konto_sw_taxon') }}</label><input name=taxon required maxlength=60 list=kspecies placeholder="Lasius niger, Camponotus"></div>
 <div style="flex:2"><label>{{ t('konto_sw_region') }}</label><select name=where>{% for v, lbl in sw_regions %}<option value="{{ v }}" {{ 'selected' if v==sw_default }}>{{ lbl }}</option>{% endfor %}</select></div>
 <button class="btn grey">{{ t('konto_add') }}</button></form>{% endif %}
{% if d.swarm_mine %}<details><summary class=muted style="cursor:pointer">{{ t('konto_sw_mine', n=d.swarm_mine|length) }}</summary>
<table class=kt>{% for r in d.swarm_mine %}<tr><td>{{ r.flight_date|dmy }}</td><td><i>{{ r.label }}</i></td><td>{{ sw_where(r.country, r.region) if r.region else '' }}</td><td class=muted>{{ r.place }}</td></tr>{% endfor %}</table></details>{% endif %}
</section>

<section class=statsec id=termine><h3>📅 {{ t('konto_s_events') }}</h3>
{% if d.rsvps %}<table class=kt><tr><th>{{ t('konto_col_date') }}</th><th>{{ t('konto_col_event') }}</th><th class=hm>{{ t('konto_col_place') }}</th><th></th></tr>
{% for r in d.rsvps %}<tr><td style="white-space:nowrap">{{ r.occ_date|dmy }}{% if not r.all_day and r.start_at %}<div class=muted>{{ r.start_at[11:16] }}</div>{% endif %}</td>
 <td>{{ r.title }}{% if r.url %} <a href="{{ r.url }}" target=_blank rel="noopener noreferrer">🔗</a>{% endif %}</td>
 <td class=hm>{{ r.venue or '' }}{% if r.country %} ({{ r.country|upper }}){% endif %}</td>
 <td style="white-space:nowrap"><a class="btn grey small" href="/map/events/{{ r.event_id }}.ics">📥</a>
  <form class="inl kdanger" method=post action="{{ act('termin/absagen') }}">{{ cs() }}{{ ul() }}<input type=hidden name=eid value="{{ r.event_id }}"><input type=hidden name=occ value="{{ r.occ_date }}"><button class="btn red small">{{ t('konto_cancel') }}</button></form></td></tr>{% endfor %}</table>
{% else %}<p>{{ t('konto_events_none') }} <a href="/map{{ qs() }}">{{ t('nav_map') }}</a></p>{% endif %}
<div class=chartbox><h4>🗓️ {{ t('konto_ics_h') }}</h4>
 <p class=muted style="margin-top:0">{{ t('konto_ics_intro') }}</p>
 {% if ics_url %}<input readonly value="{{ ics_url }}" onclick="this.select()" style="margin-bottom:8px">
 <a class="btn grey small" href="{{ webcal_url }}">{{ t('konto_ics_subscribe') }}</a>{% endif %}
 <form class=inl method=post action="{{ act('kalender/neu') }}">{{ cs() }}<button class="btn grey small">{{ t('konto_ics_new' if ics_url else 'konto_ics_create') }}</button></form>
</div>
</section>

<section class=statsec id=karte><h3>🗺️ {{ t('konto_s_map') }}</h3>
{% if d.map %}
 <p style="margin:2px 0">{{ t('konto_map_where') }}: <b>{{ map_country }}</b>{% if d.map.region_name %} · {{ d.map.region_name }}{% endif %}{% if d.map.plz_prefix %} · PLZ {{ d.map.plz_prefix }}…{% endif %}</p>
 <p class=muted style="margin:2px 0">{{ t('konto_map_vis_' ~ ('pin' if d.map.show_entry else 'count')) }} · {{ t('konto_map_name_' ~ ('on' if d.map.show_name else 'off')) }} · {{ t('konto_map_contact_' ~ ('on' if d.map.contact_ok else 'off')) }}{% if d.map.last_confirmed_at %} · {{ t('konto_map_confirmed') }} {{ d.map.last_confirmed_at|dmy }}{% endif %}</p>
 <form method=post action="{{ act('karte/tags') }}">{{ cs() }}
 {% for g in tag_groups %}<label>{{ g.label }}</label><div>{% for c, l in g.tags %}<span class=kchk><input type=checkbox name=tag value="{{ c }}" {% if c in d.map_tags %}checked{% endif %}> {{ l }}</span>{% endfor %}</div>{% endfor %}
 <p><button class="btn grey">{{ t('konto_map_tags_save') }}</button></p></form>
 <p class=muted>{{ t('konto_map_change') }}</p>
 <form class="kdanger kb" method=post action="{{ act('karte/loeschen') }}" onsubmit="return confirm({{ t('konto_confirm_map')|tojson }})">{{ cs() }}{{ ul() }}<button class="btn red">🗑️ {{ t('konto_map_delete') }}</button></form>
{% else %}<p>{{ t('konto_map_none') }}</p>{% endif %}
</section>

<section class=statsec id=inat><h3>🔬 {{ t('konto_s_inat') }}</h3>
{% if d.inat %}
 <p>{{ t('konto_inat_linked') }} <a href="https://www.inaturalist.org/people/{{ d.inat.inat_login|urlencode }}" target=_blank rel="noopener noreferrer"><b>{{ d.inat.inat_login }}</b></a></p>
 <div class=kpigrid>
  <div class=kpi><div class=v>{{ d.inat_year.species }}</div><div class=l>{{ t('konto_inat_species_y', y=year) }}</div></div>
  <div class=kpi><div class=v>{{ d.inat_year.obs }}</div><div class=l>{{ t('konto_inat_obs_y', y=year) }}</div></div>
  <div class=kpi><div class=v>{{ d.inat_all.species }}</div><div class=l>{{ t('konto_inat_species_all') }}</div></div>
 </div>
 {% if d.inat_all.taxa %}<details style="margin-top:10px"><summary style="cursor:pointer">{{ t('konto_inat_taxa', n=d.inat_all.taxa|length) }}</summary><p>{% for x in d.inat_all.taxa %}<a href="/art/{{ x|replace(' ','_')|urlencode }}{{ qs() }}"><i>{{ x }}</i></a>{% if not loop.last %}, {% endif %}{% endfor %}</p></details>{% endif %}
 <form method=post action="{{ act('inat/trennen') }}" style="margin-top:10px" onsubmit="return confirm({{ t('konto_confirm_inat')|tojson }})">{{ cs() }}<button class="btn red small">{{ t('konto_inat_unlink') }}</button></form>
{% elif d.inat_pending %}
 <p>{{ t('konto_inat_pending', login=d.inat_pending.inat_login) }}</p>
 <ol><li>{{ t('konto_inat_step1') }} <a href="https://www.inaturalist.org/users/edit" target=_blank rel="noopener noreferrer">inaturalist.org/users/edit</a></li>
 <li>{{ t('konto_inat_step2') }}<br><span class=codebox>{{ d.inat_pending.code }}</span></li><li>{{ t('konto_inat_step3') }}</li></ol>
 <form method=post action="{{ act('inat/pruefen') }}" class=inl>{{ cs() }}<button class=btn>{{ t('konto_inat_check') }}</button></form>
 <form method=post action="{{ act('inat/start') }}" class=kform>{{ cs() }}<div><label>{{ t('konto_inat_other') }}</label><input name=login maxlength=40 required></div><button class="btn grey">{{ t('konto_inat_start') }}</button></form>
{% else %}
 <p>{{ t('konto_inat_intro') }}</p>
 <form method=post action="{{ act('inat/start') }}" class=kform>{{ cs() }}<div><label>{{ t('konto_inat_login') }}</label><input name=login maxlength=40 required placeholder="mein_inat_name"></div><button class=btn>{{ t('konto_inat_start') }}</button></form>
{% endif %}
</section>

<section class=statsec id=erfolge><h3>🏅 {{ t('konto_s_ach') }}</h3>
{% if d.rank %}<p>{{ d.rank.emoji }} <b>{{ d.rank.name }}</b> · {{ t('konto_ach_n', n=d.rank.n, total=d.rank.total) }}{% if d.rank.next %} · {{ t('konto_ach_next', n=d.rank.next - d.rank.n) }}{% endif %}</p>{% endif %}
{% for cat, label in ach_cats %}{% set items = d.ach|selectattr('cat', 'equalto', cat)|list %}{% if items %}
<h4 style="margin:12px 0 6px">{{ label }}</h4><div class=achgrid>
{% for a in items %}{% if a.hidden and not a.unlocked %}<div class=ach><div class=n>🔒 ???</div><div class=d>{{ t('konto_ach_hidden') }}</div></div>
{% else %}<div class="ach {{ 'on' if a.unlocked }}"><div class=n>{{ a.emoji }} {{ a.title }}{% if a.unlocked %} ✅{% endif %}</div><div class=d>{{ a.desc }}</div>
 {% if a.unlocked %}{% if a.date %}<div class=d>{{ t('konto_ach_since') }} {{ a.date|dmy }}</div>{% endif %}{% else %}<div class=bar><i style="width:{{ (100 * a.cur / a.tgt)|round|int if a.tgt else 0 }}%"></i></div><div class=d>{{ a.cur }}/{{ a.tgt }}</div>{% endif %}</div>{% endif %}
{% endfor %}</div>{% endif %}{% endfor %}
</section>

<section class=statsec id=bewertungen><h3>⭐ {{ t('konto_s_reviews') }}</h3>
<p class=muted style="margin-top:0">{{ t('konto_reviews_intro') }}</p>
{% if d.reviews %}<table class=kt><tr><th>{{ t('konto_col_date') }}</th><th>{{ t('konto_col_shop') }}</th><th>{{ t('konto_col_rating') }}</th><th></th></tr>
{% for r in d.reviews %}<tr><td>{{ r.review_date|dmy }}</td><td>{% if r.shop %}<a href="/shop/{{ r.shop|urlencode }}{{ qs() }}">{{ shop_names.get(r.shop, r.shop) }}</a>{% endif %}</td>
 <td>{% if r.rating is not none %}⭐ {{ ('%g'|format(r.rating)) }}{% else %}–{% endif %}</td>
 <td>{% if d.review_link %}<a href="{{ d.review_link }}{{ r.message_id }}" target=_blank rel="noopener noreferrer">{{ t('konto_to_msg') }}</a>{% endif %}</td></tr>{% endfor %}</table>
{% else %}<p>{{ t('konto_reviews_none') }}</p>{% endif %}
</section>

<section class=statsec id=verlauf><h3>📨 {{ t('konto_s_history') }}</h3>
<p class=muted style="margin-top:0">{{ t('konto_history_intro', d=dm_days, n=dm_max) }}</p>
{% if d.history %}<div class=hist>{% for h in d.history %}<div><span class=w>{{ h.sent_at|berlin }}</span>{{ h.preview }}</div>{% endfor %}</div>
{% else %}<p>{{ t('konto_history_none') }}</p>{% endif %}
</section>

<section class=statsec id=daten><h3>🛡️ {{ t('konto_s_data') }}</h3>
<div class=chartbox><h4>📥 {{ t('konto_export_h') }}</h4><p class=muted style="margin-top:0">{{ t('konto_export_intro') }}</p>
 <a class=btn href="/konto/daten.json{{ qs() }}">{{ t('konto_export_btn') }}</a></div>
<div class=chartbox><h4>🔑 {{ t('konto_sessions_h') }}</h4><p class=muted style="margin-top:0">{{ t('konto_sessions_intro') }}</p>
 <form method=post action="{{ act('abmelden') }}" onsubmit="return confirm({{ t('konto_confirm_logout')|tojson }})">{{ cs() }}<button class="btn grey">{{ t('konto_logout_all') }}</button></form></div>
<div class="chartbox klocked" style="border-style:dashed">🔒 {{ t('konto_delete_locked') }} <a href="#kunlock">{{ t('konto_delete_locked_link') }}</a></div>
<div class="chartbox danger kdanger kb"><h4>🗑️ {{ t('konto_delete_h') }}</h4><p class=muted style="margin-top:0">{{ t('konto_delete_intro') }}</p>
 <table class=kt>{% for a in areas %}<tr><td><b>{{ t('konto_area_' ~ a) }}</b><div class=muted>{{ t('konto_area_' ~ a ~ '_d') }}</div></td>
  <td style="white-space:nowrap;text-align:right"><form class=inl method=post action="{{ act('loeschen') }}" onsubmit="return confirm({{ t('konto_confirm_area')|tojson }})">{{ cs() }}{{ ul() }}<input type=hidden name=area value="{{ a }}"><button class="btn red small">{{ t('konto_delete_btn') }}</button></form></td></tr>{% endfor %}</table>
 <form method=post action="{{ act('loeschen') }}" style="margin-top:14px" onsubmit="return confirm({{ t('konto_confirm_all')|tojson }})">{{ cs() }}{{ ul() }}<input type=hidden name=area value=all>
  <span class=kchk><input type=checkbox name=sure value=1 required> {{ t('konto_delete_all_sure') }}</span>
  <button class="btn red">{{ t('konto_delete_all') }}</button></form>
 <p class=muted style="margin-bottom:0">{{ t('konto_delete_note', d=log_days) }}</p>
</div>
</section>
<datalist id=kspecies>{% for s in species_names %}<option value="{{ s }}">{% endfor %}</datalist>
{% endif %}
{% endblock %}"""


# ══ Mein Konto (/konto, nur eingeloggte AAM-Mitglieder) ════════════════════════
ENV.loader.mapping["konto"] = KONTO

# ══ Schwarmflug-Radar (/schwarmflug) ═══════════════════════════════════════════
ENV.loader.mapping["schwarmflug"] = SWB.TEMPLATE
ENV.loader.mapping["swarm_box"] = SWB.ART_BOX
_SW_MONTHS = {"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                     "Oktober", "November", "Dezember"],
              "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                     "October", "November", "December"],
              "eo": ["Januaro", "Februaro", "Marto", "Aprilo", "Majo", "Junio", "Julio", "Aŭgusto", "Septembro",
                     "Oktobro", "Novembro", "Decembro"]}


SWARM_ADMIN = """{% extends "base" %}{% block body %}
<h2 style="margin:0 0 4px">🔒 {{ t('sw_unclear_h') }}</h2>
<p class=muted style="margin:0 0 12px">{{ t('sw_unclear_intro') }} <a href="/schwarmflug{{ qs() }}">← {{ t('sw_h') }}</a></p>
<div class=kpigrid style="margin-bottom:12px">
 <div class=kpi><div class=v>{{ n_dach }}</div><div class=l>{{ t('sw_unclear_k_dach') }}</div></div>
 <div class=kpi><div class=v>{{ n_abroad }}</div><div class=l>{{ t('sw_unclear_k_abroad') }}</div></div>
 <div class=kpi><div class=v>{{ n_home }}</div><div class=l>{{ t('sw_unclear_k_home') }}</div></div>
 <div class=kpi><div class=v>{{ items|length }}</div><div class=l>{{ t('sw_unclear_k_unclear') }}</div></div>
</div>
{% if ok %}<div class=flash>✅ {{ t('sw_fix_saved') }}</div>{% endif %}
<style>.fx{display:flex;gap:4px;flex-wrap:wrap}.fx input,.fx select{width:auto;padding:3px 6px;font-size:12px}.fx select{max-width:170px}.fx input[name=place]{width:150px}.fx input[name=taxa]{width:150px}</style>
<div class=chartbox style="overflow-x:auto"><h4>❓ {{ t('sw_unclear_list') }}</h4><table class=swt style="font-size:13px"><tr><th>{{ t('sw_abroad_when') }}</th><th>{{ t('sw_col_taxon') }}</th><th>{{ t('sw_abroad_place') }}</th><th>{{ t('sw_fix_state') }}</th><th>{{ t('sw_fix_h') }}</th></tr>
{% for r in items %}<tr id="m{{ r.mid }}"><td style="white-space:nowrap">{{ r.flight_date|dmy }}{% if r.link %}<br><a href="{{ r.link }}" target=_blank rel=noopener>Discord ↗</a>{% endif %}</td><td><i>{{ r.labels }}</i></td><td>{{ r.place or '?' }}</td>
<td class=muted style="font-size:12px">{{ t('sw_fix_state_' ~ (r.state or 'open')) }}</td>
<td><form method=post action="/admin/schwarmflug/fix?lang={{ lang }}" class=fx><input type=hidden name=csrf value="{{ csrf }}"><input type=hidden name=mid value="{{ r.mid }}">
<select name=where>{% for v, lbl in regions %}<option value="{{ v }}">{{ lbl }}</option>{% endfor %}</select><input name=place placeholder="{{ t('sw_abroad_place') }}" value="{{ r.place }}"><input name=taxa placeholder="{{ t('sw_fix_taxa_ph') }}"><button class="btn small">{{ t('sw_fix_btn') }}</button></form></td></tr>
{% else %}<tr><td colspan=5 class=muted>{{ t('sw_unclear_none') }}</td></tr>{% endfor %}</table></div>
{% if manual %}<div class=chartbox style="overflow-x:auto"><h4>✍️ {{ t('sw_fix_list') }}</h4><table class=swt style="font-size:13px"><tr><th>{{ t('sw_abroad_when') }}</th><th>{{ t('sw_abroad_place') }}</th><th>{{ t('sw_id_region') }}</th><th>{{ t('sw_col_taxon') }}</th></tr>
{% for o in manual %}<tr><td>{{ o.set_at|berlin }}</td><td>{{ o.place }}</td><td>{{ o.country }}{% if o.region %}-{{ o.region }}{% endif %}</td><td><i>{{ o.taxa }}</i></td></tr>{% endfor %}</table></div>{% endif %}
<div class=chartbox style="overflow-x:auto"><h4>🔤 {{ t('sw_names_h') }}</h4><p class=muted style="font-size:12px;margin-top:0">{{ t('sw_names_intro') }}</p>
<style>.sure{color:#3fb950}.guess{color:#d29922}.nf{display:inline}.nf button{padding:1px 7px;font-size:12px}</style>
{% if names_msg %}<div class=flash>{{ names_msg }}</div>{% endif %}
{% if n_guess %}<form method=post action="/admin/schwarmflug/names?lang={{ lang }}" style="margin:0 0 8px"><input type=hidden name=csrf value="{{ csrf }}"><input type=hidden name=act value="all_ok"><button class="btn small" onclick="return confirm('{{ t('sw_names_all_q', n=n_guess) }}')">✓ {{ t('sw_names_all_btn', n=n_guess) }}</button></form>{% endif %}
<table class=swt style="font-size:13px"><tr><th>{{ t('sw_names_raw') }}</th><th>{{ t('sw_names_now') }}</th><th>{{ t('sw_names_conf') }}</th><th>{{ t('sw_names_how') }}</th><th>{{ t('sw_kpi_reports') }}</th><th></th></tr>
{% for f in fixed %}{% set c = f.taxon_check or 'fixed' %}<tr><td><i>{{ f.raw_label }}</i></td><td><i>{{ f.label }}</i></td>
<td>{% if c in ('synonym', 'filler', 'typo_ok', 'ai', 'manual') %}<span class=sure>● {{ t('sw_names_sure') }}</span>{% elif c in ('typo', 'fixed') %}<span class=guess>● {{ t('sw_names_guess') }}</span>{% else %}<span class=muted>○</span>{% endif %}</td>
<td class=muted>{{ t('sw_names_' ~ c) }}</td><td>{{ f.n }}</td>
<td style="white-space:nowrap">{% if c in ('typo', 'fixed') %}{% for act, sym, tip in (('ok', '✓', 'sw_names_btn_ok'), ('no', '✗', 'sw_names_btn_no')) %}<form method=post action="/admin/schwarmflug/names?lang={{ lang }}" class=nf><input type=hidden name=csrf value="{{ csrf }}"><input type=hidden name=raw value="{{ f.raw_label }}"><input type=hidden name=label value="{{ f.label }}"><input type=hidden name=act value="{{ act }}"><button class="btn small" title="{{ t(tip) }}">{{ sym }}</button></form> {% endfor %}{% elif c in ('unknown', 'unknown_ai', 'rejected') %}<form method=post action="/admin/schwarmflug/names?lang={{ lang }}" class="nf fx"><input type=hidden name=csrf value="{{ csrf }}"><input type=hidden name=raw value="{{ f.raw_label }}"><input type=hidden name=label value="{{ f.label }}"><input type=hidden name=act value="set"><input name=name placeholder="{{ t('sw_names_set_ph') }}" style="width:170px" required><button class="btn small">{{ t('sw_fix_btn') }}</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan=6 class=muted>{{ t('sw_names_none') }}</td></tr>{% endfor %}</table></div>
{% endblock %}"""
ENV.loader.mapping["swarm_admin"] = SWARM_ADMIN


async def h_swarm_admin(req):
    """Admin: Meldungen ohne Land und Region (fließen nicht in die Auswertung ein)."""
    lang = pick_lang(req)
    if not _is_admin(req):
        raise web.HTTPFound(f"/admin/login?lang={lang}")
    from config import SWARM_CHANNEL_ID
    from utils import geo
    bot = req.app["bot"]
    unclear = [r for r in await SWB.S.rows(bot, include_indoor=True, scope="unclear") if not r["indoor"]]
    ch = bot.get_channel(SWARM_CHANNEL_ID) if SWARM_CHANNEL_ID else None
    gid = getattr(getattr(ch, "guild", None), "id", None)
    by: dict = {}
    for r in unclear:
        e = by.setdefault(r["message_id"], {"mid": r["message_id"], "flight_date": r["flight_date"],
                                            "place": r["place"], "labels": [], "state": r["geo_src"] or "",
                                            "link": f"https://discord.com/channels/{gid}/{SWARM_CHANNEL_ID}/{r['message_id']}"
                                            if gid else ""})
        e["labels"].append(r["label"])
    items = sorted(by.values(), key=lambda x: x["flight_date"], reverse=True)
    for e in items:
        e["labels"] = ", ".join(e["labels"])
    n_d = await execute_db(bot, "SELECT COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE "
                                "country IN ('de','at','ch','li')", fetch=True)
    n_a = await execute_db(bot, "SELECT COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE country<>'' AND "
                                "country NOT IN ('de','at','ch','li')", fetch=True)
    fixed = [dict(x) for x in await execute_db(bot,
        "SELECT raw_label, label, taxon_check, COUNT(DISTINCT message_id) AS n FROM swarm_reports "
        "WHERE raw_label IS NOT NULL GROUP BY raw_label, label, taxon_check "
        "ORDER BY taxon_check IN ('typo', 'fixed') DESC, n DESC LIMIT 300", fetch=True) or []]
    home = await execute_db(bot, "SELECT COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE geo_src='home'", fetch=True)
    manual = [dict(x) for x in await execute_db(bot, "SELECT * FROM swarm_override ORDER BY set_at DESC LIMIT 100",
                                                fetch=True) or []]
    resp = _render(req, "swarm_admin", title=translate(lang, "sw_unclear_h"), items=items,
                   n_dach=n_d[0]["n"] if n_d else 0, n_abroad=n_a[0]["n"] if n_a else 0,
                   n_home=home[0]["n"] if home else 0, fixed=fixed, manual=manual, csrf=_csrf_token(req),
                   n_guess=sum(1 for f in fixed if f["taxon_check"] in ("typo", "fixed")),
                   names_msg={"set": translate(lang, "sw_names_set_ok"), "bad": translate(lang, "sw_names_set_bad"),
                              "all": translate(lang, "sw_names_all_ok")}.get(req.query.get("nm", ""), ""),
                   regions=_sw_regions(lang), ok=req.query.get("ok", ""))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


async def h_swarm_status(req):
    """Live-Stand der Datensammlung (alle 10 s vom Kasten abgefragt)."""
    lang = pick_lang(req)
    if not _rate("swst:" + _hmac("swst", _ip(req)), 30, 60):
        raise web.HTTPTooManyRequests()
    prog = await SWB.progress(req.app["bot"], lambda key, **kw: translate(lang, key, **kw))
    return web.json_response(prog, headers={"Cache-Control": "no-store"})


async def h_swarm_fix(req):
    """Admin: Korrektur von Hand speichern (gilt bis zur Bearbeitung in Discord)."""
    lang = pick_lang(req)
    if not _is_admin(req):
        raise web.HTTPForbidden()
    form = await req.post()
    if not _csrf_ok(req, form):
        raise web.HTTPForbidden(text="bad csrf")
    from utils import swarm_resolve as R
    mid = re.sub(r"\D", "", str(form.get("mid") or ""))[:25]
    where = str(form.get("where") or "")
    c, _, r = where.partition("-")
    if not mid or (where and c not in geo.all_regions()):
        raise web.HTTPSeeOther(f"/admin/schwarmflug?lang={lang}")
    s = _admin_session(req)
    await R.set_override(req.app["bot"], mid, str(form.get("place") or "")[:160].strip(), c if where else "",
                         "" if c == "li" else r, str(form.get("taxa") or "")[:200].strip(), s[0] if s else "")
    await R.apply_override(req.app["bot"], mid)
    raise web.HTTPSeeOther(f"/admin/schwarmflug?lang={lang}&ok={mid}#m{mid}")


async def h_swarm_names(req):
    """Admin: vermutete Tippfehler-Korrektur bestätigen (✓) oder verwerfen (✗ → Gattung sp.)."""
    lang = pick_lang(req)
    if not _is_admin(req):
        raise web.HTTPForbidden()
    form = await req.post()
    if not _csrf_ok(req, form):
        raise web.HTTPForbidden(text="bad csrf")
    raw, label, act = (str(form.get(k) or "")[:200] for k in ("raw", "label", "act"))
    bot = req.app["bot"]
    cond = "raw_label=? AND label=? AND taxon_check IN ('typo', 'fixed')"
    back = f"/admin/schwarmflug?lang={lang}"
    if act == "all_ok":
        await execute_db(bot, "UPDATE swarm_reports SET taxon_check='typo_ok' WHERE raw_label IS NOT NULL AND "
                              "taxon_check IN ('typo', 'fixed')", commit=True)
        raise web.HTTPSeeOther(back + "&nm=all")
    if act == "set":
        from utils import species_catalog as SC
        name = re.sub(r"\s+", " ", str(form.get("name") or "")).strip()[:80]
        res = SC.check(name) if name else {"status": "empty"}
        canon = res.get("canonical") if res.get("status") in ("accepted", "synonym") else None
        if not canon:
            raise web.HTTPSeeOther(back + "&nm=bad")
        parts = canon.split()
        g, sp = parts[0], (parts[1].lower() if len(parts) > 1 else None)
        rows = await execute_db(bot, "SELECT id, qualifier FROM swarm_reports WHERE raw_label=? AND label=? AND "
                                     "taxon_check IN ('unknown', 'unknown_ai', 'rejected')", (raw, label),
                                fetch=True) or []
        for r in rows:
            q = "Gruppe" if "Gruppe" in (r["qualifier"] or "") and sp else ("sp." if not sp else "")
            t = {"genus": g, "species": sp, "qualifier": q}
            await execute_db(bot, "UPDATE OR REPLACE swarm_reports SET genus=?, species=?, qualifier=?, label=?, "
                                  "taxon_check='manual' WHERE id=?",
                             (g, sp, q, SWB.S.taxon_label(t), r["id"]), commit=True)
        raise web.HTTPSeeOther(back + "&nm=set")
    if act == "ok":
        await execute_db(bot, f"UPDATE swarm_reports SET taxon_check='typo_ok' WHERE {cond}", (raw, label), commit=True)
    elif act == "no":
        rows = await execute_db(bot, f"SELECT id, genus, qualifier FROM swarm_reports WHERE {cond}", (raw, label),
                                fetch=True) or []
        for r in rows:
            q = "cf." if "cf." in (r["qualifier"] or "") else ""
            t = {"genus": r["genus"], "species": None, "qualifier": q or "sp."}
            await execute_db(bot, "UPDATE OR REPLACE swarm_reports SET species=NULL, qualifier=?, label=?, "
                                  "taxon_check='rejected' WHERE id=?",
                             (t["qualifier"], SWB.S.taxon_label(t), r["id"]), commit=True)
    raise web.HTTPSeeOther(f"/admin/schwarmflug?lang={lang}")


async def h_swarm(req):
    lang = pick_lang(req)
    uid = _aam_member(req)
    try:
        from utils import directory as dirx
        idx = await asyncio.to_thread(dirx.index)
        known = {v["name"].lower(): v["name"] for v in idx.get("species", {}).values() if v.get("name")}
    except Exception:
        dirx, known = None, {}

    def art_slug(name):
        hit = known.get(name.lower())
        return dirx.slug(hit) if hit and dirx else None
    ctx = await SWB.page(req.app["bot"], req.query.get("z", ""), (req.query.get("g") or "").strip()[:40],
                         bool(uid), lang, art_slug, _SW_MONTHS.get(lang, _SW_MONTHS["de"]),
                         lambda key, **kw: translate(lang, key, **kw), q=dict(req.query))
    resp = _render(req, "schwarmflug", title=translate(lang, "sw_h"), login_url=_login_url(req), **ctx)
    resp.headers["Cache-Control"] = "private, no-store" if uid else "public, max-age=300"
    return resp


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

_KONTO_DANGER = {"preis/entfernen", "beobachtung/entfernen", "blacklist/entfernen", "schlagwort/entfernen",
                 "termin/absagen", "karte/loeschen", "loeschen", "schwarm/entfernen"}
_KONTO_STATUSES = ("active", "paused", "completed", "pending_feedback", "expired", "failed")
_KONTO_SPECIES_CACHE: dict = {"at": 0.0, "names": []}
_MD_STRIP = re.compile(r"(\*\*|__|`+)")


def _konto_species_names() -> list[str]:
    """Vorschläge für die Eingabefelder: alle angebotenen Arten + ihre Gattungen (10 min Cache)."""
    if time.time() - _KONTO_SPECIES_CACHE["at"] < 600 and _KONTO_SPECIES_CACHE["names"]:
        return _KONTO_SPECIES_CACHE["names"]
    try:
        from utils import directory as dirx
        names = {v["name"] for v in dirx.index()["species"].values() if v.get("name")}
    except Exception:
        names = set()
    genera = {n.split()[0] for n in names if " " in n}
    out = sorted(genera) + sorted(names)
    _KONTO_SPECIES_CACHE.update(at=time.time(), names=out[:4000])
    return _KONTO_SPECIES_CACHE["names"]


def _ics_personal_url(uid: str, salt: str) -> str:
    base = BOARD_PUBLIC_URL or ""
    return f"{base}/konto/kalender/{uid}/{salt}.ics"


async def _konto_page(req, uid, flash: str = "", form: dict | None = None, open_form: str = "",
                      status: int = 200):
    from utils.availability import load_shop_data, strip_html
    from utils.localization import l10n
    lang = pick_lang(req)
    bot = req.app["bot"]
    d = await ACC.collect(bot, uid, lang)
    shop_data = await load_shop_data(bot)
    shops = sorted(((sid, sd.get("name") or sid) for sid, sd in shop_data.items()), key=lambda x: x[1].lower())
    names = dict(shops)
    countries = sorted({(sd.get("country") or "").lower() for sd in shop_data.values() if sd.get("country")})
    regions = [("eu", "🇪🇺 " + translate(lang, "konto_region_eu"))] + \
              [(c, f"{country_name(lang, c)} ({c.upper()})") for c in countries]
    year = datetime.now(BERLIN).year
    # Produktsuche (GET ?ps=…)
    ps = (req.query.get("ps") or "").strip()[:80]
    ps_results, ps_more = [], 0
    if ps:
        from cogs.price_tracking import _find_products_for_tracking
        have = {(int(p["product_id"]), int(p["variant_id"] or 0)) for p in d["tracked"]}
        found = await _find_products_for_tracking(bot, ps)
        for sid, blk in found.items():
            for p in blk["products"]:
                try:
                    lo, hi = float(p.get("min_price") or 0), float(p.get("max_price") or 0)
                except (TypeError, ValueError):
                    lo = hi = 0.0
                price = "?" if p.get("_no_price") else (f"{lo:.2f}" + (f"–{hi:.2f}" if hi and hi != lo else "")
                                                        + f" {p.get('currency_iso') or ''}")
                ps_results.append({"key": f"{sid}|{p.get('id')}", "shop": blk["shop_info"].get("name", sid),
                                   "title": strip_html(p.get("title") or p.get("species") or "")[:140],
                                   "price": price, "stock": bool(p.get("in_stock")),
                                   "tracked": (int(p.get("id") or 0), 0) in have})
        ps_results.sort(key=lambda r: (not r["stock"], r["shop"].lower(), r["title"].lower()))
        if len(ps_results) > 80:
            ps_more = len(ps_results) - 80
            ps_results = ps_results[:80]
    salt = ACC.prefs(uid)["ics_salt"]
    ics_url = _ics_personal_url(uid, salt) if salt else ""
    tag_groups = [{"label": (g["label"].get(lang) or g["label"].get("de")),
                   "tags": [(c, (l.get(lang) or l.get("de") or c)) for c, l in g["tags"]]}
                  for g in map_tags.TAG_GROUPS]
    try:
        from cogs.offer_alerts import MAX_KEYWORDS
        from config import OFFER_CHANNEL_ID
    except Exception:
        MAX_KEYWORDS, OFFER_CHANNEL_ID = 25, 0
    from config import COMMAND_LOG_RETENTION_DAYS
    ach_cats = [(c, _MD_STRIP.sub("", l10n.get("ach_cat_" + c, lang))) for c in
                ("avail", "price", "community", "usage", "hidden")]
    form = form or {}
    form.setdefault("regions", [])
    resp = _render(req, "konto", title=translate(lang, "konto_h"), flash=flash, uid=uid, d=d,
                   member_name=_member_name(req.app, uid) or "", csrf=_member_csrf(uid),
                   year=year, sts=_KONTO_STATUSES,
                   n_active=sum(1 for n in d["notifications"] if n["status"] == "active"),
                   n_reviews_year=sum(1 for r in d["reviews"] if str(r["review_date"]).startswith(str(year))),
                   quiet_now=ACC.is_quiet(uid), regions=regions, form=form, open_form=open_form,
                   shops=[s for s in shops if s[0] not in set(d["blacklist"])],
                   blacklist=[(sid, names.get(sid, sid)) for sid in d["blacklist"]],
                   shop_names={dom: sh["name"] for dom, sh in (await asyncio.to_thread(_all_shops_safe)).items()},
                   kw_max=MAX_KEYWORDS, kw_enabled=bool(OFFER_CHANNEL_ID),
                   ps=ps, ps_results=ps_results, ps_more=ps_more,
                   ics_url=ics_url, webcal_url=("webcal://" + ics_url.split("://", 1)[-1]) if ics_url else "",
                   map_country=(country_name(lang, d["map"]["country"]) if d["map"] and d["map"].get("country") else ""),
                   tag_groups=tag_groups, ach_cats=ach_cats,
                   dm_days=ACC.DM_LOG_DAYS, dm_max=ACC.DM_LOG_MAX,
                   areas=list(ACC.AREAS), log_days=COMMAND_LOG_RETENTION_DAYS,
                   species_names=_konto_species_names(),
                   sw_enabled=bool(_swarm_on()), sw_regions=_sw_regions(lang),
                   sw_start_on=any(a["taxon"] == "*" for a in d["swarm_alerts"]),
                   fc_enabled=_fc_on(),
                   sw_where=lambda c, r: _sw_where(lang, c, r),
                   sw_default=(f"{d['map']['country']}-{d['map'].get('region_code') or ''}".rstrip("-")
                               if d["map"] and d["map"].get("country") else ""),
                   login_url="")
    resp.headers["Cache-Control"] = "private, no-store"
    resp.set_status(status)
    return resp


def _swarm_on() -> int:
    from config import SWARM_CHANNEL_ID
    return SWARM_CHANNEL_ID


def _fc_on() -> bool:
    from config import SWARM_FORECAST
    return bool(SWARM_FORECAST)


def _sw_regions(lang: str) -> list[tuple[str, str]]:
    """Auswahl für den Schwarmflug-Alarm: überall, ein Land oder eine Region."""
    out = [("", "🌍 " + translate(lang, "konto_sw_anywhere"))]
    for c, regs in geo.all_regions().items():
        out.append((c, f"{country_name(lang, c)} ({translate(lang, 'konto_sw_whole')})"))
        if c != "li":
            out += [(f"{c}-{code}", f"  · {name}") for code, name in sorted(regs.items(), key=lambda x: x[1])]
    return out


def _sw_where(lang: str, country: str, region: str) -> str:
    if not country:
        return translate(lang, "konto_sw_anywhere")
    if not region or country == "li":
        return country_name(lang, country)
    return SWB.S.region_name(country, region)


def _all_shops_safe() -> dict:
    try:
        from utils import directory as dirx
        return dirx.all_shops()
    except Exception:
        return {}


async def h_konto(req):
    lang = pick_lang(req)
    uid = _aam_member(req)
    m = req.query.get("m", "")
    flash = translate(lang, "konto_m_" + m, n=req.query.get("n", "")) if m and ("konto_m_" + m) in _BI18N_T else ""
    if not uid:
        former = _is_member(req)                    # gültige Sitzung, aber nicht mehr auf dem Server
        from config import COMMAND_LOG_RETENTION_DAYS
        resp = _render(req, "konto", title=translate(lang, "konto_h"), flash=flash, uid=None,
                       former=former, csrf=_member_csrf(former) if former else "",
                       invite_url=(DISCORD_INVITE_URL if DISCORD_INVITE_URL.startswith("https://") else ""),
                       log_days=COMMAND_LOG_RETENTION_DAYS,
                       login_url="" if former else _login_url(req))
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    nf = (req.query.get("nf") or "").strip()[:80]
    if nf:                                            # aus dem Arten-Finder: Formular vorbefüllen
        nr = (req.query.get("nr") or "").strip().lower()
        pre = {"kind": "species" if " " in nf else "genus", "name": nf,
               "regions": [nr] if re.fullmatch(r"[a-z]{2}", nr) else [],
               "swiss": req.query.get("ns") == "1"}
        return await _konto_page(req, uid, flash=flash, form=pre, open_form="notif")
    return await _konto_page(req, uid, flash=flash)


def _konto_back(req, code: str, anchor: str = "", n="") -> web.HTTPSeeOther:
    lang = pick_lang(req)
    q = {"lang": lang, "m": code}
    if n != "":
        q["n"] = str(n)
    return web.HTTPSeeOther(f"/konto?{urlencode(q)}" + (f"#{anchor}" if anchor else ""))


def _num(v, cast=int):
    try:
        return cast(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


async def h_konto_post(req):
    """Alle Änderungen aus „Mein Konto“ (Formular-POST, CSRF + Rate-Limit, nur eigene Daten)."""
    lang = pick_lang(req)
    uid = _aam_member(req)
    if not uid:
        # Nicht mehr auf dem Server: nur noch „alles löschen“ (danach abmelden).
        former = _is_member(req)
        if former and req.match_info["action"] == "loeschen":
            form = await req.post()
            if hmac.compare_digest(str(form.get("csrf", "")), _member_csrf(former)) \
                    and form.get("area") == "all" and form.get("sure") \
                    and _rate("konto:" + _hmac("konto", former), 40, 300):
                await ACC.delete_area(req.app["bot"], former, "all")
                resp = web.HTTPSeeOther(f"/konto?lang={lang}&m=left_deleted")
                resp.del_cookie(_MEMBER_COOKIE)
                resp.del_cookie(_ADMIN_COOKIE)
                logger.info("👤 Konto: Daten nach Server-Austritt gelöscht (%s)", former)
                raise resp
        raise web.HTTPSeeOther(f"/konto?lang={lang}")
    form = await req.post()
    if not hmac.compare_digest(str(form.get("csrf", "")), _member_csrf(uid)):
        raise web.HTTPForbidden(text="bad csrf")
    if not _rate("konto:" + _hmac("konto", uid), 40, 300):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app["bot"]
    act = req.match_info["action"]
    T = lambda key, **kw: translate(lang, key, **kw)
    # Löschen nur mit freigeschaltetem Schalter „Löschen freischalten“ (Seite oben).
    if (act in _KONTO_DANGER or (act == "benachrichtigung" and form.get("do") == "delete")) \
            and form.get("unlock") != "1":
        raise _konto_back(req, "locked", "")

    # ── Benachrichtigungen ────────────────────────────────────────────────────
    if act == "benachrichtigung":
        nid, do = _num(form.get("id")), form.get("do")
        if nid is None:
            raise _konto_back(req, "error", "benachrichtigungen")
        if do == "delete":
            await execute_db(bot, "DELETE FROM notifications WHERE id=? AND user_id=?", (nid, uid), commit=True)
            raise _konto_back(req, "deleted", "benachrichtigungen")
        if do == "pause":
            await execute_db(bot, "UPDATE notifications SET status='paused' WHERE id=? AND user_id=? "
                                  "AND status='active'", (nid, uid), commit=True)
            raise _konto_back(req, "paused", "benachrichtigungen")
        if do == "resume":
            rows = await execute_db(bot, "SELECT species, regions, excluded_species FROM notifications "
                                         "WHERE id=? AND user_id=?", (nid, uid), fetch=True)
            if rows:
                await execute_db(bot, "UPDATE notifications SET status='active', created_at=CURRENT_TIMESTAMP, "
                                      "pending_feedback_until=NULL WHERE id=? AND user_id=?", (nid, uid), commit=True)
                r = rows[0]
                _konto_check_now(bot, uid, r["species"], r["regions"], r["excluded_species"])
            raise _konto_back(req, "resumed", "benachrichtigungen")
        raise _konto_back(req, "error", "benachrichtigungen")

    if act == "benachrichtigung/neu":
        return await _konto_notif_add(req, uid, form)

    # ── Preise ────────────────────────────────────────────────────────────────
    if act == "preis/entfernen":
        pid, vid = _num(form.get("pid")), _num(form.get("vid")) or 0
        await execute_db(bot, "DELETE FROM user_price_tracking WHERE user_id=? AND product_id=? AND variant_id=?",
                         (uid, pid, vid), commit=True)
        raise _konto_back(req, "deleted", "preise")

    if act == "zielpreis":
        pid, vid = _num(form.get("pid")), _num(form.get("vid")) or 0
        raw = (form.get("price") or "").strip()
        mode = form.get("mode") if form.get("mode") in ("additional", "replace") else "additional"
        if not raw:
            await execute_db(bot, "UPDATE user_price_tracking SET target_price=NULL, target_mode=NULL "
                                  "WHERE user_id=? AND product_id=? AND variant_id=?", (uid, pid, vid), commit=True)
            raise _konto_back(req, "target_off", "preise")
        price = _num(raw, float)
        if price is None or price <= 0 or price > 100000:
            raise _konto_back(req, "bad_price", "preise")
        await execute_db(bot, "UPDATE user_price_tracking SET target_price=?, target_mode=? "
                              "WHERE user_id=? AND product_id=? AND variant_id=?",
                         (round(price, 2), mode, uid, pid, vid), commit=True)
        raise _konto_back(req, "target_set", "preise")

    if act == "preis/neu":
        from cogs.price_tracking import _find_products_for_tracking, save_tracking
        species = (form.get("species") or "").strip()[:80]
        sel = {str(x) for x in form.getall("sel", [])}
        if not species or not sel:
            raise _konto_back(req, "nothing", "preise")
        found = await _find_products_for_tracking(bot, species)
        saved = 0
        for sid, blk in found.items():
            for p in blk["products"]:
                if f"{sid}|{p.get('id')}" in sel and p.get("id") is not None:
                    if await save_tracking(bot, uid, sid, blk["shop_info"].get("name", sid), p):
                        saved += 1
        raise _konto_back(req, "track_added", "preise", n=saved)

    if act == "beobachtung/neu":
        from utils.availability import normalize_species_name
        from utils import species_catalog
        from cogs.price_tracking import init_species_watch_seen
        raw = (form.get("species") or "").strip()[:80]
        if len(raw) < 3:
            raise _konto_back(req, "nothing", "preise")
        canon = species_catalog.canonical(raw)
        norm = normalize_species_name(canon or raw)
        is_genus = 0 if " " in norm.strip() else 1
        rc = await execute_db(bot, "INSERT INTO user_species_watch (user_id, species, is_genus) VALUES (?,?,?) "
                                   "ON CONFLICT(user_id, species) DO NOTHING", (uid, norm, is_genus), commit=True)
        if rc:
            await init_species_watch_seen(bot, uid, norm, is_genus)
        back = str(form.get("back") or "")
        if _BACK_RE.match(back):                         # Stern auf /art: zurück zur Seite
            path, _, qs_ = back.partition("?")
            qd = dict(p.split("=", 1) for p in qs_.split("&") if "=" in p) if qs_ else {}
            qd = {k: v for k, v in qd.items() if k not in ("m", "n", "lang")}
            from urllib.parse import unquote_plus
            qd = {k: unquote_plus(v) for k, v in qd.items()}
            qd.update(lang=lang, m="watch_added" if rc else "exists")
            raise web.HTTPSeeOther(path + "?" + urlencode(qd))
        raise _konto_back(req, "watch_added" if rc else "exists", "preise")

    if act == "beobachtung/entfernen":
        sp = (form.get("species") or "").strip()
        for sql in ("DELETE FROM user_species_watch WHERE user_id=? AND species=?",
                    "DELETE FROM user_species_watch_seen WHERE user_id=? AND watched_species=?",
                    "DELETE FROM user_species_watch_variant_seen WHERE user_id=? AND watched_species=?",
                    "DELETE FROM pending_variant_removed WHERE user_id=? AND watched_species=?"):
            await execute_db(bot, sql, (uid, sp), commit=True)
        raise _konto_back(req, "deleted", "preise")

    # ── Einstellungen ─────────────────────────────────────────────────────────
    if act == "einstellungen":
        lng = form.get("language") or ""
        if lng in ("de", "en", "eo"):
            await execute_db(bot, "DELETE FROM user_settings WHERE user_id=?", (uid,), commit=True)
            await execute_db(bot, "INSERT INTO user_settings (user_id, language) VALUES (?, ?) "
                                  "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
                             (int(uid), lng), commit=True)
        elif lng == "":
            for v in (int(uid), uid):
                await execute_db(bot, "DELETE FROM user_settings WHERE user_id=?", (v,), commit=True)
        if form.get("digest"):
            await execute_db(bot, "INSERT OR IGNORE INTO digest_subscribers (user_id) VALUES (?)", (uid,), commit=True)
        else:
            await execute_db(bot, "DELETE FROM digest_subscribers WHERE user_id=?", (uid,), commit=True)
        qs_, qe_ = _num(form.get("qs")), _num(form.get("qe"))
        quiet = bool(form.get("quiet")) and qs_ is not None and qe_ is not None \
            and 0 <= qs_ < 24 and 0 <= qe_ < 24 and qs_ != qe_
        await ACC.set_prefs(bot, uid, rsvp_reminder=1 if form.get("rsvp") else 0,
                            intros=1 if form.get("intros") else 0,
                            quiet_start=qs_ if quiet else None, quiet_end=qe_ if quiet else None)
        raise _konto_back(req, "saved", "einstellungen")

    if act == "blacklist/neu":
        from utils.availability import load_shop_data
        sid = str(form.get("shop") or "")
        if sid in await load_shop_data(bot):
            await execute_db(bot, "INSERT OR IGNORE INTO user_shop_blacklist (user_id, shop_id) VALUES (?, ?)",
                             (uid, sid), commit=True)
        raise _konto_back(req, "saved", "einstellungen")

    if act == "blacklist/entfernen":
        await execute_db(bot, "DELETE FROM user_shop_blacklist WHERE user_id=? AND shop_id=?",
                         (uid, str(form.get("shop") or "")), commit=True)
        raise _konto_back(req, "deleted", "einstellungen")

    if act == "schlagwort/neu":
        from cogs.offer_alerts import _plain, MAX_KEYWORDS
        cnt = await execute_db(bot, "SELECT COUNT(*) AS n FROM offer_keywords WHERE user_id=?", (uid,), fetch=True)
        have = cnt[0]["n"] if cnt else 0
        added = []
        for part in re.split(r"[;,]", form.get("kw") or ""):
            raw = part.strip()
            kw = _plain(raw)
            if not (2 <= len(kw) <= 80) or have >= MAX_KEYWORDS:
                continue
            rc = await execute_db(bot, "INSERT OR IGNORE INTO offer_keywords (user_id, keyword, keyword_raw) "
                                       "VALUES (?,?,?)", (uid, kw, raw), commit=True)
            if rc:
                have += 1
                added.append((kw, raw))
        cog = bot.cogs.get("OfferAlerts") if hasattr(bot, "cogs") else None
        if cog and added:
            async def _bf():
                try:
                    user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                    for kw, raw in added:
                        await cog._backfill(user, uid, kw, raw)
                except Exception as e:
                    logger.info("👤 Konto: Rückblick für Schlagworte fehlgeschlagen: %s", e)
            asyncio.create_task(_bf())
        raise _konto_back(req, "kw_added", "einstellungen", n=len(added))

    if act == "schlagwort/entfernen":
        await execute_db(bot, "DELETE FROM offer_keywords WHERE user_id=? AND keyword=?",
                         (uid, str(form.get("kw") or "")), commit=True)
        await execute_db(bot, "DELETE FROM quiet_offer_queue WHERE user_id=? AND keyword=?",
                         (uid, str(form.get("kw") or "")), commit=True)
        raise _konto_back(req, "deleted", "einstellungen")

    # ── Schwarmflug-Alarm ─────────────────────────────────────────────────────
    if act == "schwarm/neu":
        from utils import swarm as SW
        if str(form.get("taxon") or "") == "*":            # PN beim Saisonstart
            await execute_db(bot, "INSERT OR IGNORE INTO swarm_alerts (user_id, taxon, country, region) "
                                  "VALUES (?,?,?,?)", (uid, "*", "", ""), commit=True)
            raise _konto_back(req, "sw_added", "schwarmflug")
        taxa = SW.parse_taxa(str(form.get("taxon") or "")[:60])
        if not taxa:
            raw = str(form.get("taxon") or "").strip().split()
            g = SW._genus_of(raw[0]) if raw else None
            taxa = [{"genus": g, "species": None}] if g else []
        where = str(form.get("where") or "")
        c, _, r = where.partition("-")
        ok_where = where == "" or (c in geo.all_regions() and (not r or r in geo.all_regions()[c]))
        cnt = await execute_db(bot, "SELECT COUNT(*) AS n FROM swarm_alerts WHERE user_id=?", (uid,), fetch=True)
        if not taxa or not ok_where or (cnt and cnt[0]["n"] >= 20):
            raise _konto_back(req, "sw_bad", "schwarmflug")
        tx = taxa[0]
        taxon = tx["genus"] + (" " + tx["species"] if tx.get("species") else "")
        await execute_db(bot, "INSERT OR IGNORE INTO swarm_alerts (user_id, taxon, country, region) VALUES (?,?,?,?)",
                         (uid, taxon, c, "" if c == "li" else r), commit=True)
        raise _konto_back(req, "sw_added", "schwarmflug")

    if act == "schwarm/prognose":
        lv = str(form.get("level") or "")
        if lv not in ("off", "rare", "normal", "often"):
            raise _konto_back(req, "error", "schwarmflug")
        await execute_db(bot, "INSERT INTO fc_pref (user_id, level) VALUES (?,?) ON CONFLICT(user_id) "
                              "DO UPDATE SET level=excluded.level", (uid, lv), commit=True)
        raise _konto_back(req, "fc_saved", "schwarmflug")

    if act == "schwarm/entfernen":
        await execute_db(bot, "DELETE FROM swarm_alerts WHERE user_id=? AND taxon=? AND country=? AND region=?",
                         (uid, str(form.get("taxon") or ""), str(form.get("country") or ""),
                          str(form.get("region") or "")), commit=True)
        raise _konto_back(req, "deleted", "schwarmflug")

    # ── Termine & Kalender ────────────────────────────────────────────────────
    if act == "termin/absagen":
        await execute_db(bot, "DELETE FROM map_event_rsvp WHERE event_id=? AND occ_date=? AND user_id=?",
                         (_num(form.get("eid")), str(form.get("occ") or "")[:10], uid), commit=True)
        raise _konto_back(req, "rsvp_removed", "termine")

    if act == "kalender/neu":
        await ACC.ics_salt(bot, uid, rotate=True)
        raise _konto_back(req, "ics_new", "termine")

    # ── Karte ─────────────────────────────────────────────────────────────────
    if act == "karte/tags":
        have = await execute_db(bot, "SELECT 1 FROM map_entries WHERE user_id=?", (uid,), fetch=True)
        if have:
            tags = [c for c in form.getall("tag", []) if map_tags.is_valid(c)]
            await execute_db(bot, "DELETE FROM map_entry_tags WHERE user_id=?", (uid,), commit=True)
            for c in dict.fromkeys(tags):
                await execute_db(bot, "INSERT OR IGNORE INTO map_entry_tags (user_id, tag_code) VALUES (?,?)",
                                 (uid, c), commit=True)
        raise _konto_back(req, "saved", "karte")

    if act == "karte/loeschen":
        for sql in ("DELETE FROM map_entry_tags WHERE user_id=?", "DELETE FROM map_entry_regions WHERE user_id=?",
                    "DELETE FROM map_entries WHERE user_id=?"):
            await execute_db(bot, sql, (uid,), commit=True)
        raise _konto_back(req, "map_deleted", "karte")

    # ── iNaturalist ───────────────────────────────────────────────────────────
    if act in ("inat/start", "inat/pruefen", "inat/trennen"):
        from utils import inat as I
        from utils.localization import l10n
        if act == "inat/trennen":
            await I.unlink(bot, uid)
            raise _konto_back(req, "inat_unlinked", "inat")
        if act == "inat/start":
            login = (form.get("login") or "").strip().lstrip("@")
            if not I.LOGIN_RE.match(login):
                return await _konto_page(req, uid, flash=_MD_STRIP.sub("", l10n.get("inat_link_bad_login", lang)))
            await I.start_link(bot, uid, login)
            raise _konto_back(req, "inat_code", "inat")
        status, u = await I.verify_link(bot, uid)
        if status == "ok":
            st = await I.user_stats(bot, uid)
            msg = l10n.get("inat_link_ok", lang, login=u["login"], obs=st["obs"], species=st["species"])
        else:
            msg = l10n.get(f"inat_link_{status}", lang)
        return await _konto_page(req, uid, flash=_MD_STRIP.sub("", msg))

    # ── Sitzungen & Datenschutz ───────────────────────────────────────────────
    if act == "abmelden":
        await ACC.set_prefs(bot, uid, logout_before=ACC.now_epoch())
        resp = web.HTTPSeeOther(f"/konto?lang={lang}&m=logout")
        resp.del_cookie(_MEMBER_COOKIE)
        resp.del_cookie(_ADMIN_COOKIE)
        logger.info("👤 Konto: überall abgemeldet (%s)", uid)
        raise resp

    if act == "loeschen":
        area = str(form.get("area") or "")
        if area == "all" and not form.get("sure"):
            raise _konto_back(req, "error", "daten")
        if area != "all" and area not in ACC.AREAS:
            raise _konto_back(req, "error", "daten")
        await ACC.delete_area(bot, uid, area)
        raise _konto_back(req, "area_deleted", "daten")

    raise web.HTTPNotFound()


def _konto_check_now(bot, uid, species, regions, excluded) -> None:
    """Sofort-Prüfung nach Anlegen/Fortsetzen (wie /notification), außer in der Ruhezeit."""
    cog = bot.cogs.get("Notifications") if hasattr(bot, "cogs") else None
    if not cog or ACC.is_quiet(uid):
        return
    ex = {s.strip().lower() for s in (excluded or "").split(",") if s.strip()}
    asyncio.create_task(cog.trigger_availability_check(uid, species, regions,
                                                       ch_mode=(regions == "ch"), excluded_species_list=ex))


async def _konto_notif_add(req, uid, form):
    """Neue Verfügbarkeits-Benachrichtigung, gleiche Regeln wie /notification."""
    from utils.availability import load_shop_data, expand_regions, species_exists
    from utils import species_catalog
    lang = pick_lang(req)
    bot = req.app["bot"]
    T = lambda key, **kw: translate(lang, key, **kw)
    kind = "genus" if form.get("kind") == "genus" else "species"
    name = " ".join((form.get("name") or "").split())[:80]
    exclude = (form.get("exclude") or "").strip()[:300]
    regs = [r for r in form.getall("region", []) if re.fullmatch(r"[a-z]{2}", r)]
    swiss = bool(form.get("swiss"))
    force = bool(form.get("force"))
    keep = {"kind": kind, "name": name, "exclude": exclude, "regions": regs, "swiss": swiss}

    def fail(msg):
        return _konto_page(req, uid, flash=_MD_STRIP.sub("", msg), form=keep, open_form="notif")

    if not name:
        return await fail(T("konto_err_name"))
    if kind == "species" and " " not in name:
        return await fail(T("konto_err_species_format"))
    if kind == "genus" and " " in name:
        name = name.split()[0]
    if not force:
        msg = species_catalog.validation_message(species_catalog.check(name), lang)
        if msg:
            return await fail(msg.replace("`force`", T("konto_force")))
    term = species_catalog.canonical(name) or name
    shop_data = await load_shop_data(bot)
    avail = {(sd.get("country") or "").lower() for sd in shop_data.values() if sd.get("country")}
    if swiss:
        valid = ["ch"]
    else:
        valid = sorted(r for r in await expand_regions(bot, regs) if r in avail)
        if not valid:
            return await fail(T("konto_err_regions"))
    if not force and not await species_exists(bot, term):
        return await fail(T("konto_err_unknown", term=term))
    excluded = exclude.lower() if (kind == "genus" and exclude) else None
    regions_str = ",".join(valid)
    await execute_db(bot,
        """INSERT INTO notifications (user_id, species, regions, status, excluded_species, server_id)
           VALUES (?, ?, ?, 'active', ?, ?)
           ON CONFLICT(user_id, species, regions)
           DO UPDATE SET created_at=CURRENT_TIMESTAMP, status='active',
               excluded_species=excluded.excluded_species, server_id=excluded.server_id""",
        (uid, term, regions_str, excluded, MAP_GUILD_ID or None), commit=True)
    if MAP_GUILD_ID:
        await execute_db(bot, "INSERT OR IGNORE INTO server_user_mappings (user_id, server_id) VALUES (?, ?)",
                         (uid, MAP_GUILD_ID), commit=True)
    _konto_check_now(bot, uid, term, regions_str, excluded)
    raise _konto_back(req, "notif_added", "benachrichtigungen")


async def h_konto_export(req):
    """Alle eigenen Daten als JSON (Art. 15/20 DSGVO)."""
    uid = _aam_member(req)
    if not uid:
        raise web.HTTPSeeOther(f"/konto?lang={pick_lang(req)}")
    if not _rate("kontoexp:" + _hmac("kontoexp", uid), 5, 300):
        raise web.HTTPTooManyRequests(text="rate limited")
    data = await ACC.export_user(req.app["bot"], uid)
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    return web.Response(body=body, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": f"attachment; filename=aam-meine-daten-{uid}.json",
        "Cache-Control": "private, no-store"})


async def h_konto_ics(req):
    """Persönlicher Kalender: nur die eigenen Zusagen. Geheimer Link (neu erzeugen = alter ungültig)."""
    uid, salt = req.match_info["uid"], req.match_info["salt"]
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    cur = ACC.prefs(uid)["ics_salt"] or ""
    if not (uid.isdigit() and cur and hmac.compare_digest(cur, salt)):
        raise web.HTTPNotFound()
    since = (datetime.now(BERLIN) - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = await execute_db(req.app["bot"],
        "SELECT r.occ_date, e.* FROM map_event_rsvp r JOIN map_events e ON e.id=r.event_id "
        "WHERE r.user_id=? AND r.occ_date>=? AND e.status='approved' ORDER BY r.occ_date",
        (uid, since), fetch=True) or []
    occ_rows = []
    for r in rows:
        r = dict(r)
        st = _naive(r["start_at"])
        if st is None:
            continue
        try:
            day = datetime.strptime(r["occ_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        nst = datetime.combine(day, st.time())
        en = _naive(r["end_at"])
        r["start_at"] = nst.isoformat()
        r["end_at"] = (nst + (en - st)).isoformat() if en and en > st else None
        r["rrule"], r["exdates"] = None, None
        r["id"] = f"{r['id']}-{r['occ_date'].replace('-', '')}"
        occ_rows.append(r)
    lang = pick_lang(req)
    body = _ics_build(req, occ_rows, feed=True, name=translate(lang, "konto_ics_name"),
                      source=_ics_personal_url(uid, salt))
    return web.Response(body=body, headers={"Content-Type": "text/calendar; charset=utf-8",
                                            "Content-Disposition": "inline; filename=aam-meine-termine.ics",
                                            "Cache-Control": "private, max-age=900"})


def build_app(bot) -> web.Application:
    app = web.Application(client_max_size=1024*1024)
    app["bot"] = bot
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


class BoardCog(commands.Cog, name="Board"):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.runner: web.AppRunner | None = None
        if BOARD_ENABLED:
            self._task = bot.loop.create_task(self._start())

    async def _start(self):
        await self.bot.wait_until_ready()
        if not _admin_ids() or not (BOARD_OAUTH_CLIENT_ID and BOARD_OAUTH_REDIRECT_URI):
            logger.warning("⚠️ Board aktiv, aber Admin-Login unmöglich: BOARD_OWNER_ID und "
                           "Discord-OAuth (BOARD_OAUTH_*) müssen gesetzt sein.")
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
        try:
            await _process_alerts(self.bot)
        except Exception as e:
            logger.warning("🔔 Alarm-Verarbeitung fehlgeschlagen: %s", e)

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
