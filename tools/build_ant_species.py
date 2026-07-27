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
tools/build_ant_species.py – erzeugt die lokale Ameisen-Artenliste
(data/ant_species.json) aus dem GBIF-Backbone (Familie Formicidae).

EINMALIG bzw. gelegentlich auf dem Server ausführen (Netzzugang nötig):

    python3 tools/build_ant_species.py

Quelle: GBIF Backbone Taxonomy (datasetKey d7dddbf4-2cf0-4f39-9b2a-bb099caae36c),
Familie Formicidae (familyKey 4342). GBIF-Daten stehen unter CC-BY –
Attributionshinweis: „Taxonomie: GBIF Backbone Taxonomy (https://www.gbif.org)".

Erzeugte Struktur (alles kleingeschrieben als Schlüssel, Anzeigename im Wert):
    {
      "_meta":     {"source": "...", "generated": "ISO", "count_accepted": N, ...},
      "genera":    {"camponotus": "Camponotus", ...},
      "accepted":  {"camponotus nicobarensis": "Camponotus nicobarensis", ...},
      "epithets":  ["nicobarensis", "niger", ...],           # akzeptierte Epitheta
      "synonyms":  {"iridomyrmex humilis": "Linepithema humile", ...}  # syn -> akzeptiert
    }
"""
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

GBIF_SEARCH = "https://api.gbif.org/v1/species/search"
BACKBONE_DATASET = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
FORMICIDAE_KEY = 4342
PAGE = 1000
UA = "AntCheckBot-speciesbuilder/1.0 (+https://antcheck.info)"

OUT = Path(__file__).resolve().parent.parent / "data" / "ant_species.json"


def _fetch_page(offset: int) -> dict:
    params = {
        "datasetKey": BACKBONE_DATASET,
        "highertaxonKey": FORMICIDAE_KEY,
        "rank": "SPECIES",
        "limit": PAGE,
        "offset": offset,
    }
    for attempt in range(1, 5):
        try:
            r = requests.get(GBIF_SEARCH, params=params, timeout=60,
                             headers={"User-Agent": UA})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠️ Seite offset={offset} Versuch {attempt} fehlgeschlagen: {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"GBIF-Abruf endgültig fehlgeschlagen bei offset={offset}")


def main() -> int:
    accepted: dict[str, str] = {}    # "genus epithet" (lower) -> Display
    synonyms: dict[str, str] = {}    # syn binomial (lower)    -> akzeptiertes Display
    genera: dict[str, str] = {}      # genus (lower)           -> Display

    offset, total = 0, None
    print("🐜 Lade Formicidae aus dem GBIF-Backbone …")
    while True:
        data = _fetch_page(offset)
        if total is None:
            total = data.get("count")
            print(f"   Datensätze gesamt (rank=SPECIES): {total}")
        results = data.get("results", [])
        for rec in results:
            canonical = (rec.get("canonicalName") or "").strip()
            toks = canonical.split()
            if len(toks) != 2:            # nur echte Binomen (keine Trinomen/Fragmente)
                continue
            status  = (rec.get("taxonomicStatus") or "").upper()
            is_syn  = bool(rec.get("synonym"))
            acc_disp = (rec.get("species") or "").strip()   # akzeptierter Name
            low = canonical.lower()
            if is_syn or status in ("SYNONYM", "HETEROTYPIC_SYNONYM", "HOMOTYPIC_SYNONYM"):
                if acc_disp and len(acc_disp.split()) == 2 and low != acc_disp.lower():
                    synonyms[low] = acc_disp
            elif status == "ACCEPTED":
                accepted[low] = canonical
                genera[toks[0].lower()] = toks[0]

        if data.get("endOfRecords") or not results:
            break
        offset += PAGE
        if total and offset >= total:
            break
        time.sleep(0.2)   # höflich zur API

    # Synonyme, deren Ziel nicht als akzeptiert bekannt ist, entfernen (Konsistenz).
    acc_lower = set(accepted)
    synonyms = {k: v for k, v in synonyms.items() if v.lower() in acc_lower}
    epithets = sorted({k.split()[1] for k in accepted})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "source": "GBIF Backbone Taxonomy (CC-BY, https://www.gbif.org)",
            "family": "Formicidae", "familyKey": FORMICIDAE_KEY,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count_accepted": len(accepted),
            "count_synonyms": len(synonyms),
            "count_genera": len(genera),
        },
        "genera": dict(sorted(genera.items())),
        "accepted": dict(sorted(accepted.items())),
        "epithets": epithets,
        "synonyms": dict(sorted(synonyms.items())),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0)
    print(f"✅ Geschrieben: {OUT}")
    print(f"   akzeptiert={len(accepted)} · Synonyme={len(synonyms)} · Gattungen={len(genera)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
