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
tools/test_inat.py – prüft auf dem Server, ob die iNaturalist-API so antwortet,
wie die iNat-Statistik es erwartet (Profil-Bio für /inat_verknuepfen, Felder der
Beobachtungen). Schreibt nichts in die Datenbank.

Aufruf (im Bot-Verzeichnis, venv aktiv):
    python tools/test_inat.py <iNat-Benutzername> [Beobachtungs-ID]
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import inat as I  # noqa: E402


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    login = sys.argv[1]
    u = await I.find_user(login)
    if u is None:
        print("❌ API nicht erreichbar")
        return
    if not u:
        print(f"❌ Benutzer {login!r} nicht gefunden")
    else:
        bio = u.get("description") or ""
        print(f"✅ Benutzer: id={u['id']} login={u['login']}")
        print(f"   Bio ({len(bio)} Zeichen): {bio[:200]!r}")
        print("   → Bio wird geliefert" if bio else "   ⚠️ Bio leer (oder Feld fehlt) – Code-Prüfung würde scheitern")
    if len(sys.argv) > 2:
        oid = int(sys.argv[2])
        d = await I.fetch_observations([oid])
        if d is None:
            print("❌ Beobachtungen: API nicht erreichbar")
        elif oid not in d:
            print(f"❌ Beobachtung {oid} nicht gefunden")
        else:
            o = d[oid]
            print(f"✅ Beobachtung {oid}: user={o['inat_user_id']}/{o['inat_login']} datum={o['observed_on']} "
                  f"taxon={o['taxon_id']} {o['taxon_name']} ({o['taxon_rank']}) "
                  f"vorfahren={len(o['ancestors'])} ameise={o['is_ant']}")


asyncio.run(main())
