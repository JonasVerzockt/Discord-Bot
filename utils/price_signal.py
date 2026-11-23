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
utils/price_signal.py – Preis-Ampel je Angebot.

Vergleicht den aktuellen Preis eines Produkts mit SEINEM EIGENEN Preis der letzten
12 Monate (gleiche Koloniegröße, gleiche Währung, kein Wechselkurs-Rauschen).
Quelle: product_price_history (Grabber, speichert nur Preisänderungen).

  low  🟢  im unteren Drittel der 12-Monats-Spanne (oder Tiefstpreis)
  mid  🟡  dazwischen
  high 🔴  im oberen Drittel (oder Höchstpreis)

Keine Ampel, wenn die Historie kürzer als MIN_SPAN_DAYS ist oder der Preis sich
in 12 Monaten nicht nennenswert bewegt hat.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from utils.directory import PRICE_HISTORY_DB

MIN_SPAN_DAYS = 90
MIN_SPREAD = 0.5            # Spanne mindestens 0,50 (Währungseinheit) …
MIN_SPREAD_REL = 0.05       # … und mindestens 5 % des Höchstpreises
EMOJI = {"low": "🟢", "mid": "🟡", "high": "🔴"}


def signals(pids, now: datetime | None = None) -> dict:
    """{product_id: 'low'|'mid'|'high'} für alle Produkte mit belastbarer Historie."""
    pids = [int(p) for p in {p for p in pids if p is not None}]
    if not pids or not PRICE_HISTORY_DB.exists():
        return {}
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)   # Grabber speichert UTC ohne Zone
    w0 = now - timedelta(days=365)
    rows = []
    con = sqlite3.connect(f"file:{PRICE_HISTORY_DB}?mode=ro", uri=True)
    try:
        for i in range(0, len(pids), 800):
            part = pids[i:i + 800]
            rows += con.execute(
                f"SELECT product_id, recorded_at, min_price, currency_iso FROM product_price_history "
                f"WHERE product_id IN ({','.join('?' * len(part))}) ORDER BY product_id, recorded_at",
                part).fetchall()
    finally:
        con.close()
    hist: dict = {}
    for pid, ts, mn, cur in rows:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", ""))
        except ValueError:
            continue
        if mn is None or float(mn) <= 0:
            continue
        hist.setdefault(pid, []).append((dt, float(mn), cur or "EUR"))
    out = {}
    for pid, pts in hist.items():
        r = classify(pts, now, w0)
        if r:
            out[pid] = r
    return out


def classify(pts: list, now: datetime, w0: datetime) -> str | None:
    if not pts or (now - pts[0][0]).days < MIN_SPAN_DAYS:
        return None
    cur_price, cur_cur = pts[-1][1], pts[-1][2]
    vals = []
    before = None
    for dt, v, c in pts:
        if c != cur_cur:
            continue
        if dt < w0:
            before = v                       # Preis, der zu Fensterbeginn galt
        else:
            vals.append(v)
    if before is not None:
        vals.append(before)
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    if hi - lo < max(MIN_SPREAD, hi * MIN_SPREAD_REL):
        return None
    pos = (cur_price - lo) / (hi - lo)
    if pos <= 1 / 3:
        return "low"
    if pos >= 2 / 3:
        return "high"
    return "mid"
