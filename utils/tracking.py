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
utils/tracking.py – SQLite-basiertes Tracking für verarbeitete und ausstehende Nachrichten.

Ersetzt die alten JSON-Flatfiles (processed_messages.json / pending_messages.json).

Tabellen:
  review_tracking  → {message_id: sheet_row_num}
  review_pending   → {message_id: reason, identifier}

Verwendung:
    from utils.tracking import (
        get_tracking, set_tracking, get_all_tracking,
        get_pending, set_pending, remove_pending, get_all_pending,
    )
"""
import logging
from utils.db import execute_db

logger = logging.getLogger(__name__)


# ── Tracking (verarbeitete Nachrichten) ───────────────────────────────────────

async def get_tracking(bot, message_id: str) -> int | None:
    """Gibt die Sheet-Zeilennummer für eine Nachrichten-ID zurück oder None."""
    rows = await execute_db(
        bot,
        "SELECT sheet_row FROM review_tracking WHERE message_id=?",
        (message_id,),
        fetch=True,
    )
    return rows[0]["sheet_row"] if rows else None


async def set_tracking(bot, message_id: str, sheet_row: int) -> None:
    """Speichert oder aktualisiert das Tracking für eine Nachricht."""
    await execute_db(
        bot,
        "INSERT OR REPLACE INTO review_tracking (message_id, sheet_row) VALUES (?, ?)",
        (message_id, sheet_row),
        commit=True,
    )


async def remove_tracking(bot, message_id: str) -> None:
    """Entfernt das Tracking (Message -> Sheet-Zeile) fuer eine Nachricht."""
    await execute_db(
        bot,
        "DELETE FROM review_tracking WHERE message_id=?",
        (message_id,),
        commit=True,
    )


async def get_all_tracking(bot) -> dict[str, int]:
    """Gibt das komplette Tracking als Dict zurück."""
    rows = await execute_db(bot, "SELECT message_id, sheet_row FROM review_tracking", fetch=True)
    return {r["message_id"]: r["sheet_row"] for r in rows}


async def tracking_count(bot) -> int:
    rows = await execute_db(bot, "SELECT COUNT(*) AS c FROM review_tracking", fetch=True)
    return rows[0]["c"] if rows else 0


# ── Pending (ausstehende Nachrichten) ─────────────────────────────────────────

async def get_pending(bot, message_id: str) -> dict | None:
    """Gibt den Pending-Eintrag für eine Nachrichten-ID zurück oder None."""
    rows = await execute_db(
        bot,
        "SELECT reason, identifier FROM review_pending WHERE message_id=?",
        (message_id,),
        fetch=True,
    )
    return dict(rows[0]) if rows else None


async def set_pending(bot, message_id: str, reason: str, identifier: str = "") -> None:
    """Speichert oder aktualisiert einen Pending-Eintrag."""
    await execute_db(
        bot,
        "INSERT OR REPLACE INTO review_pending (message_id, reason, identifier) VALUES (?, ?, ?)",
        (message_id, reason, identifier),
        commit=True,
    )


async def remove_pending(bot, message_id: str) -> None:
    """Entfernt einen Pending-Eintrag."""
    await execute_db(
        bot,
        "DELETE FROM review_pending WHERE message_id=?",
        (message_id,),
        commit=True,
    )


async def get_all_pending(bot) -> dict[str, dict]:
    """Gibt alle Pending-Einträge als Dict zurück."""
    rows = await execute_db(bot, "SELECT message_id, reason, identifier FROM review_pending", fetch=True)
    return {r["message_id"]: {"reason": r["reason"], "identifier": r["identifier"]} for r in rows}


async def pending_count(bot) -> int:
    rows = await execute_db(bot, "SELECT COUNT(*) AS c FROM review_pending", fetch=True)
    return rows[0]["c"] if rows else 0
