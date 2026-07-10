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
utils/achievements.py – Erfolge-System (Registry + Auswertung).

Alles wird bei Abfrage frisch aus vorhandenen Tabellen + user_events berechnet.
Freischaltungen werden in `achievements` persistiert (Datum + versteckte bleiben
aufgedeckt). Titel/Beschreibungen liegen als l10n-Keys vor: ach_<id>_t / ach_<id>_d.
"""
import logging

from utils.db import execute_db
from utils.localization import l10n

logger = logging.getLogger(__name__)


class Ach:
    __slots__ = ("id", "emoji", "hidden", "cat", "check")

    def __init__(self, id, emoji, hidden, cat, check):
        self.id = id
        self.emoji = emoji
        self.hidden = hidden
        self.cat = cat          # "avail" | "price" | "community" | "hidden"
        self.check = check      # stats -> (current, target)


# Reihenfolge = Anzeige-Reihenfolge
ACHIEVEMENTS = [
    Ach("first_notif", "🔔", False, "avail",     lambda s: (min(s["notif"], 1), 1)),
    Ach("collector",   "📋", False, "avail",     lambda s: (s["notif"], 10)),
    Ach("bought",      "🛒", False, "avail",     lambda s: (min(s["notif_completed"], 1), 1)),
    Ach("variety",     "🌈", False, "avail",     lambda s: (s["notif_species"], 10)),
    Ach("price_first", "📉", False, "price",     lambda s: (min(s["tracked"], 1), 1)),
    Ach("price_pro",   "📊", False, "price",     lambda s: (s["tracked"], 10)),
    Ach("target_first","🎯", False, "price",     lambda s: (min(s["targets"], 1), 1)),
    Ach("watch_first", "🔭", False, "price",     lambda s: (min(s["watches"], 1), 1)),
    Ach("digest_sub",  "📬", False, "community", lambda s: (min(s["digest"], 1), 1)),
    Ach("code_bringer_1","🏷️", False, "community", lambda s: (min(s["codes"], 1), 1)),
    Ach("code_bringer_2","🏷️", False, "community", lambda s: (s["codes"], 5)),
    Ach("code_bringer_3","🏷️", False, "community", lambda s: (s["codes"], 15)),
    # Versteckte
    Ach("night_owl",   "🦉", True,  "hidden",    lambda s: (1 if s["night"] else 0, 1)),
    Ach("explorer",    "🧭", True,  "hidden",    lambda s: (s["distinct_cmds"], 8)),
    Ach("bargain",     "⚡", True,  "hidden",    lambda s: (1 if s["target_hit"] else 0, 1)),
    Ach("genus_hunter","🧬", True,  "hidden",    lambda s: (min(s["watch_genus"], 1), 1)),
    # Meta (wird gesondert berechnet, check bekommt s["_visible_unlocked"]/s["_visible_total"])
    Ach("all_visible", "🐜", True,  "hidden",    lambda s: (s["_visible_unlocked"], s["_visible_total"])),
]

_VISIBLE_TOTAL = sum(1 for a in ACHIEVEMENTS if not a.hidden)


async def _count(bot, sql, params) -> int:
    rows = await execute_db(bot, sql, params, fetch=True)
    if not rows:
        return 0
    val = rows[0][0]
    return int(val) if val is not None else 0


async def gather_stats(bot, user_id: str, username: str | None = None) -> dict:
    """Sammelt alle für die Erfolge nötigen Kennzahlen eines Users."""
    uid = str(user_id)
    s = {}
    s["notif"]           = await _count(bot, "SELECT COUNT(*) FROM notifications WHERE user_id=?", (uid,))
    s["notif_completed"] = await _count(bot, "SELECT COUNT(*) FROM notifications WHERE user_id=? AND status='completed'", (uid,))
    s["notif_species"]   = await _count(bot, "SELECT COUNT(DISTINCT species) FROM notifications WHERE user_id=?", (uid,))
    s["tracked"]         = await _count(bot, "SELECT COUNT(*) FROM user_price_tracking WHERE user_id=?", (uid,))
    s["targets"]         = await _count(bot, "SELECT COUNT(*) FROM user_price_tracking WHERE user_id=? AND target_price IS NOT NULL", (uid,))
    s["watches"]         = await _count(bot, "SELECT COUNT(*) FROM user_species_watch WHERE user_id=?", (uid,))
    s["watch_genus"]     = await _count(bot, "SELECT COUNT(*) FROM user_species_watch WHERE user_id=? AND is_genus=1", (uid,))
    s["digest"]          = await _count(bot, "SELECT COUNT(*) FROM digest_subscribers WHERE user_id=?", (uid,))
    s["codes"]           = (await _count(bot, "SELECT COUNT(*) FROM discount_codes WHERE author=?", (username,))) if username else 0

    ev_rows = await execute_db(bot, "SELECT event, ts FROM user_events WHERE user_id=?", (uid,), fetch=True) or []
    cmds, night, target_hit = set(), False, False
    for r in ev_rows:
        ev = r["event"] if not isinstance(r, (tuple, list)) else r[0]
        ts = r["ts"]    if not isinstance(r, (tuple, list)) else r[1]
        if ev == "target_hit":
            target_hit = True
        elif ev.startswith("cmd:"):
            cmds.add(ev)
        # ts-Format: 'YYYY-MM-DD HH:MM:SS' (UTC von SQLite)
        try:
            hour = int(str(ts)[11:13])
            if hour in (2, 3):
                night = True
        except (ValueError, IndexError):
            pass
    s["distinct_cmds"] = len(cmds)
    s["night"]         = night
    s["target_hit"]    = target_hit
    return s


def evaluate(stats: dict) -> list:
    """
    Wertet alle Erfolge aus.
    Rückgabe: [(ach, current, target, unlocked_bool), …] in Anzeige-Reihenfolge.
    """
    results = []
    visible_unlocked = 0
    # erst alle außer Meta
    for a in ACHIEVEMENTS:
        if a.id == "all_visible":
            continue
        cur, tgt = a.check(stats)
        cur = min(cur, tgt)
        unlocked = cur >= tgt
        if unlocked and not a.hidden:
            visible_unlocked += 1
        results.append([a, cur, tgt, unlocked])
    # Meta zuletzt
    stats["_visible_unlocked"] = visible_unlocked
    stats["_visible_total"]    = _VISIBLE_TOTAL
    meta = next(a for a in ACHIEVEMENTS if a.id == "all_visible")
    cur, tgt = meta.check(stats)
    results.append([meta, cur, tgt, cur >= tgt])
    # Reihenfolge wie ACHIEVEMENTS
    order = {a.id: i for i, a in enumerate(ACHIEVEMENTS)}
    results.sort(key=lambda r: order[r[0].id])
    return results


async def log_event(bot, user_id, event: str) -> None:
    """Schreibt ein leichtes Event (z.B. 'cmd:track_price', 'target_hit')."""
    try:
        await execute_db(
            bot, "INSERT INTO user_events (user_id, event) VALUES (?, ?)",
            (str(user_id), event), commit=True,
        )
    except Exception as e:
        logger.debug("log_event failed: %s", e)


async def check_and_grant(bot, user, lang: str = "en") -> list:
    """
    Wertet aus, persistiert NEU freigeschaltete Erfolge und schickt pro neuem
    Erfolg eine dezente DM. Gibt die Auswertung (für /achievements) zurück.
    """
    uid = str(user.id)
    stats   = await gather_stats(bot, uid, getattr(user, "name", None))
    results = evaluate(stats)

    rows = await execute_db(bot, "SELECT achievement_id FROM achievements WHERE user_id=?", (uid,), fetch=True) or []
    have = {r["achievement_id"] if not isinstance(r, (tuple, list)) else r[0] for r in rows}

    for a, cur, tgt, unlocked in results:
        if unlocked and a.id not in have:
            await execute_db(
                bot, "INSERT OR IGNORE INTO achievements (user_id, achievement_id) VALUES (?, ?)",
                (uid, a.id), commit=True,
            )
            try:
                title = l10n.get(f"ach_{a.id}_t", lang)
                desc  = l10n.get(f"ach_{a.id}_d", lang)
                await user.send(l10n.get("ach_new_dm", lang, emoji=a.emoji, title=title, desc=desc))
            except Exception:
                pass  # DMs gesperrt o.ä. – Freischaltung bleibt trotzdem
    return results
