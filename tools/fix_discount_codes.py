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
tools/fix_discount_codes.py – korrigiert bekannte Fehler in der Tabelle
discount_codes (einmalig, abgeglichen mit #rabatt-codes und #handel-gewerblich).

Bot vorher stoppen. Ohne --apply nur Probelauf; mit --apply wird vorher eine
Sicherung der Datenbank angelegt (data/antcheckbot.db.bak-<Zeit>).

    python3 tools/fix_discount_codes.py            # Probelauf
    python3 tools/fix_discount_codes.py --apply    # schreiben
"""
import re
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import DB_FILE   # noqa: E402

AM  = ("AntMeUp", "https://antmeup.de/")
FF  = ("Fühler-Fabrik", "https://www.fuehler-fabrik.de/shop")
HFA = ("HomeforAnts", "https://homeforants.de/")
AI  = ("Ants Included", "https://ants-included.com/")
SA  = ("Special Ants", "https://special-ants.store/")


def shop_set(name_url):
    return {"shop": name_url[0], "shop_url": name_url[1]}


# (Beschreibung, Code, WHERE-Zusatz, Parameter, Aktion)
# Aktion: dict = UPDATE-Felder, "DELETE" = löschen
FIXES = [
    ("AntCorner AAM5: 15.12. ist Bestellfrist, kein Enddatum",
     "AAM5", "(shop_url LIKE '%antcorner%' OR lower(shop) LIKE '%antcorner%')", (),
     {"valid_until": None, "is_permanent": 0}),
    ("AntMeUp HERBST: gültig 07.10. bis 25.10.2026",
     "HERBST", "(shop_url LIKE '%antmeup%' OR lower(shop) LIKE '%antmeup%')", (),
     {"valid_from": "2026-10-07", "valid_until": "2026-10-25", "is_permanent": 0}),
    ("Antpire AutumnSale: bis Ende November",
     "AutumnSale", "(shop_url LIKE '%antpire%' OR lower(shop) LIKE '%antpire%')", (),
     {"valid_until": "2026-11-30", "is_permanent": 0}),
    ("Antpire LeafcutterMadness: keine Prozentangabe in der Nachricht",
     "LeafcutterMadness", "1", (), {"discount": ""}),
    ("AAM10 'Ameisenkolonie-Shop' / 'Antshop' = AntMeUp, abgelaufen 06.07.2025",
     "AAM10", "(lower(shop) LIKE '%ameisenkolonie%' OR lower(shop) = 'antshop')", (),
     {**shop_set(AM), "discount": "15%", "is_permanent": 0, "valid_until": "2025-07-06"}),
    ("Fühler-Fabrik KEIN VERSAND: Aktion von 2023, nicht dauerhaft",
     "KEIN VERSAND", "1", (), {"is_permanent": 0, "valid_until": None}),
    ("Special Ants EM2024: Kundenanmerkung, kein Rabattcode",
     "EM2024", "1", (), "DELETE"),
    ("AAM10 ohne Shop ('?', 'Finn Kraus', 'Unbekannt' vom 07.04.2024) = Ants Included",
     "AAM10", "shop_url = '' AND (shop IN ('', '?') OR lower(shop) LIKE 'finn%' "
              "OR (lower(shop) = 'unbekannt' AND message_date LIKE '2024-04-07%'))", (),
     {**shop_set(AI), "min_order": "25€"}),
    ("Ants Included AntsForAll ohne Link: ein Shop",
     "AntsForAll", "shop_url = ''", (), shop_set(AI)),
    ("HomeforAnts AAM vor 07/2026 war der Link /aam (5 %), kein Code",
     "AAM", "(shop_url LIKE '%homeforants%' OR lower(shop) LIKE '%homeforants%') "
            "AND message_date < '2026-07-01'", (), "DELETE_IF_NEWER"),
    ("HomeforAnts HomeForAnts (5 €): 2024 auf 10 Einlösungen begrenzt",
     "HomeForAnts", "1", (), {"is_permanent": 0, "valid_until": None}),
    ("GutSch2023 = MyAnts", "GutSch2023", "1", (),
     {"shop": "MyAnts", "shop_url": "https://www.myants.de/"}),
    ("NÄRRISCHE-ZEIT = Fühler-Fabrik", "NÄRRISCHE-ZEIT", "1", (), shop_set(FF)),
    ("SPORTSCHÜTZEN = Fühler-Fabrik", "SPORTSCHÜTZEN", "1", (), shop_set(FF)),
    ("2YEARSOFPASSION ohne Link = Fühler-Fabrik", "2YEARSOFPASSION", "shop_url = ''", (), shop_set(FF)),
    ("Valentinstag2024 = HomeforAnts", "Valentinstag2024", "1", (), shop_set(HFA)),
    ("Special15 ('Shopname'/'?') = HomeforAnts", "Special15", "1", (), shop_set(HFA)),
    ("MERCHANT10 ohne Shop = Antmerchant", "MERCHANT10", "shop_url = ''", (),
     {"shop": "Antmerchant", "shop_url": "https://antmerchant.com/"}),
    ("tt25 = AntDealer", "tt25", "1", (),
     {"shop": "AntDealer", "shop_url": "https://www.antdealer.com/"}),
    ("summer20 ohne Link = Special Ants", "summer20", "shop_url = ''", (), shop_set(SA)),
    ("Bavariants de.estheticants.com = Esthetic Ants", "Bavariants",
     "shop_url LIKE '%estheticants%'", (),
     {"shop": "Esthetic Ants", "shop_url": "https://www.estheticants.com/"}),
    ("German-Ants ERÖFFNUNG10: kein Code genannt", "ERÖFFNUNG10", "1", (), "DELETE"),
    ("German-Ants ERÖFFNUNG15: kein Code genannt", "ERÖFFNUNG15", "1", (), "DELETE"),
]

_REGIONAL = re.compile("[\U0001F1E6-\U0001F1FF]")
COLS = "id, message_id, shop, shop_url, code, discount, valid_from, valid_until, is_permanent, min_order, message_date"


def show(r) -> str:
    return (f"    #{r['id']} {r['message_date'] or '?':16} {r['shop'] or '?'} | {r['code']} | "
            f"{r['discount'] or '?'} | bis {r['valid_until'] or '-'} | "
            f"{'dauerhaft' if r['is_permanent'] else ''} | {r['shop_url'] or ''}")


def main() -> int:
    apply = "--apply" in sys.argv
    db = Path(DB_FILE)
    if not db.exists():
        print(f"Nicht gefunden: {db}")
        return 1
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    if apply:
        bak = db.with_name(f"{db.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        with sqlite3.connect(bak) as b:
            con.backup(b)
        print(f"💾 Sicherung: {bak}\n")

    total = 0
    for desc, code, where, params, action in FIXES:
        rows = con.execute(
            f"SELECT {COLS} FROM discount_codes WHERE lower(code)=lower(?) AND {where}",
            (code, *params)).fetchall()
        print(f"{'🗑️ ' if 'DELETE' in str(action) else '🔧'} {desc}: {len(rows)} Zeile(n)")
        for r in rows:
            print(show(r))
        if not rows:
            continue
        ids = [r["id"] for r in rows]
        q = ",".join("?" * len(ids))
        if action == "DELETE_IF_NEWER":
            newer = con.execute(
                "SELECT count(*) FROM discount_codes WHERE lower(code)='aam' "
                "AND (shop_url LIKE '%homeforants%' OR lower(shop) LIKE '%homeforants%') "
                "AND message_date >= '2026-07-01'").fetchone()[0]
            if newer:
                sql, args = f"DELETE FROM discount_codes WHERE id IN ({q})", ids
            else:
                print("    → kein neuerer AAM-Eintrag vorhanden, setze stattdessen 10 % + dauerhaft")
                sql = f"UPDATE discount_codes SET discount='10%', is_permanent=1 WHERE id IN ({q})"
                args = ids
        elif action == "DELETE":
            sql, args = f"DELETE FROM discount_codes WHERE id IN ({q})", ids
        else:
            sets = ", ".join(f"{k}=?" for k in action)
            sql, args = f"UPDATE discount_codes SET {sets} WHERE id IN ({q})", [*action.values(), *ids]
        total += len(rows)
        try:
            con.execute(sql, args)
        except sqlite3.IntegrityError as e:
            print(f"    ⚠️ übersprungen (Doppelter Eintrag): {e}")

    # Emoji-/Flaggen-"Codes" (z. B. Tor-Aktion zur EM) sind keine Codes
    flags = [r for r in con.execute(f"SELECT {COLS} FROM discount_codes").fetchall()
             if _REGIONAL.search(r["code"] or "")]
    print(f"🗑️ Emoji-/Flaggen-Codes: {len(flags)} Zeile(n)")
    for r in flags:
        print(show(r))
    if flags:
        con.execute(f"DELETE FROM discount_codes WHERE id IN ({','.join('?' * len(flags))})",
                    [r["id"] for r in flags])
    total += len(flags)

    # Nur melden: Einträge, die NACH den Korrekturen noch keinen Shop haben
    left = con.execute(
        f"SELECT {COLS} FROM discount_codes WHERE shop_url='' AND "
        "lower(trim(shop)) IN ('', '?', 'unbekannt', 'unknown', 'shopname')").fetchall()
    if left:
        print(f"\n❓ Noch ohne Shop (bitte selbst prüfen, z. B. mit /codes_edit): {len(left)}")
        for r in left:
            print(show(r))

    if apply:
        con.commit()
        print(f"\n✅ {total} Zeilen geändert/gelöscht.")
    else:
        con.rollback()
        print(f"\n{total} Zeilen betroffen. Probelauf – nichts geschrieben. Mit --apply übernehmen.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
