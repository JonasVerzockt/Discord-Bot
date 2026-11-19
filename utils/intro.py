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
utils/intro.py – Anlass-Intros beim Öffnen des Boards (Auswahl, keine Darstellung).

Die Animationen liegen in static/intro.js, die Route /intro.json in cogs/board.py.
Reihenfolge = Vorrang: persönlich → Feiertage → Saison. Jeder Anlass kommt je Jahr
einmal (Cookie board_intro), höchstens ein Intro pro Tag.

Zeiträume (Berliner Zeit):
  Neujahr 31.12.–1.1. · Weihnachten 1.–26.12. · Ostern Karfreitag–Ostermontag (berechnet)
  31.10.–2.11. Halloween + AAM-Server-Geburtstag (seit 31.10.2017) · 22.–24.6. Bot-Geburtstag (seit 22.06.2026)
  Jahresrückblick 1.11.–31.1. · Schwarmflug 1.7.–31.8. · Frühlingsstart 20.3.–10.4.

Schwarmflug-Zeitraum laut Quellen: Lasius niger „Mating swarms occur from July to late
August“ (Collingwood 1979, zitiert auf https://www.antwiki.org/wiki/Lasius_niger) und
„usually occurs in July or August“ (Natural History Museum London,
https://www.nhm.ac.uk/discover/when-why-winged-ants-swarm-nuptial-flight.html).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from utils.db import execute_db

SERVER_BIRTH = date(2017, 10, 31)
BOT_BIRTH = date(2026, 6, 22)
MILESTONES_INAT = (10, 25, 50)
MILESTONES_REVIEWS = (100,)

# Alle Szenen (für ?intro=… im Admin-Test). Schlüssel = Szene in static/intro.js.
SCENES = ("newyear", "xmas", "easter", "aam", "botbday", "yearreview", "swarm", "spring",
          "join", "ach", "inat", "reviews")


def easter(y: int) -> date:
    """Ostersonntag (gregorianisch, anonymer Algorithmus nach Meeus/Jones/Butcher)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _in(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def seasonal(today: date) -> list[dict]:
    """Feiertage und Saison-Anlässe für heute, nach Vorrang sortiert."""
    y = today.year
    out: list[dict] = []
    # Neujahr: Schlüssel = das neue Jahr
    if today.month == 12 and today.day == 31:
        out.append({"key": f"newyear{y + 1}", "scene": "newyear", "p": {"y": y + 1}})
    elif today.month == 1 and today.day == 1:
        out.append({"key": f"newyear{y}", "scene": "newyear", "p": {"y": y}})
    if today.month == 12 and today.day <= 26:
        out.append({"key": f"xmas{y}", "scene": "xmas", "p": {}})
    eas = easter(y)
    if _in(today, eas - timedelta(days=2), eas + timedelta(days=1)):
        out.append({"key": f"easter{y}", "scene": "easter", "p": {}})
    if _in(today, date(y, 10, 31), date(y, 11, 2)):
        out.append({"key": f"aam{y}", "scene": "aam", "p": {"n": y - SERVER_BIRTH.year}})
    if y > BOT_BIRTH.year and _in(today, date(y, 6, 22), date(y, 6, 24)):
        out.append({"key": f"botbday{y}", "scene": "botbday", "p": {"n": y - BOT_BIRTH.year}})
    # Saison
    if today.month in (11, 12, 1):
        ry = y if today.month >= 11 else y - 1
        out.append({"key": f"yearreview{ry}", "scene": "yearreview", "p": {"y": ry}, "link": "/rueckblick"})
    if today.month in (7, 8):
        out.append({"key": f"swarm{y}", "scene": "swarm", "p": {}})
    if _in(today, date(y, 3, 20), date(y, 4, 10)):
        out.append({"key": f"spring{y}", "scene": "spring", "p": {}})
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def personal(bot, uid: str, today: date, joined: date | None, prefs: dict, lang: str) -> list[dict]:
    """Persönliche Anlässe. Beim ersten Mal wird nur der Ist-Stand gemerkt (keine Feier
    für Altes). 'commit' sagt, was nach dem Anzeigen gespeichert wird."""
    from utils.localization import l10n
    out: list[dict] = []
    uid = str(uid)
    # Discord-Beitritt (Jahrestag, Tag selbst bis +2 Tage)
    if joined and today.year > joined.year:
        try:
            anniv = joined.replace(year=today.year)
        except ValueError:                       # 29.02.
            anniv = date(today.year, 3, 1)
        if _in(today, anniv, anniv + timedelta(days=2)):
            out.append({"key": f"join{today.year}", "scene": "join", "p": {"n": today.year - joined.year}})
    # Neue Erfolge seit dem letzten Intro
    seen = prefs.get("ach_seen_at")
    if not seen:
        out.append({"key": "", "scene": "", "commit": {"ach_seen_at": _utc_now()}, "silent": True})
    else:
        rows = await execute_db(bot,
            "SELECT achievement_id FROM achievements WHERE user_id=? AND unlocked_at > ? "
            "ORDER BY unlocked_at DESC", (uid, seen), fetch=True) or []
        if rows:
            try:
                from utils.achievements import ACHIEVEMENTS
                emo = {a.id: a.emoji for a in ACHIEVEMENTS}
            except Exception:
                emo = {}
            aid = rows[0]["achievement_id"]
            out.append({"key": f"ach-{aid}", "scene": "ach",
                        "p": {"n": len(rows), "e": emo.get(aid, "🏅"),
                              "name": l10n.get(f"ach_{aid}_t", lang)},
                        "link": "/konto#erfolge", "commit": {"ach_seen_at": _utc_now()}})
    # Meilensteine (iNat-Arten insgesamt, eigene Bewertungen insgesamt)
    have = {m for m in (prefs.get("milestones") or "").split(",") if m}
    try:
        from utils import inat as I
        species = (await I.user_stats(bot, uid))["species"]
    except Exception:
        species = 0
    r = await execute_db(bot, "SELECT COUNT(*) AS n FROM review_authors WHERE user_id=?", (uid,), fetch=True)
    reviews = r[0]["n"] if r else 0
    reached = {f"inat{m}" for m in MILESTONES_INAT if species >= m} | \
              {f"rev{m}" for m in MILESTONES_REVIEWS if reviews >= m}
    if prefs.get("milestones") is None:
        out.append({"key": "", "scene": "", "silent": True,
                    "commit": {"milestones": ",".join(sorted(reached)) or ","}})
    else:
        order = [f"inat{m}" for m in MILESTONES_INAT] + [f"rev{m}" for m in MILESTONES_REVIEWS]
        new = [k for k in order if k in reached and k not in have]
        if new:
            top = new[-1]
            scene = "reviews" if top.startswith("rev") else "inat"
            n = int(top[3:]) if scene == "reviews" else int(top[4:])
            out.append({"key": f"m-{top}", "scene": scene, "p": {"n": n},
                        "link": "/konto#bewertungen" if scene == "reviews" else "/konto#inat",
                        "commit": {"milestones": ",".join(sorted(have | reached))}})
    return out


def demo(key: str, today: date) -> dict | None:
    """Beispiel-Intro für den Admin-Test (?intro=szene)."""
    if key not in SCENES:
        return None
    p = {"newyear": {"y": today.year + 1}, "aam": {"n": today.year - SERVER_BIRTH.year},
         "botbday": {"n": max(1, today.year - BOT_BIRTH.year)}, "yearreview": {"y": today.year},
         "join": {"n": 3}, "ach": {"n": 1, "e": "🔭", "name": "Weitblick"},
         "inat": {"n": 25}, "reviews": {"n": 100}}.get(key, {})
    link = {"yearreview": "/rueckblick", "ach": "/konto#erfolge"}.get(key, "")
    return {"key": "", "scene": key, "p": p, "link": link}


def parse_cookie(raw: str) -> tuple[str, list[str]]:
    """'YYYYMMDD.key1.key2…' → (Tag des letzten Intros, gesehene Schlüssel)."""
    parts = [p for p in (raw or "").split(".") if p]
    day = parts[0] if parts and parts[0].isdigit() and len(parts[0]) == 8 else ""
    keys = [p for p in parts[1 if day else 0:] if len(p) <= 40 and all(c.isalnum() or c == "-" for c in p)]
    return day, keys


def build_cookie(day: str, keys: list[str], new_key: str) -> str:
    ks = [k for k in keys if k != new_key] + [new_key]
    return ".".join([day] + ks[-15:])
