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
utils/swarm.py – Schwarmflug-Radar: Meldungen aus dem Schwarmflug-Kanal auswerten.

Die Community meldet Schwarmflüge seit 2019 nach einer festen Vorlage
(🐜 Art · 🕑 Datum u. Uhrzeit · 🌡️ Temperatur · 🌬️ Wind · 🗺️ Ort · 🌧️ Wetter). Diese
Meldungen werden mit festen Regeln gelesen (kostet nichts). Nur Nachrichten ohne
erkennbare Art gehen an die KI (wie bei Rabattcodes/Bewertungen).

Region: aus der erwähnten Regionsrolle (@Bayern …), sonst aus dem Ortstext, sonst aus
der Ortsauflösung (utils/geocode.py). Öffentlich wird nur die Region gezeigt; Ort und
Koordinaten sehen nur eingeloggte Mitglieder.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

from utils.db import execute_db
from utils.timez import BERLIN

logger = logging.getLogger(__name__)

# ── Gattungen ─────────────────────────────────────────────────────────────────
GENERA = {g.lower(): g for g in (
    "Lasius Formica Camponotus Tetramorium Myrmica Solenopsis Temnothorax Manica Colobopsis "
    "Myrmecina Messor Leptothorax Polyergus Formicoxenus Dolichoderus Tapinoma Ponera Hypoponera "
    "Stenamma Strongylognathus Harpagoxenus Anergates Plagiolepis Cardiocondyla Aphaenogaster "
    "Crematogaster Liometopum Prenolepis Linepithema Pheidole Proformica Cataglyphis Myrmoxenus "
    "Chalepoxenus Epimyrma Bothriomyrmex Monomorium Goniomma Oxyopomyrmex Rossomyrmex "
    "Myrmecocystus Polyrhachis Oecophylla Pogonomyrmex Atta Acromyrmex Strumigenys Myrmecia").split()}
# Untergattungen → Gattung (werden der Gattung zugeordnet)
SUBGENERA = {"chthonolasius": "Lasius", "chtonolasius": "Lasius", "dendrolasius": "Lasius", "austrolasius": "Lasius",
             "cautolasius": "Lasius", "acanthomyops": "Lasius", "serviformica": "Formica",
             "raptiformica": "Formica", "coptoformica": "Formica", "myrmentoma": "Camponotus",
             "tanaemyrmex": "Camponotus"}
# „Gattung (Untergattung)“ bzw. „Gattung Untergattung“ (nur bekannte Untergattungen)
_GEN_SUB = re.compile(r"\b([A-Za-z]+)\s*\(?\s*(" + "|".join(SUBGENERA) + r")\s*\)?(?![A-Za-z])", re.I)
_SP = {"sp", "sp.", "spec", "spec.", "spp", "spp.", "species"}
_CF = {"cf", "cf.", "aff", "aff."}
_GROUP = {"gruppe", "group", "komplex", "complex", "agg", "agg.", "artengruppe", "gr", "gr."}
_STOP = {"und", "and", "oder", "mit", "aus", "dem", "der", "die", "das", "von", "bei", "in", "im",
         "evtl", "evtl.", "vermutlich", "wahrscheinlich", "wohl", "eventuell", "auch", "sowie",
         "gyne", "gynen", "königin", "königinnen", "männchen", "drohnen", "arbeiterin", "arbeiterinnen",
         "schwarmflug", "flug", "massen", "viele", "einige", "ganz", "nicht", "sicher", "unsicher",
         "kahlrückige", "waldameise", "rote", "schwarze", "gelbe", "wegameise", "s", "str", "stricto",
         "sensu", "lato", "bestimmt", "mikroskop", "mit", "ca", "ca."}


def _genus_of(word: str) -> str | None:
    w = word.strip(".,;:!?()[]*\"'").lower()
    if w in GENERA:
        return GENERA[w]
    if w in SUBGENERA:
        return SUBGENERA[w]
    try:
        from utils import species_catalog
        g = species_catalog.resolve_genus(word) if word[:1].isupper() and len(word) > 3 else None
        return g
    except Exception:
        return None


def parse_taxa(text: str) -> list[dict]:
    """Taxa aus einem Text: [{genus, species|None, qualifier}] (qualifier: '', 'cf.', 'sp.',
    'Gruppe', 's. str.'). Untergattungen werden der Gattung zugeordnet."""
    t = re.sub(r"(?<=[A-Za-z])\.(?=[a-z])", " ", text or "")          # Formica.polyctena
    t = re.sub(r"[*_`]", " ", t)
    # Abgekürzte Gattung vor einem Artnamen: „L. fuliginosus“, „F. rufa“, „C. ligniperda“
    t = re.sub(r"\b([LFC])\.\s*(?=[a-z]{4,})", lambda m: {"L": "Lasius ", "F": "Formica ", "C": "Camponotus "}[m.group(1)], t)
    # „Lasius (Chthonolasius) …“ / „Lasius Chthonolasius …“ ist EIN Taxon: Gattung vor der eigenen
    # Untergattung weglassen (sonst entstünde zusätzlich „Lasius sp.“ und die Meldung zählte doppelt)
    t = _GEN_SUB.sub(lambda m: m.group(2) if SUBGENERA[m.group(2).lower()].lower() == m.group(1).lower()
                     else m.group(0), t)
    toks = re.findall(r"[A-Za-zÄÖÜäöüß]+\.?|\(|\)|,|/|\+|&", t)
    out, i = [], 0
    while i < len(toks):
        g = _genus_of(toks[i]) if toks[i][:1].isalpha() else None
        if not g:
            i += 1
            continue
        sub = toks[i].strip(".").lower() in SUBGENERA
        species, qual, j = None, [], i + 1
        while j < len(toks) and j <= i + 6:
            w = toks[j]
            wl = w.lower()
            if w in ("(", ")"):
                j += 1
                continue
            if wl in _CF:
                qual.append("cf."); j += 1; continue
            if wl in _SP:
                qual.append("sp."); j += 1; break
            if wl in _GROUP:
                qual.append("Gruppe"); j += 1; continue
            if wl in ("sensu", "senso", "s.") and j + 1 < len(toks) and toks[j + 1].lower().startswith("str"):
                qual.append("s. str."); j += 2; continue
            if _genus_of(w) or w in (",", "/", "+", "&"):
                break
            if wl in _STOP:
                if wl in ("aus", "dem", "der", "die", "das"):
                    j += 1
                    continue
                break
            if species is None and re.fullmatch(r"[a-zäöü]{3,}\.?", wl) and not w.isupper():
                from utils.swarm_resolve import epithet_plausible
                if not epithet_plausible(wl):
                    break                                  # Füllwort („Lasius ist …“): nur Gattung
                species = wl.strip(".")
                j += 1
                continue
            break
        if sub and not species:
            qual.append(f"({toks[i].strip('.').capitalize()})")
        q = " ".join(dict.fromkeys(qual))
        item = {"genus": g, "species": species, "qualifier": q}
        if item not in out:
            out.append(item)
        # „Camponotus ligniperda/herculeanus“: zweites Epitheton ohne Gattung
        while species and j + 1 < len(toks) and toks[j] == "/" and re.fullmatch(r"[a-zäöü]{3,}", toks[j + 1]) \
                and not _genus_of(toks[j + 1]) and toks[j + 1] not in _STOP \
                and __import__("utils.swarm_resolve", fromlist=["x"]).epithet_plausible(toks[j + 1]):
            alt = {"genus": g, "species": toks[j + 1], "qualifier": q}
            if alt not in out:
                out.append(alt)
            j += 2
        i = j
    return out


def taxon_label(t: dict) -> str:
    g, s, q = t.get("genus") or "", t.get("species"), t.get("qualifier") or ""
    if s:
        if "cf." in q:
            base = f"{g} cf. {s}"
        else:
            base = f"{g} {s}"
        return base + (" Gruppe" if "Gruppe" in q else "")
    if "s. str." in q:
        return f"{g} s. str."
    m = re.search(r"\((\w+)\)", q)
    return f"{g} ({m.group(1)})" if m else f"{g} sp."


# ── Regionen ──────────────────────────────────────────────────────────────────
def _region_names() -> dict:
    from utils import geo
    out = {}
    for cc, table in geo.all_regions().items():
        for code, name in table.items():
            out[name.lower()] = (cc, code)
    out.update({"nrw": ("de", "NW"), "bawü": ("de", "BW"), "bawue": ("de", "BW"),
                "baden württemberg": ("de", "BW"), "rlp": ("de", "RP"), "meck-pomm": ("de", "MV"),
                "mecklenburg vorpommern": ("de", "MV"), "schleswig holstein": ("de", "SH"),
                "sachsen anhalt": ("de", "ST"), "nordrhein westfalen": ("de", "NW"),
                "rheinland pfalz": ("de", "RP"), "oberoesterreich": ("at", "4"),
                "niederoesterreich": ("at", "3"), "schweiz": ("ch", ""), "österreich": ("at", ""), "deutschland": ("de", ""),
                "liechtenstein": ("li", "LI")})
    try:
        from utils.swarm_resolve import aliases
        for k, v in aliases().items():
            out.setdefault(k, v)
    except Exception:
        pass
    return out


_REGNAMES: dict | None = None


def region_from_name(name: str) -> tuple[str, str] | None:
    global _REGNAMES
    if _REGNAMES is None:
        _REGNAMES = _region_names()
    return _REGNAMES.get((name or "").strip().lower().replace("–", "-"))


def region_in_text(text: str) -> tuple[str, str] | None:
    """Sucht Bundesland/Kanton-Namen als ganze Wörter im Text (längste zuerst)."""
    global _REGNAMES
    if _REGNAMES is None:
        _REGNAMES = _region_names()
    low = " " + re.sub(r"[^\wäöüß\- ]", " ", (text or "").lower()) + " "
    for name in sorted(_REGNAMES, key=len, reverse=True):
        if len(name) < 3:
            continue
        if re.search(rf"(?<![\wäöü]){re.escape(name)}(?![\wäöü])", low):
            # „Bern“/„Zug“/„Wien“ nur, wenn eindeutig: kurze CH-Namen nicht im Fließtext raten
            if name in ("zug", "uri", "bern", "jura") and "kanton" not in low:
                continue
            return _REGNAMES[name]
    try:                                    # Kürzel (BW, NRW, OÖ …) und Tippfehler („Rehinland-Pfalz“)
        from utils.swarm_resolve import region_fuzzy
        return region_fuzzy(text)
    except Exception:
        return None


def region_name(country: str, code: str) -> str:
    from utils import geo
    if not country:
        return ""
    if not code:
        if country not in DACH:
            return country_label(country)
        return {"ch": "Schweiz", "at": "Österreich", "de": "Deutschland", "li": "Liechtenstein"}.get(country, "")
    return geo.all_regions().get(country, {}).get(code, code)


# ── Ausland / unklarer Ort ────────────────────────────────────────────────────
# Ausgewertet (Karte, Steckbriefe, Prognose …) werden nur Meldungen mit Land in DACH.
# Ausland (country = ISO-Code außerhalb DACH) erscheint nur in der Liste und im Kasten
# „Aus dem Urlaub“; unklare Orte (weder Land noch Region) sieht nur der Admin.
DACH = ("de", "at", "ch", "li")
DACH_SQL = "country IN ('de','at','ch','li')"
_FOREIGN: dict | None = None
_FOREIGN_RE = None
# Inseln/Regionen, die oft ohne Land genannt werden (Land → Liste)
_FOREIGN_EXTRA = {
    "es": ["mallorca", "menorca", "ibiza", "teneriffa", "tenerife", "gran canaria", "fuerteventura", "lanzarote",
           "kanaren", "kanarische inseln", "costa brava", "costa blanca", "andalusien", "katalonien", "valencia"],
    "it": ["gardasee", "sardinien", "sizilien", "toskana", "südtirol", "suedtirol", "dolomiten", "lago maggiore",
           "comer see", "elba", "venetien", "ligurien", "apulien", "kalabrien"],
    "fr": ["korsika", "provence", "elsass", "bretagne", "normandie", "côte d'azur", "cote d'azur"],
    "gr": ["kreta", "rhodos", "korfu", "kos", "zakynthos", "kykladen", "chalkidiki", "peloponnes", "santorini"],
    "hr": ["istrien", "dalmatien", "krk", "pag", "rab", "brač", "hvar", "split", "dubrovnik", "croatien", "croatia"],
    "pt": ["madeira", "azoren", "algarve"],
    "cz": ["tschechische republik", "tschechei"],
    "nl": ["holland"], "gb": ["england", "schottland", "wales", "großbritannien", "grossbritannien"],
    "us": ["usa", "vereinigte staaten", "amerika"], "th": ["phuket", "koh samui"], "tr": ["antalya"],
    "id": ["bali", "sumatra", "java", "borneo"],
}


def _foreign_table() -> dict:
    """Ländername/Alias (klein) → ISO-Code für alle Länder außer DACH (Babel/CLDR de+en+eo)."""
    global _FOREIGN, _FOREIGN_RE
    if _FOREIGN is not None:
        return _FOREIGN
    t: dict = {}
    try:
        from babel import Locale
        for lg in ("de", "en", "eo"):
            for code, name in Locale.parse(lg).territories.items():
                if len(code) == 2 and code.isalpha() and code.lower() not in DACH and len(name) >= 4:
                    t[name.lower()] = code.lower()
    except Exception as e:
        logger.info("🌍 Ländernamen nicht geladen: %s", e)
    for c, names in _FOREIGN_EXTRA.items():
        for n in names:
            t.setdefault(n, c)
    # Zu kurze bzw. mehrdeutige Treffer in deutschen Ortsnamen vermeiden
    for bad in ("jersey", "georgia", "georgien", "chad", "jordan", "niger", "guinea", "turkey", "monaco"):
        t.pop(bad, None)
    _FOREIGN = t
    names = sorted(t, key=len, reverse=True)
    _FOREIGN_RE = re.compile(r"(?<![\wäöüß])(?:süd|sued|nord|ost|west|mittel|zentral)?(" +
                             "|".join(re.escape(n) for n in names) + r")(?![\wäöüß])", re.I)
    return t


def foreign_country(text: str) -> str | None:
    """ISO-Code, wenn der Text ein Land (oder eine bekannte Urlaubsregion) außerhalb DACH nennt."""
    if not text:
        return None
    t = _foreign_table()
    if not t or _FOREIGN_RE is None:
        return None
    for m in _FOREIGN_RE.finditer(text):
        before = text[max(0, m.start() - 25):m.start()].lower()
        # „nähe Niederlande“, „Grenze zu Tschechien“, „Richtung Polen“: liegt noch in DACH
        if re.search(r"(nähe|naehe|nahe|grenze|richtung|gegenüber)\W+(\w+\W+){0,2}$", before):
            continue
        return t.get(m.group(1).lower())
    return None


def country_label(code: str, lang: str = "de") -> str:
    try:
        from utils.countries import country_name
        return country_name(code, lang)
    except Exception:
        return (code or "?").upper()


def scope_of(r: dict) -> str:
    """dach | abroad | unclear"""
    c = r.get("country") or ""
    if c in DACH:
        return "dach"
    return "abroad" if c else "unclear"


async def reclassify_abroad(bot) -> int:
    """Bestehende Meldungen nachträglich prüfen: Ortstext nennt ein Land außerhalb DACH →
    als Ausland markieren (Koordinate/Region verwerfen, falls die Ortssuche danebenlag)."""
    rows = await execute_db(bot, "SELECT DISTINCT place FROM swarm_reports WHERE place<>'' "
                                 "AND COALESCE(geo_src,'')<>'abroad'", fetch=True) or []
    n = 0
    for r in rows:
        c = foreign_country(r["place"])
        if not c:
            continue
        n += max(0, await execute_db(bot,
            "UPDATE swarm_reports SET country=?, region='', lat=NULL, lon=NULL, geo_src='abroad', w_state=NULL, "
            "w_src=NULL WHERE place=? AND COALESCE(geo_src,'')<>'abroad'", (c, r["place"]), commit=True) or 0)
    if n:
        logger.info("🌍 %d Schwarmflug-Meldungen als Ausland erkannt", n)
    return n


# ── Felder der Vorlage ────────────────────────────────────────────────────────
_FIELDS = [
    ("taxa", r"(?:unter)?(?:art(?:en)?|gattung)(?:\s*[,/]\s*gattung)?"),
    ("protected", r"(?:besonders\s*/\s*)?(?:streng\s+)?gesch(?:ü|ue)tzt[^:\n]*"),
    ("when", r"(?:datum|uhrzeit|zeit)[^:\n]*"),
    ("temp", r"temp(?:eratur)?[^:\n]*"),
    ("wind", r"win[d]?[^:\n]*"),
    ("place", r"ort[^:\n]*"),
    ("weather", r"wetter[^:\n]*"),
    ("extra", r"zusatz[^:\n]*|anmerkung[^:\n]*|bemerkung[^:\n]*"),
]
_LINE = re.compile(r"^\W*(?:[a-z_]+\d?\s+)?(" + "|".join(f"(?P<{k}>{p})" for k, p in _FIELDS) + r")\s*[:=]?\s*\**\s*(?P<val>.*)$", re.I)
_MONTHS = {"jan": 1, "feb": 2, "mär": 3, "maer": 3, "mar": 3, "apr": 4, "mai": 5, "may": 5,
           "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12}
_INDOOR = re.compile(r"\b(zimmer|box|formicarium|formikarium|becken|arena|terrarium|wohnung|"
                     r"in meiner haltung|aus meiner kolonie|bei mir drin|tropenbecken)\b", re.I)


def _clean(text: str, roles: dict) -> str:
    """Rollen-Erwähnungen durch Namen ersetzen, Custom-Emojis und Markdown entfernen."""
    t = text or ""
    t = re.sub(r"<@&(\d+)>", lambda m: "@" + roles.get(m.group(1), "Rolle"), t)
    t = re.sub(r"<a?:\w+:\d+>", " ", t)
    t = re.sub(r"(?<![\w:]):[a-z][a-z0-9_]*:(?!\d)", " ", t)          # nicht umgewandelte :shortcodes:
    t = re.sub(r"<@!?\d+>", " ", t)
    t = t.replace("**", "").replace("__", "")
    return t


def _fields(text: str) -> dict:
    out: dict = {}
    for line in re.split(r"[\n\r]+|\s{3,}(?=\W*[A-ZÄÖÜ][a-zäöü]+\s*:)", text):
        m = _LINE.match(line.strip())
        if not m:
            continue
        for k, _ in _FIELDS:
            if m.group(k):
                if k not in out:
                    out[k] = m.group("val").strip()
                elif k == "when":
                    out[k] += " " + m.group("val").strip()      # „Datum: 30.03.“ und „Zeit: 12:00“ getrennt
                break
    return out


def _date(raw: str, ref: date) -> date | None:
    s = (raw or "").lower()
    m = re.search(r"(?<!\d)(\d{1,2})\s*\.\s*(\d{1,2})\s*\.(?:\s*(\d{4})|(\d{2})(?!\d|\s*:))?", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = m.group(3) or m.group(4)
        y = ref.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s*\.?\s*(jan|feb|mär|maer|mar|apr|mai|may|jun|jul|aug|sep|okt|oct|nov|dez|dec)", s)
    if m:
        try:
            return date(ref.year, _MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    if "gestern" in s:
        return ref - timedelta(days=1)
    return None


def _time(raw: str) -> str | None:
    # Datum entfernen (Jahr nur, wenn es keine Uhrzeit ist: „30.03. 12:00“)
    s = re.sub(r"\d{1,2}\s*\.\s*\d{1,2}\s*\.(?:\s*\d{4}|\d{2}(?!\d|\s*:))?", " ", raw or "")
    m = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*:\s*([0-5]\d)(?!\d)", s)
    if not m:
        m = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*[.,]\s*([0-5]\d)\s*(?:uhr|h\b)", s, re.I)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*(?:uhr|h\b)", s, re.I)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def _temp(raw: str) -> float | None:
    m = re.search(r"(-?\d{1,2}(?:[.,]\d)?)", raw or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if -5 <= v <= 45 else None


def _place(raw: str) -> str:
    p = re.sub(r"@[\wÄÖÜäöüß\-]+(?:[ -][A-ZÄÖÜ][\wäöüß]+)?", " ", raw or "")
    p = re.sub(r"\s+", " ", p).strip(" ,;:-/")
    if p.count("(") > p.count(")"):
        p += ")"
    p = p.lstrip(")").rstrip("(").strip()
    if _PLACE_NOISE.fullmatch(p.strip(" .`\"'")):
        return ""
    return "" if region_from_name(p) else p[:160]


# Platzhalter und Vorlagenreste im Ortsfeld (kein Ort)
_PLACE_NOISE = re.compile(r"(\?+|-+|\.+|x+|/|keine?\s*angabe|k\.?\s*a\.?|unbekannt|geheim|"
                          r"\(?bundesländerrollen taggen\)?|bundesland.*taggen.*|)", re.I)


# ── Auswertung einer Nachricht ────────────────────────────────────────────────
def parse_message(content: str, roles: dict, posted_utc: datetime) -> dict:
    """Regelbasierte Auswertung. roles: {rollen_id: name} der erwähnten Rollen.
    Rückgabe: {taxa:[…], date, time, temp, wind, weather, place, country, region, indoor,
    protected, templated}. taxa leer = keine Art erkannt (→ ggf. KI)."""
    text = _clean(content, roles)
    ref = posted_utc.astimezone(BERLIN).date()
    f = _fields(text)
    taxa = parse_taxa(f["taxa"]) if f.get("taxa") else []
    if not taxa and f and not f.get("taxa"):
        # Vorlage ohne „Art:“-Zeile (z. B. „🐜: Lasius niger“): erste Zeile(n) ohne Feld
        head = "\n".join(l for l in text.splitlines() if not _LINE.match(l.strip()))
        taxa = parse_taxa(head)
    free_ok = True
    if not f:
        taxa = parse_taxa(text)
        # Längere Gespräche nicht per Regel als Meldung werten (→ KI entscheidet)
        free_ok = len(re.findall(r"\w+", text)) <= 20
    d = _date(f.get("when", ""), ref)
    if d is None or not (ref - timedelta(days=10) <= d <= ref + timedelta(days=1)):
        d = ref
    reg = None
    for rid, name in roles.items():
        reg = region_from_name(name)
        if reg:
            break
    place_raw = f.get("place", "")
    if not reg:
        reg = region_in_text(place_raw) or region_in_text(text)
    out = {
        "taxa": taxa, "date": d.isoformat(), "time": _time(f.get("when", "")) if f.get("when") else _time(text if not f else ""),
        "temp": _temp(f.get("temp", "")), "wind": (f.get("wind") or "")[:60] or None,
        "weather": (f.get("weather") or "")[:80] or None,
        "place": _place(place_raw) if place_raw else "",
        "country": reg[0] if reg else "", "region": reg[1] if reg else "",
        "indoor": bool(_INDOOR.search(text)),
        "protected": (f.get("protected") or "")[:20] or None,
        "templated": bool(f.get("taxa")),
        "free": not f,
        "rule_ok": bool(taxa) and free_ok,
    }
    if not out["place"] and not f:
        # Freitext: „… in Düsseldorf“ / „In 47167 Duisburg …“
        m = re.search(r"\b(?:in|bei|nähe|naehe)\s+((?:\d{4,5}\s+)?[A-ZÄÖÜ][\wäöüß\-]+(?:\s+[A-ZÄÖÜ][\wäöüß\-]+)?)", text)
        if m and not _genus_of(m.group(1).split()[-1]):
            out["place"] = m.group(1)
    # Ausland: Ortsfeld (bzw. bei Freitext der ganze Text) nennt ein Land außerhalb DACH
    out["abroad"] = foreign_country(place_raw or out["place"] or ("" if f else text))
    return out


# ── KI-Rückfallebene für Freitext ─────────────────────────────────────────────
_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "is_report": {"type": "boolean"},
        "taxa": {"type": "array", "items": {"type": "object", "properties": {
            "genus": {"type": "string"}, "species": {"type": ["string", "null"]},
            "qualifier": {"type": "string"}}, "required": ["genus", "species", "qualifier"],
            "additionalProperties": False}},
        "place": {"type": ["string", "null"]},
        "region": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
        "time": {"type": ["string", "null"]},
        "temp": {"type": ["number", "null"]},
        "indoor": {"type": "boolean"},
    },
    "required": ["is_report", "taxa", "place", "region", "date", "time", "temp", "indoor"],
    "additionalProperties": False,
}
_AI_PROMPT = """Du wertest eine Nachricht aus dem Discord-Kanal „schwarmflug-meldungen“ einer
deutschsprachigen Ameisen-Community aus. Nachrichtendatum: {date}.
Ist es eine Meldung eines beobachteten Schwarmflugs (Hochzeitsflug, geflügelte Tiere, frisch
begattete Königinnen)? Gespräche, Fragen, Links ohne Meldung → is_report=false.
Extrahiere nur, was wirklich dasteht, nichts erfinden:
- taxa: Gattung (wissenschaftlich, z. B. Lasius), species = Art-Epitheton klein (z. B. niger) oder null,
  qualifier = "cf." / "sp." / "Gruppe" / "" (Untergattungen wie Chthonolasius der Gattung zuordnen)
- place: Ort/Stadt/PLZ wie genannt oder null; region: Bundesland/Kanton/Land falls genannt, sonst null
- date: YYYY-MM-DD falls genannt, sonst null; time: HH:MM falls genannt, sonst null; temp in °C oder null
- indoor: true, wenn der Flug in der eigenen Haltung/Zimmer/Becken war
Nachricht:
{text}"""


def ai_parse(content: str, posted_utc: datetime) -> dict | None:
    """KI-Auswertung (nur für Nachrichten ohne erkannte Art). None bei Fehler/kein Key."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        from config import SWARM_PARSER_MODEL
        from utils.ai_compat import thinking_off, max_tokens_for, text_of
        ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = ai.messages.create(
            model=SWARM_PARSER_MODEL, max_tokens=max_tokens_for(SWARM_PARSER_MODEL, 400),
            **thinking_off(SWARM_PARSER_MODEL),
            messages=[{"role": "user", "content": _AI_PROMPT.format(
                date=posted_utc.astimezone(BERLIN).date().isoformat(), text=content[:1500])}],
            output_config={"format": {"type": "json_schema", "schema": _AI_SCHEMA}},
        )
        d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text_of(resp).strip()))
    except Exception as e:
        logger.warning("🪽 Schwarmflug-KI fehlgeschlagen: %s", e)
        return None
    if not d.get("is_report"):
        return {"taxa": []}
    taxa = []
    for t in d.get("taxa") or []:
        g = _genus_of(str(t.get("genus") or ""))
        if not g:
            continue
        sp = (t.get("species") or "") or None
        sp = sp.lower() if sp and re.fullmatch(r"[A-Za-z]{3,}", sp) else None
        taxa.append({"genus": g, "species": sp, "qualifier": (t.get("qualifier") or "")[:20]})
    ref = posted_utc.astimezone(BERLIN).date()
    try:
        dd = date.fromisoformat(d["date"]) if d.get("date") else ref
    except ValueError:
        dd = ref
    if not (ref - timedelta(days=10) <= dd <= ref + timedelta(days=1)):
        dd = ref
    reg = region_from_name(d.get("region") or "") or region_in_text(d.get("region") or "") \
        or region_in_text(d.get("place") or "")
    tm = d.get("time") if re.fullmatch(r"[0-2]\d:[0-5]\d", str(d.get("time") or "")) else None
    temp = d.get("temp") if isinstance(d.get("temp"), (int, float)) and -5 <= d["temp"] <= 45 else None
    return {"taxa": taxa, "date": dd.isoformat(), "time": tm, "temp": temp,
            "place": (d.get("place") or "")[:160], "country": reg[0] if reg else "",
            "region": reg[1] if reg else "", "indoor": bool(d.get("indoor"))}


# ── Speichern ─────────────────────────────────────────────────────────────────
async def store(bot, message_id, author_id, posted_utc: datetime, p: dict, source: str,
                lat=None, lon=None, geo_src=None) -> int:
    """Ersetzt die Einträge einer Nachricht. Gibt die Zahl der Taxa zurück."""
    mid = str(message_id)
    await execute_db(bot, "DELETE FROM swarm_reports WHERE message_id=?", (mid,), commit=True)
    n = 0
    for t in p.get("taxa") or []:
        await execute_db(bot,
            "INSERT OR IGNORE INTO swarm_reports (message_id, author_id, posted_at, flight_date, flight_time, "
            "genus, species, qualifier, label, country, region, place, lat, lon, geo_src, temp_c, wind, weather, "
            "indoor, protected, source, raw_label, taxon_check) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, str(author_id) if author_id else None,
             posted_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             p.get("date"), p.get("time"), t["genus"], t.get("species"), t.get("qualifier") or "",
             taxon_label(t), p.get("country") or "", p.get("region") or "", p.get("place") or "",
             lat, lon, geo_src, p.get("temp"), p.get("wind"), p.get("weather"),
             1 if p.get("indoor") else 0, p.get("protected"), source,
             t.get("raw") if t.get("raw") and t.get("raw") != taxon_label(t) else None, t.get("check")), commit=True)
        n += 1
    await execute_db(bot, "INSERT INTO swarm_scanned (message_id, status) VALUES (?,?) "
                          "ON CONFLICT(message_id) DO UPDATE SET status=excluded.status, scanned_at=datetime('now')",
                     (mid, "ok" if n else "none"), commit=True)
    return n


_COUNTRY_WORDS = {"deutschland", "germany", "de", "brd", "österreich", "oesterreich", "austria", "at",
                  "schweiz", "switzerland", "suisse", "svizzera", "ch", "liechtenstein", "li", "dach"}


def is_comment(p: dict, prev: dict | None, free: bool) -> bool:
    """Kurzer Freitext ohne Vorlagenfelder kurz nach einer eigenen Meldung derselben Gattung und ohne
    eigenen genaueren Ort: Kommentar zur Meldung davor, keine neue Meldung."""
    if not free or not prev or p.get("time") or p.get("temp") is not None:
        return False                                  # eigene Uhrzeit oder Temperatur: eigene Meldung
    if prev["genus"] not in {t["genus"] for t in p.get("taxa") or []}:
        return False
    place = (p.get("place") or "").strip().lower().strip(".!,")
    if not place and not p.get("region"):
        return True
    if place in _COUNTRY_WORDS and not p.get("region"):
        return True                                   # nur „Deutschland“ o. Ä.
    if p.get("region") and p.get("region") == prev.get("region") and \
            (not place or place == (prev.get("place") or "").strip().lower()):
        return True
    return False


async def previous_report(bot, author_id, before_utc: datetime, exclude_mid, hours: int = 3) -> list[dict]:
    """Meldungen derselben Person in den Stunden davor (neueste zuerst, ohne die Nachricht selbst)."""
    if not author_id:
        return []
    since = (before_utc - timedelta(hours=hours)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    until = before_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [dict(r) for r in await execute_db(bot,
        "SELECT * FROM swarm_reports WHERE author_id=? AND posted_at>=? AND posted_at<? AND message_id<>? "
        "ORDER BY posted_at DESC", (str(author_id), since, until, str(exclude_mid)), fetch=True) or []]


def comment_of(p: dict, prevs: list[dict], free: bool) -> dict | None:
    """Passende frühere Meldung, wenn die Nachricht nur ein Kommentar dazu ist."""
    for prev in prevs:
        if is_comment(p, prev, free):
            return prev
    return None


async def remove(bot, message_id) -> None:
    for sql in ("DELETE FROM swarm_reports WHERE message_id=?", "DELETE FROM swarm_scanned WHERE message_id=?"):
        await execute_db(bot, sql, (str(message_id),), commit=True)


async def previous_of(bot, author_id, before_utc: datetime, hours: int = 3) -> dict | None:
    """Letzte Meldung derselben Person kurz davor (für Ergänzungen ohne eigenen Ort)."""
    if not author_id:
        return None
    since = (before_utc - timedelta(hours=hours)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    until = before_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    r = await execute_db(bot,
        "SELECT * FROM swarm_reports WHERE author_id=? AND posted_at>=? AND posted_at<? "
        "AND (region<>'' OR place<>'') ORDER BY posted_at DESC LIMIT 1", (str(author_id), since, until), fetch=True)
    return dict(r[0]) if r else None


# ── Auswertungen fürs Board ───────────────────────────────────────────────────
async def rows(bot, since: str | None = None, include_indoor: bool = False, scope: str = "dach") -> list[dict]:
    """scope: dach (Auswertung) | abroad | unclear | all"""
    sql = "SELECT * FROM swarm_reports"
    cond, params = [], []
    if scope == "dach":
        cond.append(DACH_SQL)
    elif scope == "abroad":
        cond.append("country<>'' AND NOT " + DACH_SQL)
    elif scope == "unclear":
        cond.append("country=''")
    if since:
        cond.append("flight_date >= ?"); params.append(since)
    if not include_indoor:
        cond.append("indoor = 0")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    return [dict(r) for r in (await execute_db(bot, sql + " ORDER BY flight_date DESC, flight_time DESC",
                                              tuple(params), fetch=True) or [])]


def temp_of(r: dict):
    """Temperatur aus der Meldung, sonst (geschätzt) der nachgetragene Wetterwert."""
    return r["temp_c"] if r.get("temp_c") is not None else r.get("w_temp")


def _week(d: str) -> int:
    try:
        return date.fromisoformat(d).isocalendar()[1]
    except ValueError:
        return 0


def summarize(rs: list[dict]) -> dict:
    """Kennzahlen, Wochenverteilung je Gattung und Steckbrief je Taxon."""
    from statistics import median
    years = sorted({r["flight_date"][:4] for r in rs})
    by_genus: dict = {}
    by_taxon: dict = {}
    for r in rs:
        by_genus.setdefault(r["genus"], []).append(r)
        key = r["genus"] + (" " + r["species"] if r["species"] else "")
        by_taxon.setdefault(key, []).append(r)
    weeks = list(range(14, 45))                   # Anfang April bis Ende Oktober
    series = []
    for g, lst in sorted(by_genus.items(), key=lambda x: -len(x[1]))[:6]:
        cnt = [0] * len(weeks)
        for r in lst:
            w = _week(r["flight_date"])
            if w in weeks:
                cnt[weeks.index(w)] += 1
        series.append({"genus": g, "n": len(lst), "weeks": cnt})
    taxa = []
    for k, lst in by_taxon.items():
        if len(lst) < 3:
            continue
        months = [0] * 12
        for r in lst:
            months[int(r["flight_date"][5:7]) - 1] += 1
        hrs = [int(r["flight_time"][:2]) for r in lst if r["flight_time"]]
        tmp = [x for x in (temp_of(r) for r in lst) if x is not None]
        regs: dict = {}
        for r in lst:
            if r["region"] or r["country"]:
                nm = region_name(r["country"], r["region"])
                regs[nm] = regs.get(nm, 0) + 1
        ws = sorted(_week(r["flight_date"]) for r in lst)
        taxa.append({"name": k, "genus": lst[0]["genus"], "n": len(lst),
                     "years": len({r["flight_date"][:4] for r in lst}),
                     "w_from": ws[len(ws) // 10] if ws else None, "w_to": ws[(len(ws) * 9) // 10] if ws else None,
                     "w_peak": max(set(ws), key=ws.count) if ws else None,
                     "months": months,
                     "hour": int(median(hrs)) if len(hrs) >= 3 else None,
                     "h_from": sorted(hrs)[len(hrs) // 4] if len(hrs) >= 4 else None,
                     "h_to": sorted(hrs)[(len(hrs) * 3) // 4] if len(hrs) >= 4 else None,
                     "temp": round(median(tmp)) if len(tmp) >= 3 else None,
                     "t_from": round(sorted(tmp)[len(tmp) // 4]) if len(tmp) >= 4 else None,
                     "t_to": round(sorted(tmp)[(len(tmp) * 3) // 4]) if len(tmp) >= 4 else None,
                     "regions": sorted(regs.items(), key=lambda x: -x[1])[:3],
                     "last": max(r["flight_date"] for r in lst)})
    taxa.sort(key=lambda x: -x["n"])
    region_counts: dict = {}
    for r in rs:
        if r["country"] and r["region"]:
            k = f"{r['country']}-{r['region']}"
            region_counts[k] = region_counts.get(k, 0) + 1
    return {"total": len(rs), "messages": len({r["message_id"] for r in rs}),
            "reporters": len({r["author_id"] for r in rs if r["author_id"]}),
            "years": years, "genera": len(by_genus), "weeks": weeks, "series": series,
            "taxa": taxa, "regions": region_counts}


def week_label(w: int | None, lang: str = "de") -> str:
    """KW → ungefähre Monatsangabe, z. B. 27 → 'Anfang Juli' / 'early July'."""
    if not w:
        return "?"
    d = date.fromisocalendar(2025, max(1, min(52, w)), 3)
    i = 0 if d.day <= 10 else (1 if d.day <= 20 else 2)
    parts = {"de": ("Anfang", "Mitte", "Ende"), "en": ("early", "mid", "late"),
             "eo": ("komence de", "meze de", "fine de")}.get(lang, ("Anfang", "Mitte", "Ende"))
    mons = {"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                   "Oktober", "November", "Dezember"],
            "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                   "October", "November", "December"],
            "eo": ["januaro", "februaro", "marto", "aprilo", "majo", "junio", "julio", "aŭgusto", "septembro",
                   "oktobro", "novembro", "decembro"]}.get(lang)
    return f"{parts[i]} {(mons or [])[d.month - 1] if mons else ''}".strip()


# ── Saisonstart ───────────────────────────────────────────────────────────────
# Cache {jahr: zeile} für Board (Banner/Intro) und Cog; wird beim Start und bei jedem
# neuen Saisonstart aktualisiert.
SEASON: dict[int, dict] = {}
SEASON_DAYS = 14            # so lange gelten Banner und Intro nach dem Saisonstart


async def load_season(bot) -> None:
    rows = await execute_db(bot, "SELECT * FROM swarm_season", fetch=True) or []
    SEASON.clear()
    SEASON.update({int(r["year"]): dict(r) for r in rows})


SEASON_FROM = (3, 1)        # frühester Saisonstart: 1. März
SEASON_CONFIRM_DAYS = 7     # Bestätigung durch eine zweite Person (gleiche Gattung) innerhalb von 7 Tagen
_Q = ("indoor=0 AND source='rule' AND country IN ('de','at','ch','li') AND region<>'' "
      "AND substr(flight_date,1,4)=? AND flight_date>=? "
      "AND posted_at >= (SELECT value FROM swarm_meta WHERE key='ach_since')")


async def _season_skip(bot, year: int) -> set[str]:
    r = await execute_db(bot, "SELECT value FROM swarm_meta WHERE key=?", (f"season_skip_{year}",), fetch=True)
    return {x for x in (r[0]["value"] if r else "").split(",") if x}


async def season_check(bot, message_id=None) -> dict | None:
    """Saisonstart des laufenden Jahres ermitteln. Regeln:
      • nur neue Nachrichten (ab dem Einschalten, swarm_meta.ach_since), keine eingelesenen alten Posts
      • im Freien, mit Region in DE/AT/CH/LI, ab dem 1. März, nur nach Regeln erkannt (nicht KI)
      • bestätigt: eine zweite Person meldet dieselbe Gattung innerhalb von 7 Tagen
    Saisonstart ist dann die frühere der beiden Meldungen. Von Admins verworfene Meldungen zählen nicht."""
    now = datetime.now(BERLIN).date()
    y = now.year
    if SEASON.get(y) or (now.month, now.day) < SEASON_FROM:
        return None
    if await execute_db(bot, "SELECT 1 FROM swarm_season WHERE year=?", (y,), fetch=True):
        return None
    skip = await _season_skip(bot, y)
    rows = [dict(r) for r in await execute_db(bot,
        f"SELECT message_id, author_id, genus, label, country, region, flight_date FROM swarm_reports WHERE {_Q} "
        "ORDER BY flight_date, posted_at", (str(y), f"{y}-{SEASON_FROM[0]:02d}-{SEASON_FROM[1]:02d}"),
        fetch=True) or []]
    rows = [r for r in rows if r["message_id"] not in skip and r["author_id"]]
    for i, r in enumerate(rows):
        d0 = date.fromisoformat(r["flight_date"])
        ok = any(o["genus"] == r["genus"] and o["author_id"] != r["author_id"]
                 and 0 <= (date.fromisoformat(o["flight_date"]) - d0).days <= SEASON_CONFIRM_DAYS
                 for o in rows[i + 1:])
        if not ok:
            continue
        rc = await execute_db(bot, "INSERT OR IGNORE INTO swarm_season (year, message_id, author_id, label, country, "
                                   "region, flight_date) VALUES (?,?,?,?,?,?,?)",
                              (y, r["message_id"], r["author_id"], r["label"], r["country"], r["region"],
                               r["flight_date"]), commit=True)
        if rc:
            await load_season(bot)
            return SEASON.get(y)
        return None
    return None


async def season_reset(bot, year: int | None = None) -> dict | None:
    """Admin: falschen Saisonstart verwerfen. Die Meldung zählt danach nicht mehr; der nächste
    bestätigte Kandidat wird sofort gesucht. Gibt den verworfenen Eintrag zurück."""
    y = year or datetime.now(BERLIN).year
    r = await execute_db(bot, "SELECT * FROM swarm_season WHERE year=?", (y,), fetch=True)
    if not r:
        return None
    old = dict(r[0])
    skip = await _season_skip(bot, y) | {old["message_id"]}
    await execute_db(bot, "INSERT INTO swarm_meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                          "value=excluded.value", (f"season_skip_{y}", ",".join(sorted(skip))), commit=True)
    await execute_db(bot, "DELETE FROM swarm_season WHERE year=?", (y,), commit=True)
    await execute_db(bot, "DELETE FROM swarm_alert_sent WHERE message_id=?", (f"start-{y}",), commit=True)
    await load_season(bot)
    return old


def season_now(today: date | None = None) -> dict | None:
    """Saisonstart dieses Jahres, wenn er höchstens SEASON_DAYS Tage her ist."""
    today = today or datetime.now(BERLIN).date()
    r = SEASON.get(today.year)
    if not r:
        return None
    try:
        d = date.fromisoformat(r["created_at"][:10])
    except (ValueError, TypeError):
        return None
    return r if 0 <= (today - d).days <= SEASON_DAYS else None


# ── Melde-Vorlage (wie die angepinnte Vorlage im Kanal) ───────────────────────
TEMPLATE = [("taxa", ":ant:", "🐜", "Art:"),
            ("protected", ":octagonal_sign:", "🛑", "Besonders/Streng geschützt [ja/nein]:"),
            ("when", ":clock2:", "🕑", "Datum u. Uhrzeit:"),
            ("temp", ":thermometer:", "🌡️", "Temperatur:"),
            ("wind", ":wind_blowing_face:", "🌬️", "Wind:"),
            ("place", ":map:", "🗺️", "Ort:"),
            ("weather", ":cloud_rain:", "🌧️", "Wetter:"),
            ("extra", ":grey_question:", "❔", "Zusatzinformationen:")]


def build_template(data: dict, shortcodes: bool = True) -> str:
    out = []
    for key, code, emo, label in TEMPLATE:
        val = (data.get(key) or "").strip()
        out.append(f"{code if shortcodes else emo} {label} {val}".rstrip())
    return "\n".join(out)
