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
utils/map_store.py – Datenzugriff der Halter-Karte (Einträge, Zusatzregionen, Zusagen, Events).

Gemeinsam genutzt von cogs/map.py (Befehle) und cogs/map_tasks.py (Austritt, Ablauf,
Aufräumen) – vorher importierte map_tasks diese Funktionen direkt aus dem Map-Cog.
"""
from utils import geo
from utils.db import execute_db


MAX_EXTRA_REGIONS = 2


async def _load_extra_regions(bot, uid) -> list[tuple[str, str]]:
    rows = await execute_db(bot, "SELECT country, region_code FROM map_entry_regions WHERE user_id=?",
                            (str(uid),), fetch=True) or []
    return [(r["country"], r["region_code"]) for r in rows]


async def _save_extra_regions(bot, uid, pairs) -> None:
    uid = str(uid)
    await execute_db(bot, "DELETE FROM map_entry_regions WHERE user_id=?", (uid,), commit=True)
    valid = geo.all_regions()
    for cc, code in list(pairs)[:MAX_EXTRA_REGIONS]:
        if code in valid.get(cc, {}):
            await execute_db(bot,
                "INSERT OR IGNORE INTO map_entry_regions (user_id, country, region_code) VALUES (?,?,?)",
                (uid, cc, code), commit=True)


def _region_label(cc: str, code: str) -> str:
    return geo.all_regions().get(cc, {}).get(code, code)


async def _delete_entry(bot, uid):
    uid = str(uid)
    await execute_db(bot, "DELETE FROM map_entry_tags WHERE user_id=?", (uid,), commit=True)
    await execute_db(bot, "DELETE FROM map_entry_regions WHERE user_id=?", (uid,), commit=True)
    await execute_db(bot, "DELETE FROM map_entries WHERE user_id=?", (uid,), commit=True)


async def _delete_rsvps(bot, uid):
    """Alle Teilnahme-Angaben einer Person löschen (z. B. beim Server-Austritt)."""
    await execute_db(bot, "DELETE FROM map_event_rsvp WHERE user_id=?", (str(uid),), commit=True)


async def _event_row(bot, eid):
    rows = await execute_db(bot, "SELECT * FROM map_events WHERE id=?", (int(eid),), fetch=True)
    return rows[0] if rows else None
