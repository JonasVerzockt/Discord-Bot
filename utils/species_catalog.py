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
utils/species_catalog.py – Prüfung/Kanonisierung von Ameisen-Namen gegen eine
lokale AntCat-Artenliste (data/ant_species.json, erzeugt von
tools/build_ant_species.py).

Kernidee:
  • VALIDIEREN  – ist die Eingabe eine bekannte Gattung/Art (oder ein Epitheton)?
  • VORSCHLAGEN – bei Tippfehlern die korrekte Schreibweise nennen (nicht selbst
    ersetzen – der User entscheidet und tippt neu bzw. nutzt force).
  • KANONISIEREN– Synonyme auf den akzeptierten Namen abbilden; aus „Rausch"-
    Strings (Shop-Titel) das enthaltene bekannte Binomen ziehen (resolve_field).

Robust: fehlt die Datei, meldet available()=False und check() liefert
status="unavailable" → Aufrufer lassen die Eingabe dann unverändert durch
(kein Feature-Bruch, bevor die Liste erzeugt wurde).
"""
import os
import json
import logging
from pathlib import Path
from difflib import get_close_matches

from config import SPECIES_CATALOG_FILE
from utils.localization import l10n
from utils.availability import normalize_species_name

logger = logging.getLogger(__name__)

CATALOG_FILE = Path(SPECIES_CATALOG_FILE)

_FUZZY_CUTOFF = 0.82

# Geladener Zustand (mtime-gecacht).
_mtime: float | None = None
_genera: dict[str, str] = {}          # "camponotus" -> "Camponotus"
_accepted: dict[str, str] = {}        # "camponotus nicobarensis" -> Display
_synonyms: dict[str, str] = {}        # syn (lower) -> akzeptiertes Display
_epithets: set[str] = set()           # {"nicobarensis", ...}
_by_genus: dict[str, list[str]] = {}  # "camponotus" -> ["nicobarensis", ...]


def _load() -> None:
    """Lädt die Katalogdatei neu, wenn sie sich geändert hat (mtime-Cache)."""
    global _mtime, _genera, _accepted, _synonyms, _epithets, _by_genus
    try:
        m = CATALOG_FILE.stat().st_mtime
    except FileNotFoundError:
        if _mtime is not None:
            _mtime, _genera, _accepted, _synonyms, _epithets, _by_genus = None, {}, {}, {}, set(), {}
        return
    if m == _mtime:
        return
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("❌ Artenliste nicht lesbar (%s): %s", CATALOG_FILE, e)
        return
    _genera   = {k.lower(): v for k, v in data.get("genera", {}).items()}
    _accepted = {k.lower(): v for k, v in data.get("accepted", {}).items()}
    _synonyms = {k.lower(): v for k, v in data.get("synonyms", {}).items()}
    _epithets = set(data.get("epithets", []))
    by: dict[str, list[str]] = {}
    for key in _accepted:
        g, _, ep = key.partition(" ")
        if ep:
            by.setdefault(g, []).append(ep)
    _by_genus = by
    _mtime = m
    logger.info("🐜 Artenliste geladen: %d Arten, %d Synonyme, %d Gattungen",
                len(_accepted), len(_synonyms), len(_genera))


def available() -> bool:
    """True, wenn eine Artenliste geladen ist."""
    _load()
    return bool(_accepted)


def _alpha_tokens(raw: str) -> list[str]:
    """Normalisierte, rein alphabetische Tokens (verwirft Klammern/Zahlen/Autor)."""
    return [t for t in normalize_species_name(raw or "").split() if t.isalpha()]


def _suggest_binomial(genus: str, epithet: str) -> str | None:
    """Sucht den ähnlichsten bekannten akzeptierten Namen (Tippfehler-Vorschlag)."""
    # Gattung ggf. zuerst korrigieren.
    g = genus if genus in _by_genus else None
    if g is None:
        gg = get_close_matches(genus, list(_by_genus), n=1, cutoff=0.80)
        g = gg[0] if gg else None
    if g is not None:
        ep = get_close_matches(epithet, _by_genus.get(g, []), n=1, cutoff=_FUZZY_CUTOFF)
        if ep:
            return _accepted.get(f"{g} {ep[0]}")
    # Ganzes Binomen gegen alle akzeptierten Namen (bounded).
    m = get_close_matches(f"{genus} {epithet}", list(_accepted), n=1, cutoff=_FUZZY_CUTOFF)
    return _accepted.get(m[0]) if m else None


def check(raw: str) -> dict:
    """
    Prüft eine Nutzereingabe (Gattung, Art-Binomen oder einzelnes Epitheton).

    Rückgabe-dict: {status, kind, canonical, suggestion, input}
      status:  "accepted" | "synonym" | "suggest" | "unknown" | "unavailable" | "empty"
      kind:    "genus" | "species" | "epithet" | None
      canonical:  akzeptierter Anzeigename (bei accepted/synonym)
      suggestion: vorgeschlagener Anzeigename (bei suggest)
    """
    res = {"status": "unknown", "kind": None, "canonical": None,
           "suggestion": None, "input": (raw or "").strip()}
    _load()
    if not _accepted:
        res["status"] = "unavailable"
        return res
    toks = _alpha_tokens(raw)
    if not toks:
        res["status"] = "empty"
        return res

    if len(toks) >= 2:
        genus, epithet = toks[0], toks[1]
        key = f"{genus} {epithet}"
        res["kind"] = "species"
        if key in _accepted:
            res["status"], res["canonical"] = "accepted", _accepted[key]
        elif key in _synonyms:
            res["status"], res["canonical"] = "synonym", _synonyms[key]
        else:
            sug = _suggest_binomial(genus, epithet)
            if sug:
                res["status"], res["suggestion"] = "suggest", sug
            else:
                res["status"] = "unknown"
        return res

    # Ein Token: Gattung oder Epitheton
    t = toks[0]
    if t in _genera:
        res["status"], res["kind"], res["canonical"] = "accepted", "genus", _genera[t]
    elif t in _epithets:
        res["status"], res["kind"] = "accepted", "epithet"
    else:
        cand = get_close_matches(t, list(_genera) + sorted(_epithets), n=1, cutoff=_FUZZY_CUTOFF)
        if cand:
            hit = cand[0]
            res["status"], res["kind"] = "suggest", "genus" if hit in _genera else "epithet"
            res["suggestion"] = _genera.get(hit, hit)
        else:
            res["status"], res["kind"] = "unknown", "genus"
    return res


def resolve_field(species_str: str) -> str | None:
    """
    Zieht aus einem (evtl. verrauschten) Shop-„species"-String das enthaltene
    bekannte Binomen und gibt den AKZEPTIERTEN Anzeigenamen zurück (Synonyme
    werden aufgelöst). Deterministisch, ohne Fuzzy – für den Grabber gedacht.
    None, wenn nichts Bekanntes gefunden wird oder keine Liste vorhanden ist.
    """
    _load()
    if not _accepted:
        return None
    toks = _alpha_tokens(species_str)
    for i in range(len(toks) - 1):
        cand = f"{toks[i]} {toks[i + 1]}"
        if cand in _accepted:
            return _accepted[cand]
        if cand in _synonyms:   # auch Synonym-Gattungen (z.B. „Iridomyrmex humilis")
            return _synonyms[cand]
    return None


def canonical(name: str) -> str | None:
    """
    Akzeptierter Anzeigename für einen SAUBEREN Gattungs-/Artnamen (nicht verrauscht):
      • Art-Binomen: accepted → sich selbst; Synonym → aktueller Name; sonst None
      • Gattung (ein Token): akzeptierte Gattung → Anzeigename; sonst None
    None, wenn unbekannt oder keine Liste geladen. Für die Kanonisierung von
    Nutzereingaben und gespeicherten Beobachtungen (Synonym-Vereinheitlichung).
    """
    _load()
    if not _accepted and not _genera:
        return None
    toks = _alpha_tokens(name)
    if len(toks) >= 2:
        key = f"{toks[0]} {toks[1]}"
        if key in _accepted:
            return _accepted[key]
        if key in _synonyms:
            return _synonyms[key]
        return None
    if len(toks) == 1:
        return _genera.get(toks[0])
    return None


def validation_message(res: dict, lang: str) -> str | None:
    """
    Liefert eine lokalisierte Hinweis-Nachricht, wenn die Eingabe NICHT direkt
    akzeptiert ist (Synonym/Tippfehler/unbekannt). Bei accepted/unavailable/empty
    → None (Aufrufer verarbeitet normal weiter).
    Die Nachricht KORRIGIERT NICHT selbst, sondern nennt die richtige Schreibweise.
    """
    st = res.get("status")
    if st == "synonym":
        return l10n.get("species_synonym", lang,
                        input=res["input"], correct=res["canonical"])
    if st == "suggest":
        return l10n.get("species_suggest", lang,
                        input=res["input"], correct=res["suggestion"])
    if st == "unknown":
        return l10n.get("species_unknown", lang, input=res["input"])
    return None
