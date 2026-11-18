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
tools/fix_shop_mapping.py – repariert data/shop_mapping.csv (einmalig).

Was passiert:
  1. Verklebte Zeilen trennen (entstanden, wenn die Datei ohne Zeilenumbruch
     endete und der Bot die nächste Zeile direkt angehängt hat).
  2. Identifier auf die erste Zeile kürzen, Mentions zu nackten IDs.
  3. Shopnamen ohne https:// und ohne www. (antpire.net statt https://www.antpire.net/).
  4. Bekannte Fehlzuordnungen korrigieren (FIXES unten), antcube → antstore.net.
  5. Doppelte Identifier (Groß-/Kleinschreibung egal) zusammenführen.
  6. Auto-gelernte Einträge, die nicht zum Namen passen, nur MELDEN.

Bot vorher stoppen. Ohne --apply wird nur angezeigt, was sich ändern würde.

    python3 tools/fix_shop_mapping.py            # Probelauf
    python3 tools/fix_shop_mapping.py --apply    # schreiben (mit Backup)
"""
import csv
import io
import re
import sys
import shutil
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MAPPING_FILE                                    # noqa: E402
from utils.shop import (                                           # noqa: E402
    _normalize_identifier, clean_shop_name, is_plausible, _CSV_COLS,
)

# Fehlzuordnungen (alle landeten bei "ants from asia/exoticantstore")
FIXES = {
    "special ants":        "special-ants.store",
    "home for ants":       "homeforants.de",
    "esthetic ants":       "estheticants.com",
    "ants invasion":       "antsinvasion.pl",
    "ants of switzerland": "antsofswitzerland.ch",
    "914837804893216779":  "antstore.net",   # Discord-Konto antcube = Antstore
    "antcube":             "antstore.net",
    "antcube.de":          "antstore.net",
}

_HINT_SPLIT = re.compile(
    r"^(auto-gelernt|manuell gesetzt|Beispiel-Eintrag|\d{2}\.\d{2}\.\d{4}|prüfen: [^']*'[^']*')(.+)$"
)


def _split_glued(rec: list[str]) -> list[list[str]]:
    """Eine CSV-Zeile mit mehr als 4 Feldern in echte Zeilen zerlegen."""
    out = []
    while len(rec) > 4:
        m = _HINT_SPLIT.match(rec[3])
        if m:                                   # "auto-gelerntSpecial Ants"
            out.append(rec[:3] + [m.group(1)])
            rec = [m.group(2)] + rec[4:]
        else:                                   # Hinweis war leer
            out.append(rec[:3] + [""])
            rec = rec[3:]
    out.append(rec + [""] * (4 - len(rec)))
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    path = Path(MAPPING_FILE)
    if not path.exists():
        print(f"Nicht gefunden: {path}")
        return 1

    records = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    body = records[1:] if records and records[0][:1] == ["identifier"] else records

    rows, glued = [], 0
    for rec in body:
        if not any(x.strip() for x in rec):
            continue
        parts = _split_glued(rec)
        if len(parts) > 1:
            glued += 1
            print(f"✂️  verklebt → {len(parts)} Zeilen: {rec}")
        rows.extend(parts)

    merged: dict[str, dict] = {}
    changes, warn = [], []
    for ident_raw, shop_raw, mid, hint in rows:
        ident = _normalize_identifier(ident_raw)
        if not ident:
            continue
        key = ident.casefold()
        shop = clean_shop_name(shop_raw)
        if shop != shop_raw.strip():
            changes.append(f"🧹 {ident}: '{shop_raw}' → '{shop}'")
        if key in FIXES and shop != FIXES[key]:
            changes.append(f"🔧 {ident}: '{shop or '(leer)'}' → '{FIXES[key]}'")
            shop, hint = FIXES[key], "manuell korrigiert"
        if ident != ident_raw.strip():
            changes.append(f"✂️  Identifier '{ident_raw.strip()[:60]}' → '{ident}'")
        new = {"identifier": ident, "shop_url": shop, "message_id": mid, "hinweis": hint}
        old = merged.get(key)
        if old is None:
            merged[key] = new
            continue
        # Duplikat: Eintrag mit Shop gewinnt, FIX gewinnt immer
        if key in FIXES or (not old["shop_url"] and shop):
            merged[key] = new
        if old["shop_url"] and shop and old["shop_url"] != shop and key not in FIXES:
            warn.append(f"⚠️  Doppelt mit verschiedenen Shops: '{ident}': "
                        f"'{old['shop_url']}' / '{shop}' (erster bleibt)")
        changes.append(f"🗑️  Duplikat entfernt: '{ident}'")

    for r in merged.values():
        if (r["shop_url"] and "auto" in (r["hinweis"] or "")
                and not r["identifier"].isdigit()
                and not is_plausible(r["identifier"], r["shop_url"])):
            warn.append(f"❓ Bitte prüfen: '{r['identifier']}' → '{r['shop_url']}'")

    print(f"\n{len(rows)} Zeilen gelesen, {glued} verklebte repariert, "
          f"{len(merged)} Einträge danach.")
    for c in changes:
        print(c)
    for w in warn:
        print(w)

    if not apply:
        print("\nProbelauf – nichts geschrieben. Mit --apply übernehmen.")
        return 0

    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in merged.values():
            w.writerow(r)
    print(f"\n✅ Geschrieben: {path}\n   Backup:     {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
