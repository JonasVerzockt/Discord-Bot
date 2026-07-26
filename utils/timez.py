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
utils/timez.py – Zentrale Zeitzonen-Hilfen.

Intern rechnet/speichert der Bot in UTC; für die *Anzeige* werden Zeitstempel
hiermit einheitlich in Europe/Berlin (MEZ/MESZ, automatische Sommerzeit)
umgerechnet. Nutzt die Standardbibliothek (zoneinfo), keine Fremdpakete.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

_DEFAULT_FMT = "%d.%m.%Y %H:%M %Z"


def _fmt_berlin(dt: datetime, fmt: str) -> str:
    """Formatiert eine (tz-bewusste) Berliner Zeit und stellt sicher, dass die
    Zeitzone klar als deutsches Kürzel MEZ/MESZ erscheint.

    - Enthält fmt ein %Z, wird das (locale-abhängige) CET/CEST durch MEZ/MESZ ersetzt.
    - Enthält fmt kein %Z, wird das passende Kürzel angehängt.
    So ist in JEDER in Discord geposteten Zeitausgabe erkennbar, dass es sich um
    MEZ (Winter) bzw. MESZ (Sommer) handelt."""
    label = "MESZ" if dt.dst() else "MEZ"
    s = dt.strftime(fmt)
    if "%Z" in fmt:
        return s.replace("CEST", "MESZ").replace("CET", "MEZ")
    return f"{s} {label}"


def now_berlin(fmt: str = _DEFAULT_FMT) -> str:
    """Aktuelle Zeit in Berliner Zeit als String (inkl. MEZ/MESZ-Kennzeichnung)."""
    return _fmt_berlin(datetime.now(BERLIN), fmt)


def berlin_from_iso(iso, fmt: str = _DEFAULT_FMT) -> str | None:
    """ISO-Zeitstempel (z.B. '2026-07-16T14:00:00+00:00' oder '…Z') → Berliner Zeit.
    Naive Angaben werden als UTC interpretiert. Rückgabe None bei Parse-Fehler."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _fmt_berlin(dt.astimezone(BERLIN), fmt)


def berlin_from_utc_naive(value, in_fmt: str, fmt: str = _DEFAULT_FMT) -> str:
    """Als UTC gespeicherten *naiven* Zeitstempel (Format in_fmt) → Berliner Zeit.
    Bei Parse-Fehler oder leerem Wert wird der Originalwert (als String) zurückgegeben."""
    if not value:
        return "" if value is None else str(value)
    try:
        dt = datetime.strptime(str(value), in_fmt).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(value)
    return _fmt_berlin(dt.astimezone(BERLIN), fmt)
