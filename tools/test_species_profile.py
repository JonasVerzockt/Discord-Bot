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
tools/test_species_profile.py – lädt den Kurzsteckbrief einer Art (Wikipedia + GBIF)
einmal neu und zeigt, was gefunden wurde. Schreibt dabei den Cache
data/species_profiles.json (wie die Board-Seite /art/<Name>).

    python3 tools/test_species_profile.py "Lasius niger"
"""
import sys
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import species_profile   # noqa: E402


def main() -> int:
    name = " ".join(sys.argv[1:]).strip() or "Lasius niger"
    entry = asyncio.run(species_profile._fetch(name))
    for lang, w in (entry.get("wiki") or {}).items():
        print(f"📖 Wikipedia {lang}: {w['title']}  ({'gekürzt' if w['cut'] else 'vollständig'})")
        print(f"   {w['text']}")
        print(f"   {w['url']}")
    if not entry.get("wiki"):
        print("📖 Wikipedia: nichts gefunden")
    g = entry.get("gbif")
    if g:
        print(f"🌍 GBIF {g['key']}: {g['count']} Nachweise, {len(g['countries'])} Länder, "
              f"Top: {', '.join(c for c, _ in g['countries'][:5])}  {g['url']}")
    else:
        print("🌍 GBIF: nichts gefunden")
    print("🔗", json.dumps(species_profile.links(name), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
