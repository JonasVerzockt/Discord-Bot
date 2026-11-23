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
utils/species_profile.py – Kurzsteckbrief einer Art aus seriösen, frei lizenzierten Quellen
für die Board-Seite /art/<Name> (NICHT in Discord).

Priorität / Quellen:
  1. AntCat     – gültiger Name (lokal, utils/species_catalog; hier nur als Schlüssel)
  2. Wikipedia  – 2–3 Sätze aus der Artikel-Einleitung (Seitensprache, sonst en).
                  Lizenz CC BY-SA 4.0 → Quelle, Autorenliste (Versionsgeschichte) und
                  Lizenz werden verlinkt, Kürzung wird gekennzeichnet.
  3. GBIF       – Zahl der Fundnachweise und Länder (aggregierte Zahlen, Link auf GBIF).
  4. AntWeb     – nur Link (Bildlizenzen auf den Seiten nicht eindeutig → keine Bilder).
  immer AntWiki – nur Link (Lizenz je Autor unterschiedlich → kein Text).

Abrufe laufen serverseitig (keine Anfragen aus dem Browser der Besucher), Ergebnis
wird in data/species_profiles.json gecacht: Erfolg 30 Tage, Fehlschlag 1 Tag.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from config import DATA_DIR, BOARD_PUBLIC_URL, VERSION

logger = logging.getLogger(__name__)

CACHE_FILE = Path(DATA_DIR) / "species_profiles.json"
_OK_TTL = 30 * 86400
_FAIL_TTL = 86400
_TIMEOUT = aiohttp.ClientTimeout(total=8)
_UA = f"AAM-Bot/{VERSION} species-profile (+{BOARD_PUBLIC_URL or 'https://github.com/JonasVerzockt/Discord-Bot'})"
_MAX_CHARS = 520
_MAX_SENTENCES = 3
_lock = threading.Lock()
_inflight: dict[str, asyncio.Task] = {}


# ── Cache ─────────────────────────────────────────────────────────────────────
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(key: str, entry: dict) -> None:
    with _lock:
        data = _load_cache()
        data[key] = entry
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CACHE_FILE)


def _fresh(entry: dict | None) -> bool:
    if not entry:
        return False
    ttl = _OK_TTL if entry.get("ok") else _FAIL_TTL
    return time.time() - entry.get("at", 0) < ttl


# ── Links (immer verfügbar, ohne Abruf) ───────────────────────────────────────
def links(name: str) -> dict:
    g, _, ep = name.strip().partition(" ")
    return {
        "antwiki": f"https://www.antwiki.org/wiki/{quote(name.strip().replace(' ', '_'))}",
        "antweb": (f"https://www.antweb.org/description.do?genus={quote(g.lower())}"
                   f"&name={quote(ep.lower())}&rank=species") if ep else "",
    }


# ── Wikipedia ─────────────────────────────────────────────────────────────────
def _shorten(text: str) -> tuple[str, bool]:
    """Auf ganze Sätze bis _MAX_CHARS kürzen. Rückgabe (Text, gekürzt?)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])", text)
    if len(text) <= _MAX_CHARS and len(parts) <= _MAX_SENTENCES:
        return text, False
    out = ""
    for i, p in enumerate(parts):
        if i >= _MAX_SENTENCES or (out and len(out) + 1 + len(p) > _MAX_CHARS):
            break
        out = f"{out} {p}".strip()
    if not out:
        out = text[:_MAX_CHARS].rsplit(" ", 1)[0] + " …"
    return out, True


async def _wikipedia(sess: aiohttp.ClientSession, name: str, lang: str) -> dict | None:
    genus = name.split()[0].lower()
    title = quote(name.replace(" ", "_"))
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
    async with sess.get(url) as r:
        if r.status != 200:
            return None
        d = await r.json(content_type=None)
    if d.get("type") not in (None, "standard"):          # z. B. disambiguation
        return None
    extract = d.get("extract") or ""
    # Plausibilität: Der Artikel muss die Gattung nennen (Schutz vor falschen Weiterleitungen)
    if genus not in (extract + " " + (d.get("title") or "")).lower():
        return None
    text, cut = _shorten(extract)
    if len(text) < 40:
        return None
    page = ((d.get("content_urls") or {}).get("desktop") or {}).get("page") \
        or f"https://{lang}.wikipedia.org/wiki/{title}"
    art_title = d.get("title") or name
    return {
        "lang": lang, "title": art_title, "text": text, "cut": cut, "url": page,
        "history": f"https://{lang}.wikipedia.org/w/index.php?title="
                   f"{quote(art_title.replace(' ', '_'))}&action=history",
    }


# ── GBIF ──────────────────────────────────────────────────────────────────────
async def _gbif(sess: aiohttp.ClientSession, name: str) -> dict | None:
    async with sess.get("https://api.gbif.org/v1/species/match",
                        params={"name": name, "kingdom": "Animalia"}) as r:
        if r.status != 200:
            return None
        m = await r.json(content_type=None)
    key = m.get("usageKey")
    if not key or (m.get("canonicalName") or "").lower() != name.lower() \
            or (m.get("family") or "").lower() != "formicidae":
        return None
    key = m.get("acceptedUsageKey") or key
    async with sess.get("https://api.gbif.org/v1/occurrence/search",
                        params={"taxonKey": str(key), "limit": "0",
                                "facet": "country", "facetLimit": "300"}) as r:
        if r.status != 200:
            return None
        o = await r.json(content_type=None)
    countries = []
    for f in o.get("facets") or []:
        if str(f.get("field", "")).upper() == "COUNTRY":
            countries = [(c.get("name", ""), int(c.get("count", 0))) for c in f.get("counts") or []]
    return {"key": key, "count": int(o.get("count") or 0),
            "countries": countries, "url": f"https://www.gbif.org/species/{key}"}


# ── Öffentliche API ───────────────────────────────────────────────────────────
async def _fetch(name: str) -> dict:
    entry: dict = {"at": time.time(), "ok": False, "wiki": {}, "gbif": None}
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers={"User-Agent": _UA,
                                     "Accept": "application/json"}) as sess:
        for lang in ("de", "en", "eo"):
            try:
                w = await _wikipedia(sess, name, lang)
                if w:
                    entry["wiki"][lang] = w
            except Exception as e:
                logger.info("📖 Wikipedia (%s) für %s: %s", lang, name, e)
        try:
            entry["gbif"] = await _gbif(sess, name)
        except Exception as e:
            logger.info("🌍 GBIF für %s: %s", name, e)
    entry["ok"] = bool(entry["wiki"] or entry["gbif"])
    _save(name.lower(), entry)
    logger.info("🐜 Steckbrief %s: Wikipedia %s, GBIF %s", name,
                ",".join(entry["wiki"]) or "–", "ja" if entry["gbif"] else "–")
    return entry


async def get(name: str, wait: float = 6.0) -> dict | None:
    """Steckbrief aus dem Cache; fehlt er oder ist er alt, wird neu geladen.
    Wartet höchstens *wait* Sekunden – sonst kommt der (evtl. alte) Cache-Stand,
    und der Abruf läuft im Hintergrund weiter."""
    key = name.lower()
    entry = _load_cache().get(key)
    if _fresh(entry):
        return entry
    task = _inflight.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(_fetch(name))
        _inflight[key] = task
        task.add_done_callback(lambda _t: _inflight.pop(key, None))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
    except Exception:
        return entry


def view(entry: dict | None, lang: str) -> dict:
    """Für das Template: Wikipedia in Seitensprache, sonst en, sonst de."""
    if not entry:
        return {"wiki": None, "gbif": None}
    wiki = entry.get("wiki") or {}
    w = wiki.get(lang) or wiki.get("en") or wiki.get("de")
    return {"wiki": w, "gbif": entry.get("gbif")}
