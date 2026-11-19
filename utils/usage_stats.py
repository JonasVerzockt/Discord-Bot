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
utils/usage_stats.py – öffentliche Nutzungsstatistik (Abschnitt „Nutzung“ auf /stats).

Nur Summen, keine Namen/IDs. Grundlage:
  • command_log  → Monatssummen in usage_monthly (Aufrufe, aktive Nutzer, je Befehl).
    Gezählt werden nur Befehle für alle: Admin-Befehle (Discord-Standardrechte oder
    Check admin_or_manage_messages) und Text-Trigger bleiben außen vor.
    Die Summen werden täglich neu berechnet und nie verkleinert (MAX) – so überdauern
    sie die Retention des Command-Logs (COMMAND_LOG_RETENTION_DAYS).
  • Bewertungs-Sheet (geladener Cache) und discount_codes → neue Einträge je Monat.
  • Bestände: aktive Benachrichtigungen, Preis-Tracker, Arten-Beobachtungen.
Zahlen unter MASK_BELOW werden im Front-End als „< 3“ angezeigt (wie bei der Karte).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from utils.db import execute_db, execute_many

logger = logging.getLogger(__name__)

MASK_BELOW = 3
TOP_N = 10


def _admin_command_names(bot) -> set[str]:
    """Qualifizierte Namen aller Admin-Befehle des laufenden Bots."""
    names: set[str] = set()

    def is_admin(cmd) -> bool:
        if getattr(cmd, "default_member_permissions", None) is not None:
            return True
        for chk in getattr(cmd, "checks", []) or []:
            q = getattr(chk, "__qualname__", "")
            if "admin_or_manage_messages" in q or "is_owner" in q:
                return True
        desc = (getattr(cmd, "description", "") or "")
        return desc.startswith("🔒") or desc.startswith("(Admin)")

    def walk(cmd, parent_admin=False):
        adm = parent_admin or is_admin(cmd)
        subs = getattr(cmd, "subcommands", None) or []
        if adm:
            names.add(getattr(cmd, "qualified_name", cmd.name))
        for sc in subs:
            walk(sc, adm)

    for cmd in getattr(bot, "pending_application_commands", []) or getattr(bot, "application_commands", []):
        walk(cmd)
    return names | _LEGACY_ADMIN


# Frühere Namen der Admin-Befehle (vor /bot, /review, /codes_admin, /info_admin,
# /event_admin, /wartung): ältere Einträge im Befehlsprotokoll bleiben so ausgeblendet.
_LEGACY_ADMIN = {
    "status", "system", "stats", "known_users", "command_log", "export",
    "offer_alert check", "pending", "test", "reprocess", "run_jobs", "rescan",
    "reloadshops", "codes_set", "codes_date", "codes_edit", "codes_rescan",
    "codes_fix_links", "info_add", "info_edit", "info_remove", "info_raw",
    "event_pending", "event_approve", "event_reject", "event_exclude",
    "event_edit", "map_refresh", "lieferzeit_backfill",
}


async def aggregate(bot) -> int:
    """command_log → usage_monthly. Rückgabe: Anzahl geschriebener Werte."""
    admin = _admin_command_names(bot)
    rows = await execute_db(
        bot,
        "SELECT substr(created_at,1,7) AS month, command, user_id FROM command_log "
        "WHERE status IS NULL OR status <> 'text'",
        fetch=True) or []
    calls: Counter = Counter()
    per_cmd: Counter = Counter()
    users: dict[str, set] = {}
    for r in rows:
        cmd = (r["command"] or "").strip()
        if not cmd or cmd in admin or cmd.split(" ")[0] in admin:
            continue
        m = r["month"]
        calls[m] += 1
        per_cmd[(m, cmd)] += 1
        users.setdefault(m, set()).add(r["user_id"])
    vals = [(m, "calls", n) for m, n in calls.items()]
    vals += [(m, "users", len(u)) for m, u in users.items()]
    vals += [(m, f"cmd:{c}", n) for (m, c), n in per_cmd.items()]
    if vals:
        await execute_many(
            bot,
            "INSERT INTO usage_monthly (month, metric, value) VALUES (?,?,?) "
            "ON CONFLICT(month, metric) DO UPDATE SET value=MAX(value, excluded.value)",
            vals)
    logger.info("📈 Nutzungsstatistik: %d Monatswerte aktualisiert (%d Admin-Befehle ausgenommen)",
                len(vals), len(admin))
    return len(vals)


def _months(first: str, last: str) -> list[str]:
    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


async def _count(bot, sql: str) -> int | None:
    try:
        r = await execute_db(bot, sql, fetch=True)
        return int(r[0][0]) if r else 0
    except Exception:
        return None        # Tabelle fehlt o. ä. → Kachel entfällt


async def collect(bot) -> dict | None:
    """Daten für den Abschnitt „Nutzung“. None, wenn noch nichts aggregiert wurde."""
    rows = await execute_db(bot, "SELECT month, metric, value FROM usage_monthly", fetch=True) or []
    if not rows:
        return None
    data: dict = {}
    for r in rows:
        data.setdefault(r["month"], {})[r["metric"]] = r["value"]
    now_m = datetime.now(timezone.utc).strftime("%Y-%m")
    months = _months(min(data), now_m)

    # Bewertungen je Monat aus dem geladenen Sheet (Spalte A = TT.MM.JJJJ)
    reviews: Counter = Counter()
    try:
        from utils.sheet import sheet
        for row in (getattr(sheet, "_rows", None) or [])[1:]:
            d = (row[0] if row else "").strip()
            if len(d) == 10 and d[2] == "." and d[5] == ".":
                reviews[f"{d[6:10]}-{d[3:5]}"] += 1
    except Exception:
        pass
    codes: Counter = Counter()
    for r in await execute_db(
            bot, "SELECT substr(message_date,1,7) AS m, COUNT(DISTINCT lower(code)) AS n "
                 "FROM discount_codes GROUP BY m", fetch=True) or []:
        if r["m"]:
            codes[r["m"]] = r["n"]

    top: Counter = Counter()
    for m, mets in data.items():
        for k, v in mets.items():
            if k.startswith("cmd:"):
                top[k[4:]] += v
    top_list = [(c, n) for c, n in top.most_common() if n >= MASK_BELOW][:TOP_N]

    def users_val(m):
        v = data.get(m, {}).get("users", 0)
        return v if v >= MASK_BELOW else None             # < 3 nicht als Kurvenpunkt zeigen

    cur = data.get(now_m, {})
    return {
        "labels": [f"{m[5:7]}/{m[2:4]}" for m in months],
        "calls": [data.get(m, {}).get("calls", 0) for m in months],
        "users": [users_val(m) for m in months],
        "reviews": [reviews.get(m, 0) for m in months],
        "codes": [codes.get(m, 0) for m in months],
        "top": top_list,
        "kpi": {
            "calls_month": cur.get("calls", 0),
            "users_month": cur.get("users", 0),
            "calls_total": sum(v.get("calls", 0) for v in data.values()),
            "notifications": await _count(bot, "SELECT COUNT(*) FROM notifications WHERE status='active'"),
            "price_tracking": await _count(bot, "SELECT COUNT(*) FROM user_price_tracking"),
            "species_watch": await _count(bot, "SELECT COUNT(*) FROM user_species_watch"),
        },
        "since": f"{months[0][5:7]}/{months[0][:4]}",
    }
