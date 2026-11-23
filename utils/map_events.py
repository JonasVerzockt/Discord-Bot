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
utils/map_events.py – Termin-Logik der Karten-Events (ohne Discord/Web).

Gemeinsam genutzt von cogs/map.py (Discord-Abläufe, Admin-Freigabe) und
web/features/events.py (Karte, Zusagen, ICS-Kalender):
  • Zeitstempel lesen (gespeichert als naive Berliner Ortszeit),
  • Wiederholungen (RRULE) erzeugen/erkennen, ausgefallene Termine (EXDATE),
  • nächstes Vorkommen und Termin-Ende berechnen.

Vorher doppelt verteilt auf cogs/map.py und cogs/board.py; seit 5.0.0 hier gebündelt.
"""
from datetime import datetime, timedelta, timezone

from utils.timez import BERLIN


try:
    from dateutil.rrule import rrulestr as _rrulestr
    from dateutil.parser import isoparse as _isoparse
except Exception:                                   # dateutil optional -> Serien aus
    _rrulestr = None
    def _isoparse(s): return datetime.fromisoformat(s)


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


_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _rrule_from(kind: str, dt: datetime) -> str | None:
    if kind == "weekly":
        return "FREQ=WEEKLY"
    if kind == "monthly_day":
        return f"FREQ=MONTHLY;BYMONTHDAY={dt.day}"
    if kind == "monthly_weekday":
        ordn = ((dt.day - 1) // 7) + 1
        return f"FREQ=MONTHLY;BYDAY={ordn}{_WEEKDAYS[dt.weekday()]}"
    if kind == "yearly":
        return "FREQ=YEARLY"
    return None


def _recur_code(rrule) -> str:
    """Gespeicherte RRULE -> Code der Wiederholungs-Auswahl (für Anzeige/Neuberechnung)."""
    r = (rrule or "").upper()
    if "FREQ=WEEKLY" in r:
        return "weekly"
    if "FREQ=MONTHLY" in r:
        return "monthly_weekday" if "BYDAY" in r else "monthly_day"
    if "FREQ=YEARLY" in r:
        return "yearly"
    return "none"


def _row_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("T", " ")) if s else None
    except ValueError:
        return None
