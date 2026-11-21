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
utils/swarm_resolve.py – Unklares in Schwarmflug-Meldungen klären.

Kette (günstig → teuer, jede Stufe nur, wenn die vorige nichts ergibt):

  Art   1. Artenliste (AntCat, utils/species_catalog): akzeptiert / Synonym → aktueller Name /
           Tippfehler → Vorschlag (Ähnlichkeit ≥ 0,82). Originaltext bleibt in raw_label.
        2. Unbekannt → vorerst „Gattung sp.“ (taxon_check='unknown'), die KI darf nachbessern;
           ihr Vorschlag zählt nur, wenn ihn die Artenliste bestätigt.

  Ort   1. Regionsrolle  2. Region im Text (inkl. Landschaften, Kürzel, Tippfehler)
        3. Land außerhalb DACH (utils/swarm.foreign_country)  4. PLZ/Ortsliste, auch unscharf
        5. OpenStreetMap in DACH  6. OpenStreetMap weltweit (Treffer im Ausland → Auslandsmeldung)
        7. „s. o.“ / Anführungszeichen → Ort der vorherigen Meldung derselben Person
        8. KI mit den zwei Nachrichten davor als Zusammenhang
        9. Heimatregion: ≥ 3 Meldungen der Person aus derselben Region (≥ 60 %) → nur Region,
           als geschätzt markiert (geo_src='home'), nie als Punkt
       10. sonst unklar (nur im Admin-Bereich)

  Admin  Korrektur von Hand (swarm_override) gilt, bis die Nachricht in Discord bearbeitet wird.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from utils.db import execute_db

logger = logging.getLogger(__name__)

# ── Regionen: Landschaften, Regierungsbezirke, Kürzel ─────────────────────────
# Nur eindeutige Zuordnungen (z. B. nicht Harz, Bodensee, Allgäu, Eifel, Rhön, Lausitz).
REGION_ALIASES = {
    "de": {
        "BY": ["oberbayern", "niederbayern", "oberpfalz", "oberfranken", "mittelfranken", "unterfranken", "franken",
               "fichtelgebirge", "bayerischer wald", "chiemgau", "berchtesgadener land", "frankenwald"],
        "BW": ["schwarzwald", "hochschwarzwald", "südschwarzwald", "nordschwarzwald", "remstal", "schwäbische alb",
               "kraichgau", "hohenlohe", "breisgau", "ortenau", "badenwürttemberg", "baden-wuerttemberg"],
        "NI": ["emsland", "ostfriesland", "lüneburger heide", "wendland", "weserbergland", "südniedersachsen"],
        "SN": ["oberlausitz", "erzgebirge", "vogtland", "sächsische schweiz", "nordsachsen", "mittelsachsen"],
        "BB": ["niederlausitz", "spreewald", "uckermark", "prignitz", "havelland"],
        "SH": ["sylt", "föhr", "amrum", "nordfriesland", "dithmarschen"],
        "MV": ["usedom", "rügen", "darß", "mecklenburgische seenplatte"],
        "NW": ["sauerland", "siegerland", "münsterland", "ruhrgebiet", "ruhrpott", "niederrhein", "bergisches land",
               "ostwestfalen", "westfalen"],
        "RP": ["hunsrück", "westerwald", "pfalz", "pfälzerwald", "rheinlandpfalz", "rheinhessen"],
        "HE": ["taunus", "vogelsberg", "wetterau", "nordhessen", "südhessen"],
        "ST": ["altmark", "sachsenanhalt", "saalekreis"],
        "TH": ["thüringer wald", "eichsfeld"],
    },
    "at": {"2": ["südkärnten", "oberkärnten"], "3": ["wachau", "waldviertel", "weinviertel", "mostviertel"],
           "4": ["mühlviertel", "innviertel", "hausruckviertel", "traunviertel"], "6": ["murtal", "obersteiermark"],
           "7": ["zillertal", "ötztal", "osttirol"], "8": ["montafon", "bregenzerwald"]},
}
ABBR = {"BW": ("de", "BW"), "BY": ("de", "BY"), "NRW": ("de", "NW"), "RLP": ("de", "RP"), "Rlf": ("de", "RP"),
        "MV": ("de", "MV"), "SH": ("de", "SH"), "HH": ("de", "HH"), "HB": ("de", "HB"), "BB": ("de", "BB"),
        "SN": ("de", "SN"), "TH": ("de", "TH"), "OÖ": ("at", "4"), "NÖ": ("at", "3"), "Stmk": ("at", "6"),
        "Ktn": ("at", "2"), "Vbg": ("at", "8")}
_ABBR_RE = re.compile(r"(?<![\wÄÖÜäöü])(" + "|".join(re.escape(k) for k in ABBR) + r")(?![\wÄÖÜäöü])")
INHERIT_RE = re.compile(r"^\s*(s\.?\s*o\.?|siehe oben|wie oben|dito|ditto|\"+|“+|„+|''+|〃)\s*$", re.I)


def aliases() -> dict:
    out = {}
    for c, regs in REGION_ALIASES.items():
        for code, names in regs.items():
            for n in names:
                out[n] = (c, code)
    return out


def _norm(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z]", "", s)


_FUZZ: list | None = None


def region_fuzzy(text: str) -> tuple[str, str] | None:
    """Kürzel (BW, NRW, OÖ …) und Tippfehler in Bundesland-/Kantonsnamen („Rehinland-Pfalz“)."""
    global _FUZZ
    if not text:
        return None
    m = _ABBR_RE.search(text)
    if m:
        return ABBR[m.group(1)]
    try:
        from rapidfuzz import fuzz
    except Exception:
        return None
    if _FUZZ is None:
        from utils import geo
        _FUZZ = [(_norm(n), (c, code)) for c, t in geo.all_regions().items() for code, n in t.items() if len(n) >= 6]
    toks = [t for t in re.split(r"[^A-Za-zÄÖÜäöüß]+", text) if t]
    cands = toks + [a + b for a, b in zip(toks, toks[1:])] + [a + b + c for a, b, c in zip(toks, toks[1:], toks[2:])]
    best = (0, None)
    for c in cands:
        n = _norm(c)
        if len(n) < 6:
            continue
        for rn, reg in _FUZZ:
            sc = fuzz.ratio(n, rn)
            if sc > best[0]:
                best = (sc, reg)
    return best[1] if best[0] >= 90 else None


# ── Arten: aktueller Name aus der Artenliste ──────────────────────────────────
def canon_taxa(taxa: list[dict]) -> list[dict]:
    """Setzt den aktuellen Namen (Synonym/Tippfehler) und merkt den Originaltext (raw).
    check: ok | fixed | unknown | nolist. Unbekannte Epitheta werden vorerst „Gattung sp.“."""
    from utils import species_catalog as SC
    from utils.swarm import taxon_label
    out = []
    for t in taxa:
        t = dict(t)
        t["raw"] = taxon_label(t)
        if not t.get("species"):
            g = SC.canonical(t["genus"])
            if g and g != t["genus"]:
                t["genus"], t["check"] = g, "fixed"
            else:
                t["check"] = "ok" if g or not SC.available() else "nolist"
            out.append(t)
            continue
        res = SC.check(f"{t['genus']} {t['species']}")
        if res["status"] == "unavailable":
            t["check"] = "nolist"
        elif res["status"] == "accepted":
            g, s = res["canonical"].split()[:2]
            t["check"] = "ok" if (g, s.lower()) == (t["genus"], t["species"]) else "fixed"
            t["genus"], t["species"] = g, s.lower()
        elif res["status"] in ("synonym", "suggest"):
            name = res["canonical"] or res["suggestion"]
            g, s = name.split()[:2]
            t["genus"], t["species"], t["check"] = g, s.lower(), "fixed"
        else:
            t["species"], t["check"] = None, "unknown"
            if not t.get("qualifier"):
                t["qualifier"] = "sp."
        out.append(t)
    # doppelte nach der Korrektur entfernen
    seen, uniq = set(), []
    for t in out:
        k = (t["genus"], t.get("species"), t.get("qualifier") or "")
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


# ── Heimatregion ─────────────────────────────────────────────────────────────
async def home_region(bot, author_id) -> tuple[str, str] | None:
    if not author_id:
        return None
    rows = await execute_db(bot,
        "SELECT country, region, COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE author_id=? "
        "AND country IN ('de','at','ch','li') AND region<>'' AND COALESCE(geo_src,'') NOT IN ('home') "
        "GROUP BY country, region ORDER BY n DESC", (str(author_id),), fetch=True) or []
    if not rows:
        return None
    tot = sum(r["n"] for r in rows)
    top = rows[0]
    return (top["country"], top["region"]) if top["n"] >= 3 and top["n"] / tot >= 0.6 else None


async def previous_place(bot, author_id, before: str, days: int = 3) -> dict | None:
    """Letzte eigene Meldung mit Ort/Region vor einem Zeitpunkt („s. o.“)."""
    if not author_id:
        return None
    since = (datetime.strptime(before, "%Y-%m-%d %H:%M:%S") - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    r = await execute_db(bot,
        "SELECT place, country, region, lat, lon, geo_src FROM swarm_reports WHERE author_id=? AND posted_at<? "
        "AND posted_at>=? AND country<>'' ORDER BY posted_at DESC LIMIT 1", (str(author_id), before, since), fetch=True)
    return dict(r[0]) if r else None


# ── KI: Ort und unsichere Arten mit Zusammenhang ──────────────────────────────
_SCHEMA = {
    "type": "object",
    "properties": {
        "place": {"type": "string"},
        "country_iso": {"type": "string"},
        "region": {"type": "string"},
        "taxa": {"type": "array", "items": {"type": "object", "properties": {
            "reported": {"type": "string"}, "genus": {"type": "string"}, "species": {"type": "string"}},
            "required": ["reported", "genus", "species"], "additionalProperties": False}},
    },
    "required": ["place", "country_iso", "region", "taxa"], "additionalProperties": False,
}
_PROMPT = """Du hilfst, Schwarmflug-Meldungen von Ameisen aus einem Discord-Kanal (meist Deutschland,
Österreich, Schweiz) zu ergänzen. Antworte nur mit JSON nach Schema.

Aufgabe 1 (nur wenn ORT_GESUCHT = ja): Wo war der Schwarmflug? Nutze die Meldung und die
vorherigen Nachrichten derselben Person. place = Ort so genau wie genannt, country_iso = zweistelliger
ISO-Code des Landes, region = Bundesland bzw. Kanton (deutscher Name) oder leer. Wenn es nicht
erkennbar ist: alles leer lassen. Nichts raten.

Aufgabe 2: Für jede unsichere Art in UNSICHER: welche gültige Ameisenart ist vermutlich gemeint
(Tippfehler)? genus und species (Epitheton klein) oder species leer, wenn unklar.

ORT_GESUCHT: {need_place}
UNSICHER: {uncertain}

Vorherige Nachrichten derselben Person:
{context}

Meldung ({date}):
{text}"""


def ai_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def ai_resolve(text: str, context: list[str], date: str, need_place: bool, uncertain: list[str]) -> dict | None:
    if not ai_available() or (not need_place and not uncertain):
        return None
    try:
        import anthropic
        from config import SWARM_PARSER_MODEL
        from utils.ai_compat import thinking_off, max_tokens_for, text_of
        ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = ai.messages.create(
            model=SWARM_PARSER_MODEL, max_tokens=max_tokens_for(SWARM_PARSER_MODEL, 400),
            **thinking_off(SWARM_PARSER_MODEL),
            messages=[{"role": "user", "content": _PROMPT.format(
                need_place="ja" if need_place else "nein", uncertain=", ".join(uncertain) or "-",
                context="\n---\n".join(c[:500] for c in context) or "-", date=date, text=text[:1500])}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text_of(resp).strip()))
    except Exception as e:
        logger.warning("🪽 KI-Klärung fehlgeschlagen: %s", e)
        return None


def apply_ai_taxa(taxa: list[dict], ai: dict | None) -> list[dict]:
    """Unsichere Arten durch den KI-Vorschlag ersetzen, wenn die Artenliste ihn bestätigt."""
    from utils import species_catalog as SC
    if not ai:
        return taxa
    by_raw = {str(x.get("reported", "")).strip().lower(): x for x in ai.get("taxa") or []}
    out = []
    for t in taxa:
        if t.get("check") == "unknown":
            x = by_raw.get(t["raw"].lower()) or by_raw.get(t["raw"].replace(" sp.", "").lower())
            if x and x.get("species"):
                res = SC.check(f"{x.get('genus') or t['genus']} {x['species']}")
                name = res.get("canonical") if res["status"] in ("accepted", "synonym") else None
                if name:
                    g, s = name.split()[:2]
                    t = {**t, "genus": g, "species": s.lower(), "qualifier": "", "check": "ai"}
        out.append(t)
    return out


def ai_place(ai: dict | None) -> dict | None:
    """KI-Ort → {place, country, region} (Region nur, wenn sie zu unseren Regionen passt)."""
    from utils import swarm as S
    if not ai:
        return None
    cc = (ai.get("country_iso") or "").strip().lower()[:2]
    place = (ai.get("place") or "").strip()[:160]
    reg = S.region_from_name(ai.get("region") or "") or S.region_in_text(ai.get("region") or "")
    if reg and (not cc or cc == reg[0]):
        return {"place": place, "country": reg[0], "region": reg[1]}
    if cc and cc not in S.DACH and re.fullmatch(r"[a-z]{2}", cc):
        return {"place": place, "country": cc, "region": ""}
    if cc in S.DACH or place:
        return {"place": place, "country": cc if cc in S.DACH else "", "region": ""}
    return None


# ── Korrektur von Hand ───────────────────────────────────────────────────────
async def override_of(bot, message_id) -> dict | None:
    r = await execute_db(bot, "SELECT * FROM swarm_override WHERE message_id=?", (str(message_id),), fetch=True)
    return dict(r[0]) if r else None


async def set_override(bot, message_id, place: str, country: str, region: str, taxa: str, by: str) -> None:
    await execute_db(bot, "INSERT INTO swarm_override (message_id, place, country, region, taxa, set_by) "
                          "VALUES (?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET place=excluded.place, "
                          "country=excluded.country, region=excluded.region, taxa=excluded.taxa, "
                          "set_by=excluded.set_by, set_at=datetime('now')",
                     (str(message_id), place, country, region, taxa, by), commit=True)


async def drop_override(bot, message_id) -> None:
    await execute_db(bot, "DELETE FROM swarm_override WHERE message_id=?", (str(message_id),), commit=True)


async def apply_override(bot, message_id) -> int:
    """Gespeicherte Meldung mit der Korrektur neu schreiben (ohne die Discord-Nachricht zu laden)."""
    from utils import swarm as S
    from utils import geocode
    ov = await override_of(bot, message_id)
    rows = [dict(r) for r in await execute_db(bot, "SELECT * FROM swarm_reports WHERE message_id=?",
                                              (str(message_id),), fetch=True) or []]
    if not ov or not rows:
        return 0
    b = rows[0]
    taxa = [{"genus": r["genus"], "species": r["species"], "qualifier": r["qualifier"]} for r in rows]
    if (ov.get("taxa") or "").strip():
        parsed = S.parse_taxa(ov["taxa"])
        if parsed:
            taxa = canon_taxa(parsed)
    p = {"taxa": taxa, "date": b["flight_date"], "time": b["flight_time"], "temp": b["temp_c"], "wind": b["wind"],
         "weather": b["weather"], "place": ov.get("place") or b["place"], "country": ov.get("country") or b["country"],
         "region": ov.get("region") if ov.get("country") else b["region"], "indoor": bool(b["indoor"]),
         "protected": b["protected"]}
    lat = lon = None
    if p["place"] and not p["country"]:
        g, st = await geocode.locate_status(bot, p["place"])
        if st == "abroad":
            p["country"], p["region"] = g["country"], ""
        elif g:
            p["country"], p["region"], lat, lon = g["country"], g.get("region") or "", g["lat"], g["lon"]
    elif p["place"] and p["country"] in S.DACH:
        g = await geocode.locate(bot, p["place"], p["country"], p["region"] or "")
        if g:
            lat, lon = g["lat"], g["lon"]
            p["region"] = p["region"] or g.get("region") or ""
    posted = datetime.strptime(b["posted_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return await S.store(bot, message_id, b["author_id"], posted, p, b["source"], lat, lon, "manual")
