#!/usr/bin/env python3
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
tools/fix_review_sheet.py – korrigiert die Spalte "Name des Shops" im
Bewertungs-Sheet (einmalig).

  1. Falsch zugeordnete Zeilen (FIXES) – nur wenn Datum UND alter Wert noch
     stimmen, sonst wird die Zeile übersprungen und gemeldet.
  2. Alle Shopnamen ohne https:// und ohne www. (clean_shop_name);
     dabei wird antcube.de immer zu antstore.net (SHOP_ALIASES).

Bot vorher stoppen (er liest das Sheet nur beim Start) und danach starten.

    python3 tools/fix_review_sheet.py            # Probelauf
    python3 tools/fix_review_sheet.py --apply    # schreiben
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.sheet import sheet                 # noqa: E402
from utils.shop import clean_shop_name        # noqa: E402

COL = 2                                        # C = "Name des Shops"
WRONG = "ants from asia/exoticantstore"
# Zeile: (Datum, richtiger Shop) – geprüft gegen review_tracking + Discord-Nachricht
FIXES = {
    741: ("10.08.2026", "estheticants.com"),
    744: ("12.08.2026", "antsinvasion.pl"),
    753: ("31.08.2026", "antsofswitzerland.ch"),
    755: ("03.09.2026", "special-ants.store"),
    758: ("08.09.2026", "special-ants.store"),
    775: ("07.10.2026", "special-ants.store"),
    776: ("07.10.2026", "homeforants.de"),
}


def main() -> int:
    apply = "--apply" in sys.argv
    rows = sheet.ws.get_all_values()
    if not rows or len(rows[0]) <= COL or "shop" not in rows[0][COL].lower():
        print(f"Spalte C ist nicht 'Name des Shops': {rows[0][:4] if rows else '-'}")
        return 1

    updates = {}
    for num, (date, shop) in FIXES.items():
        r = rows[num - 1] if num <= len(rows) else []
        cur_date = (r[0] if r else "").strip()
        cur_shop = (r[COL] if len(r) > COL else "").strip()
        if cur_date != date or clean_shop_name(cur_shop) not in (WRONG, shop):
            print(f"⏭️  Zeile {num} übersprungen: ist '{cur_date}' / '{cur_shop}', "
                  f"erwartet '{date}' / '{WRONG}'")
            continue
        if cur_shop != shop:
            updates[num] = (cur_shop, shop)

    for num, r in enumerate(rows[1:], start=2):
        if num in updates or num in FIXES or len(r) <= COL:
            continue
        cur = r[COL].strip()
        new = clean_shop_name(cur)
        if cur and new != cur:
            updates[num] = (cur, new)

    for num in sorted(updates):
        old, new = updates[num]
        tag = "🔧" if num in FIXES else "🧹"
        print(f"{tag} Zeile {num}: '{old}' → '{new}'")
    print(f"\n{len(updates)} Zellen zu ändern.")

    if not apply:
        print("Probelauf – nichts geschrieben. Mit --apply übernehmen.")
        return 0
    if updates:
        sheet.ws.batch_update(
            [{"range": f"C{n}", "values": [[new]]} for n, (_, new) in sorted(updates.items())],
            value_input_option="RAW",
        )
    print("✅ Sheet aktualisiert. Jetzt den Bot starten.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
