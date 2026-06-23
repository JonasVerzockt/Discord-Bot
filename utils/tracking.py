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


# ── Tracking (verarbeitete Nachrichten) ────────────────────────────────────────

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


async def get_all_tracking(bot) -> dict[str, int]:
    """Gibt das komplette Tracking als Dict zurück."""
    rows = await execute_db(bot, "SELECT message_id, sheet_row FROM review_tracking", fetch=True)
    return {r["message_id"]: r["sheet_row"] for r in rows}


async def tracking_count(bot) -> int:
    rows = await execute_db(bot, "SELECT COUNT(*) AS c FROM review_tracking", fetch=True)
    return rows[0]["c"] if rows else 0


# ── Pending (ausstehende Nachrichten) ──────────────────────────────────────────

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
