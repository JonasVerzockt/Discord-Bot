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
utils/season.py – Saison-Verfügbarkeit je Art.

Die Preishistorie kennt nur Preisänderungen, nicht den Lagerstatus. Deshalb wird
hier bei jedem Pipeline-Lauf festgehalten, wie viele Shops eine Art lagernd haben
(Tabelle species_stock_daily, Maximum pro Tag). Angezeigt wird das Profil erst,
wenn mindestens MIN_DAYS_SPAN Tage erfasst sind (ein ganzes Jahr).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from utils.db import execute_db, execute_many
from utils.timez import BERLIN

MIN_DAYS_SPAN = 365        # erst ab einem vollen Jahr Daten anzeigen
MIN_DAYS_MONTH = 10        # Monat zählt erst mit so vielen erfassten Tagen
SHARE_SEASON = 0.5         # Monat gilt als „verfügbar“, wenn an ≥ 50 % der Tage lagernd
SHARE_ALLYEAR = 0.8        # ganzjährig, wenn jeder Monat ≥ 80 %
_MARK = "*"

_cache: dict = {"at": 0.0, "first": None, "days": None}


async def record(bot) -> int:
    """Erfasst den heutigen Lagerstand je Art. Gibt die Zahl lagernder Arten zurück."""
    import asyncio
    from utils import directory as D
    idx = await asyncio.to_thread(D.index, True)
    day = datetime.now(BERLIN).strftime("%Y-%m-%d")
    rows = []
    for key, sp in idx["species"].items():
        n = len({o["domain"] for o in sp["offers"] if o["in_stock"]})
        if n:
            rows.append((day, key, n))
    rows.append((day, _MARK, len(rows)))
    await execute_many(
        bot,
        "INSERT INTO species_stock_daily(day, species_key, shops) VALUES (?,?,?) "
        "ON CONFLICT(day, species_key) DO UPDATE SET shops=MAX(shops, excluded.shops)",
        rows,
    )
    _cache["at"] = 0.0
    return len(rows) - 1


async def _recorded_days(bot) -> list[str]:
    if time.time() - _cache["at"] < 900 and _cache["days"] is not None:
        return _cache["days"]
    r = await execute_db(bot, "SELECT day FROM species_stock_daily WHERE species_key=? ORDER BY day",
                         (_MARK,), fetch=True) or []
    _cache.update(at=time.time(), days=[x["day"] for x in r])
    return _cache["days"]


async def ready(bot) -> bool:
    days = await _recorded_days(bot)
    if not days:
        return False
    first = datetime.strptime(days[0], "%Y-%m-%d").date()
    return (datetime.now(BERLIN).date() - first).days >= MIN_DAYS_SPAN


async def profile(bot, species_key: str) -> dict | None:
    """{'months': [Anteil 0..1 oder None je Monat 1-12], 'kind': 'allyear'|'season'|'rare',
    'ranges': [(von, bis), …]} oder None, solange noch kein volles Jahr erfasst ist."""
    if not await ready(bot):
        return None
    days = await _recorded_days(bot)
    since = (datetime.now(BERLIN).date() - timedelta(days=730)).isoformat()   # max. 2 Jahre
    days = [d for d in days if d >= since]
    r = await execute_db(bot, "SELECT day FROM species_stock_daily WHERE species_key=? AND day>=? "
                              "AND shops>0", (species_key.lower(), since), fetch=True) or []
    have = {x["day"] for x in r}
    tot = [0] * 12
    hit = [0] * 12
    for d in days:
        m = int(d[5:7]) - 1
        tot[m] += 1
        hit[m] += d in have
    months = [(hit[i] / tot[i]) if tot[i] >= MIN_DAYS_MONTH else None for i in range(12)]
    known = [m for m in months if m is not None]
    if known and all(m >= SHARE_ALLYEAR for m in known):
        kind = "allyear"
    elif any(m is not None and m >= SHARE_SEASON for m in months):
        kind = "season"
    else:
        kind = "rare"
    return {"months": months, "kind": kind, "ranges": _ranges(months) if kind == "season" else []}


def _ranges(months: list) -> list[tuple[int, int]]:
    """Zusammenhängende Monatsbereiche (1-12) mit Anteil ≥ SHARE_SEASON, über den
    Jahreswechsel hinweg verbunden (z. B. Nov bis Feb)."""
    on = [m is not None and m >= SHARE_SEASON for m in months]
    if all(on):
        return [(1, 12)]
    start = on.index(False)                 # an einer Lücke beginnen → Wrap sauber
    out, cur = [], None
    for k in range(1, 13):
        i = (start + k) % 12
        if on[i] and cur is None:
            cur = i
        if not on[i] and cur is not None:
            out.append((cur + 1, (i - 1) % 12 + 1))
            cur = None
    if cur is not None:
        out.append((cur + 1, start % 12 + 1 if on[start] else (start - 1) % 12 + 1))
    return sorted(out)


MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
    "eo": ["januaro", "februaro", "marto", "aprilo", "majo", "junio", "julio", "aŭgusto",
           "septembro", "oktobro", "novembro", "decembro"],
}
MONTHS_SHORT = {lg: [m[:3] for m in v] for lg, v in MONTHS.items()}


def describe(prof: dict | None, lang: str) -> str | None:
    """Ein Satz für Discord/Board, z. B. „Meist im Angebot: März bis Juni“."""
    if not prof:
        return None
    from utils.localization import l10n
    names = MONTHS.get(lang) or MONTHS["de"]
    if prof["kind"] == "allyear":
        return l10n.get("season_allyear", lang)
    if prof["kind"] == "rare":
        return l10n.get("season_rare", lang)
    sep = l10n.get("season_to", lang)
    parts = [names[a - 1] if a == b else f"{names[a - 1]} {sep} {names[b - 1]}"
             for a, b in prof["ranges"]]
    return l10n.get("season_range", lang, ranges=", ".join(parts))
