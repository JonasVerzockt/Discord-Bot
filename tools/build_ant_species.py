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
(data/ant_species.json) aus der AntCat REST-API (antcat.org/v1).

AntCat ist die maßgebliche taxonomische Autorität für Ameisen (Formicidae) – im
Gegensatz zum allgemeinen GBIF-Backbone sind hier Gültig/Synonym und die
akzeptierten Namen aktuell (z. B. Camponotus ligniperda, Neoponera apicalis).

EINMALIG bzw. gelegentlich (z. B. monatlich per Cron) auf dem Server ausführen –
Netzzugang nötig:

    python3 tools/build_ant_species.py

Wichtig: antcat.org steht hinter Cloudflare; der REST-Pfad /v1 ist frei, aber ein
Browser-User-Agent ist nötig (sonst 403). Wird hier gesetzt.

Erzeugte Struktur (Schlüssel klein, Anzeigename im Wert) – identisch zum bisherigen
Format, daher KEINE Änderung am Bot nötig:
    {
      "_meta":    {"source": "AntCat REST API (antcat.org/v1)", ...},
      "genera":   {"camponotus": "Camponotus", ...},
      "accepted": {"camponotus ligniperda": "Camponotus ligniperda", ...},
      "epithets": ["ligniperda", "niger", ...],
      "synonyms": {"camponotus ligniperdus": "Camponotus ligniperda",
                   "pachycondyla apicalis":  "Neoponera apicalis", ...}
    }
"""
import os
import sys
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://antcat.org/v1/taxa"
# Browser-User-Agent ist Pflicht (Cloudflare blockt sonst mit 403 auf /v1).
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
PAGE_SIZE = 100          # AntCat liefert 100 Taxa pro Seite
MAX_PAGES = 5000         # Sicherheitslimit

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.getenv("DATA_DIR", str(_ROOT / "data")))
OUT = Path(os.getenv("SPECIES_CATALOG_FILE", str(_DATA_DIR / "ant_species.json")))


def _two(name: str) -> bool:
    """True bei einem Binomen (genau zwei Tokens)."""
    return len((name or "").split()) == 2


def _fetch_all() -> list[dict]:
    """Zieht alle Taxa seitenweise aus der AntCat-API."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})
    records: list[dict] = []
    print("🐜 Lade AntCat /v1/taxa …")
    for page in range(1, MAX_PAGES + 1):
        data = None
        for attempt in range(1, 5):
            try:
                r = sess.get(API, params={"page": page}, timeout=60)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                print(f"  ⚠️ Seite {page} Versuch {attempt} fehlgeschlagen: {e}")
                time.sleep(3 * attempt)
        if data is None:
            raise RuntimeError(f"AntCat-Abruf endgültig fehlgeschlagen bei Seite {page}")
        if not data:
            break
        records.extend(data)
        if page % 50 == 0:
            print(f"   … {len(records)} Taxa (Seite {page})")
        if len(data) < PAGE_SIZE:
            break
        time.sleep(0.15)   # höflich zur API
    print(f"   {len(records)} Taxa geladen.")
    return records


def main() -> int:
    records = _fetch_all()

    # ── Index über alle Taxa (für die Synonym-Auflösung) ──────────────────────
    id_name: dict[int, str] = {}
    id_status: dict[int, str] = {}
    id_target: dict[int, int] = {}   # current_taxon_id bzw. homonym_replaced_by_id
    id_rank: dict[int, str] = {}
    for rec in records:
        if not rec:
            continue
        rank, obj = next(iter(rec.items()))
        tid = obj.get("id")
        if tid is None:
            continue
        id_name[tid] = (obj.get("name_cache") or "").strip()
        id_status[tid] = (obj.get("status") or "").lower()
        id_target[tid] = obj.get("current_taxon_id") or obj.get("homonym_replaced_by_id")
        id_rank[tid] = rank

    def resolve(target_id, hops: int = 8):
        """Folgt current_taxon_id/homonym-Kette bis zu einem gültigen Taxon."""
        seen = set()
        tid = target_id
        while tid is not None and tid not in seen and hops > 0:
            seen.add(tid)
            hops -= 1
            if id_status.get(tid) == "valid":
                return id_name.get(tid)
            tid = id_target.get(tid)
        return None

    accepted: dict[str, str] = {}
    synonyms: dict[str, str] = {}
    genera: dict[str, str] = {}

    for tid, rank in id_rank.items():
        name = id_name.get(tid, "")
        status = id_status.get(tid)
        if not name:
            continue
        if rank == "genus" and status == "valid":
            genera.setdefault(name.lower(), name)
        elif rank == "species" and _two(name):
            if status == "valid":
                accepted[name.lower()] = name
                genera.setdefault(name.split()[0].lower(), name.split()[0])
        # Synonyme/Homonyme/obsolete Kombinationen etc. → auf den aktuell gültigen
        # Namen abbilden (nur Art-Binomen, deren Ziel ebenfalls ein Binomen ist).
        if rank in ("species", "subspecies") and _two(name) and status != "valid":
            tgt = resolve(id_target.get(tid))
            if tgt and _two(tgt) and tgt.lower() != name.lower():
                synonyms[name.lower()] = tgt

    # Konsistenz: Synonyme nur behalten, wenn ihr Ziel als akzeptiert bekannt ist.
    synonyms = {k: v for k, v in synonyms.items() if v.lower() in accepted}
    epithets = sorted({k.split()[1] for k in accepted})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "source": "AntCat REST API (antcat.org/v1, CC-BY-SA)",
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count_accepted": len(accepted),
            "count_synonyms": len(synonyms),
            "count_genera": len(genera),
            "count_taxa_raw": len(records),
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
