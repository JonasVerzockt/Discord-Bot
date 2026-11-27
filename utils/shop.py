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
utils/shop.py – Shop-Auflösung und CSV-Mapping für den AAM Review Bot.

Auflösungs-Reihenfolge:
  1. shop_mapping.csv  (manuell / auto-gelernt, Groß-/Kleinschreibung egal)
  2. Discord display_name wenn URL-artig
  3. Fuzzy-Match gegen bestehende Sheet-Shopnamen (≥ FUZZY_THRESHOLD)
     – aber nur, wenn der Treffer plausibel zum Namen passt (is_plausible)
  4. UnresolvableShop-Exception

Shopnamen werden immer ohne https:// und ohne www. gespeichert (clean_shop_name).

Verwendung:
    from utils.shop import resolve_shop, extract_identifier, learn_shop, UnresolvableShop
"""
import re
import csv
import logging
from pathlib import Path

import discord
from rapidfuzz import process as fuzz_proc, fuzz

from config import MAPPING_FILE, FUZZY_THRESHOLD
from utils.sheet import sheet

logger = logging.getLogger(__name__)


# ── Eigene Exception ──────────────────────────────────────────────────────────
class UnresolvableShop(Exception):
    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"Unaufgelöst: '{identifier}'")


# ── Interne Helfer ────────────────────────────────────────────────────────────
_CSV_COLS    = ["identifier", "shop_url", "message_id", "hinweis"]
_URL_RE      = re.compile(r'[\w\-]+\.(de|com|net|fr|store|eu|shop|at|ch|nl|pl|es|it|be|co\.uk)\b', re.I)
_MARKDOWN_RE = re.compile(r'\[([^\]]*)\]\(https?://(?:www\.)?([^/)\s]+)[^)]*\)')
_MENTION_RE  = re.compile(r'^<@!?(\d+)>$')
_MENTION_ANY = re.compile(r'<@!?(\d+)>')
_SHOP_LINE   = re.compile(r'Shop[\s*_~`]*[:：]\s*(.*)', re.I)
_TRIM_MD     = re.compile(r'^[\s*_~`|>:\-–]+|[\s*_~`|]+$')
_PURE_URL    = re.compile(r'^[\w\-]+(?:\.[\w\-]+)*\.[a-z]{2,}(?:[/?#].*)?$', re.I)
_IDENT_MAX   = 100

# Allerweltswörter, die allein keinen Shop identifizieren (Grund für die
# Fehlzuordnungen zu "ants from asia/exoticantstore": jedes "… Ants" passte).
_STOP = {
    "ant", "ants", "ameise", "ameisen", "shop", "store", "the", "of", "for",
    "and", "und", "aus", "von", "aka", "by", "www", "http", "https",
    "de", "com", "net", "org", "eu", "fr", "ch", "at", "pl", "es", "it", "nl",
    "be", "uk", "co",
}

# Feste Zusammenlegungen: links (klein geschrieben) wird IMMER zu rechts.
# antcube gehört zum Antstore → wird als antstore.net geführt.
SHOP_ALIASES = {
    "antcube":    "antstore.net",
    "antcube.de": "antstore.net",
}

_map_cache: dict | None = None   # None = noch nicht geladen


def _strip_markdown_url(s: str) -> str:
    """
    Wandelt Discord-Markdown-Links in saubere Domains um:
      [www.antpire.net](https://www.antpire.net) → antpire.net
      [Antpire](https://www.antpire.net/shop)   → antpire.net
    Ohne Match: Originalstring zurück.
    """
    m = _MARKDOWN_RE.search(s)
    return m.group(2) if m else s


def clean_shop_name(s: str) -> str:
    """Shopname so, wie er gespeichert wird: nie mit https:// oder www.

      https://www.terraristikshop.net/  → terraristikshop.net
      https://antomas.es/de/            → antomas.es
      www.Antpire.net                   → antpire.net
      ants from asia/exoticantstore     → unverändert (kein reiner Link)
      antcube.de                        → antstore.net (SHOP_ALIASES)
    """
    s = _strip_markdown_url((s or "").strip()).strip().strip("<>").strip()
    s = re.sub(r'^https?://', '', s, flags=re.I)
    s = re.sub(r'^www\.', '', s, flags=re.I)
    if _PURE_URL.match(s):
        s = re.split(r'[/?#]', s, maxsplit=1)[0].lower()
    s = s.rstrip("/").strip()
    return SHOP_ALIASES.get(s.casefold(), s)


def _key(identifier: str) -> str:
    """Nachschlage-Schlüssel: normalisiert + ohne Groß-/Kleinschreibung."""
    return _normalize_identifier(identifier).casefold()


def _normalize_identifier(s: str) -> str:
    """Vereinheitlicht einen Identifier für Speichern UND Nachschlagen.

    Eine Discord-Mention `<@123>`/`<@!123>` wird zur nackten ID `123` – genau so,
    wie resolve_shop() sie beim Auflösen nachschlägt. Markdown-Links werden zur
    Domain. Mehrzeilige Werte werden auf die erste Zeile gekürzt.
    """
    s = (s or "").strip()
    s = s.splitlines()[0].strip() if s else ""
    m = _MENTION_RE.match(s)
    if m:
        return m.group(1)
    return _strip_markdown_url(s)[:_IDENT_MAX].strip()


def _is_url(s: str) -> bool:
    return bool(_URL_RE.search(s))


_DOMAIN_IN = re.compile(
    r'(?:https?://)?(?:www\.)?((?:[\w\-]+\.)+(?:de|com|net|fr|store|eu|shop|at|ch|nl|pl|es|it|be|co\.uk))\b',
    re.I,
)


def _domain_in(s: str) -> str:
    """Erste Domain aus einem Text: 'www.falle.de - Thomas Carow' → 'falle.de'."""
    m = _DOMAIN_IN.search(_strip_markdown_url(s or ""))
    return m.group(1).lower() if m else ""


# ── Plausibilität ─────────────────────────────────────────────────────────────
def _compact(s: str) -> str:
    return re.sub(r'[^0-9a-zäöüß]', '', (s or "").casefold().replace("&", "and"))


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r'[^0-9a-zäöüß]+', (s or "").casefold())
            if len(t) >= 3 and t not in _STOP}


def _shop_base(shop: str) -> str:
    """special-ants.store → special-ants ; antstore.net → antstore"""
    sh = clean_shop_name(shop)
    if _PURE_URL.match(sh):
        sh = sh.rsplit(".", 1)[0]
        sh = re.sub(r'\.co$', '', sh)
    return sh


def is_plausible(name: str, shop: str) -> bool:
    """Passt ein vom Nutzer geschriebener Shopname zum Shop-Eintrag?

    Bewusst streng: ein gemeinsames Allerweltswort wie "Ants" reicht NICHT.
    Treffer bei: gleicher kompakter Name, Name beginnt mit Shop-Namen (oder
    umgekehrt), gemeinsames markantes Wort, langer gemeinsamer Anfang oder
    kleiner Tippfehler.
    """
    n = clean_shop_name(name)
    base = _shop_base(shop)
    cn, cb = _compact(n), _compact(base)
    if not cn or not cb:
        return False
    if cn == cb:
        return True
    if min(len(cn), len(cb)) >= 4 and (cn.startswith(cb) or cb.startswith(cn)):
        return True
    if _tokens(n) & _tokens(base):
        return True
    pre = 0
    for a, b in zip(cn, cb):
        if a != b:
            break
        pre += 1
    if pre >= 6 and pre >= 0.65 * min(len(cn), len(cb)):
        return True
    return len(cn) >= 6 and fuzz.ratio(cn, cb) >= 85


def _fuzzy(identifier: str) -> str | None:
    if not identifier:
        return None
    candidates = fuzz_proc.extract(
        identifier, sheet.known_shops,
        scorer=fuzz.WRatio, score_cutoff=FUZZY_THRESHOLD,
        processor=str.casefold, limit=8,
    )
    for cand, score, _ in candidates:
        if is_plausible(identifier, cand):
            logger.info(f"🔍 Fuzzy: '{identifier}' → '{cand}' ({score:.0f}%)")
            return clean_shop_name(cand)
        logger.info(f"🔍 Fuzzy verworfen (unplausibel): '{identifier}' ↛ '{cand}' ({score:.0f}%)")
    # Zweiter Versuch: genau EIN bekannter Shop passt plausibel
    # (z. B. "Antstore Berlin" → antstore.net, Tippfehler "Sepcial Ants").
    hits = {clean_shop_name(s) for s in sheet.known_shops if is_plausible(identifier, s)}
    if len(hits) == 1:
        hit = hits.pop()
        logger.info(f"🔍 Eindeutig plausibel: '{identifier}' → '{hit}'")
        return hit
    return None


# ── CSV ───────────────────────────────────────────────────────────────────────
def _read_csv() -> dict:
    """{key (casefold): shop} – nur Zeilen mit ausgefülltem shop_url."""
    if not Path(MAPPING_FILE).exists():
        return {}
    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        result = {}
        for r in csv.DictReader(f):
            raw_url = (r.get("shop_url") or "").strip()
            ident = r.get("identifier") or ""
            if not raw_url or not ident.strip():
                continue
            result.setdefault(_key(ident), clean_shop_name(raw_url))
        return result


def _ensure_trailing_newline(path: str) -> None:
    """Verhindert verklebte Zeilen, wenn die Datei ohne Zeilenumbruch endet
    (z. B. nach manuellem Bearbeiten)."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return
    with open(p, "rb+") as f:
        f.seek(-1, 2)
        if f.read(1) not in (b"\n", b"\r"):
            f.write(b"\n")


def _write_csv_row(identifier: str, shop_url: str, msg_id: str, hint: str) -> bool:
    """Schreibt eine Zeile wenn Identifier noch nicht vorhanden. Gibt True bei Neueinträgen."""
    identifier = _normalize_identifier(identifier)
    if not identifier:
        return False
    key = identifier.casefold()
    exists = Path(MAPPING_FILE).exists()
    if exists:
        with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
            if any(_key(r.get("identifier", "")) == key for r in csv.DictReader(f)):
                return False
        _ensure_trailing_newline(MAPPING_FILE)
    with open(MAPPING_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        if not exists:
            w.writeheader()
        w.writerow({"identifier": identifier,
                    "shop_url": clean_shop_name(shop_url) if shop_url else "",
                    "message_id": msg_id, "hinweis": hint})
    return True


# ── Öffentliche API ───────────────────────────────────────────────────────────
def load_mapping() -> dict:
    """Gibt {key: shop} zurück (key = Identifier in Kleinschreibung) – gecacht."""
    global _map_cache
    if _map_cache is None:
        _map_cache = _read_csv()
    return _map_cache


def mapping_get(identifier: str) -> str | None:
    """Nachschlagen ohne Unterscheidung von Groß-/Kleinschreibung."""
    if not identifier:
        return None
    return load_mapping().get(_key(identifier))


def reload_mapping() -> dict:
    """Erzwingt Neu-Einlesen der CSV (z.B. nach manuellem Ausfüllen durch User)."""
    global _map_cache
    _map_cache = None
    return load_mapping()


def add_to_csv(identifier: str, msg_id: str, hint: str = "") -> None:
    """Unbekannter Shop: leer eintragen zum manuellen Ausfüllen."""
    if _write_csv_row(identifier, "", msg_id, hint):
        logger.info(f"📋 CSV: '{identifier}' zum Ausfüllen hinzugefügt")


def _read_all_rows() -> list:
    """Liest ALLE CSV-Zeilen (auch mit leerem shop_url) als Liste von Dicts."""
    if not Path(MAPPING_FILE).exists():
        return []
    with open(MAPPING_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_all_rows(rows: list) -> None:
    """Schreibt die komplette CSV neu (Header + alle Zeilen)."""
    with open(MAPPING_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c, "") or "") for c in _CSV_COLS})


def set_mapping(identifier: str, shop_url: str,
                msg_id: str = "manual", hint: str = "manuell gesetzt") -> None:
    """
    Setzt/aktualisiert die Zuordnung identifier → shop_url (Upsert) und lädt
    den In-Memory-Cache neu, damit die Änderung sofort greift (ohne Neustart).
    """
    identifier = _normalize_identifier(identifier)
    key        = identifier.casefold()
    shop_url   = clean_shop_name(shop_url)
    rows = _read_all_rows()
    for r in rows:
        if _key(r.get("identifier", "")) == key:
            r["identifier"] = identifier          # ggf. alte `<@…>`-Schreibweise heilen
            r["shop_url"]   = shop_url
            r["hinweis"]    = hint or r.get("hinweis", "")
            break
    else:
        rows.append({"identifier": identifier, "shop_url": shop_url,
                     "message_id": msg_id, "hinweis": hint})
    _write_all_rows(rows)
    reload_mapping()


def remove_mapping(identifier: str) -> bool:
    """Entfernt eine Zuordnung. True wenn etwas entfernt wurde. Lädt Cache neu."""
    key  = _key(identifier)
    rows = _read_all_rows()
    kept = [r for r in rows if _key(r.get("identifier", "")) != key]
    if len(kept) == len(rows):
        return False
    _write_all_rows(kept)
    reload_mapping()
    return True


def all_mappings() -> list:
    """Alle Zeilen als [(identifier, shop_url), …] (auch leere shop_url)."""
    return [(r.get("identifier", "").strip(), (r.get("shop_url", "") or "").strip())
            for r in _read_all_rows()]


def learn_shop(identifier: str, shop_url: str) -> None:
    """Identifier → Shop dauerhaft speichern (ohne Prüfung; Prüfung macht
    learn_if_plausible)."""
    identifier = _normalize_identifier(identifier)
    shop_url   = clean_shop_name(shop_url)
    if not identifier or not shop_url:
        return
    mapping = load_mapping()
    key = identifier.casefold()
    if key in mapping:
        return
    if _write_csv_row(identifier, shop_url, "auto", "auto-gelernt"):
        mapping[key] = shop_url
        logger.info(f"📚 Gelernt: '{identifier}' → '{shop_url}'")


def shop_names_in(content: str, guild: discord.Guild | None = None) -> list[str]:
    """Alle Namen, unter denen die Nachricht den Shop nennt: Text der Shop-Zeile
    (ohne Mentions) und – bei Mention – der Discord-Anzeigename."""
    names: list[str] = []
    line = _shop_line(content) or ""
    rest = _TRIM_MD.sub("", _MENTION_ANY.sub(" ", line)).strip(" /|-–,")
    if rest:
        names.append(rest)
    uid = _mention_id(content)
    if uid and guild is not None:
        member = guild.get_member(int(uid))
        if member:
            for n in (member.display_name, member.global_name, member.name):
                if n and n not in names:
                    names.append(n)
    return names


def learn_if_plausible(identifier: str, sheet_shop: str, content: str,
                       guild: discord.Guild | None = None) -> bool:
    """Lernt identifier → sheet_shop nur, wenn der Shop zum Namen in der
    Nachricht passt. Gibt False zurück, wenn es zur manuellen Prüfung muss."""
    if not identifier or not sheet_shop:
        return False
    if mapping_get(identifier):
        return True
    names = shop_names_in(content, guild)
    if not identifier.isdigit():
        names.insert(0, identifier)
    if any(is_plausible(n, sheet_shop) for n in names):
        learn_shop(identifier, sheet_shop)
        return True
    logger.warning(
        f"⚠️  Nicht gelernt (unplausibel): '{identifier}' ↛ '{sheet_shop}' "
        f"(Namen: {names or '–'})"
    )
    return False


def _shop_line(content: str) -> str | None:
    """Text hinter 'Shop:' – nur die erste Zeile. Steht der Wert (z. B. bei
    `__🛒 Shop:__`) erst in der nächsten Zeile, wird diese genommen."""
    lines = (content or "").splitlines()
    for i, line in enumerate(lines):
        m = _SHOP_LINE.search(line)
        if not m:
            continue
        val = _TRIM_MD.sub("", m.group(1))
        if not val:
            for nxt in lines[i + 1:i + 3]:
                val = _TRIM_MD.sub("", nxt)
                if val:
                    break
        return val[:_IDENT_MAX].strip() or None
    return None


def _mention_id(content: str) -> str | None:
    """Mention bevorzugt aus der Shop-Zeile, sonst die erste im Text."""
    line = _shop_line(content) or ""
    ids = _MENTION_ANY.findall(line) or _MENTION_ANY.findall(content or "")
    return ids[0] if ids else None


def extract_identifier(content: str) -> str | None:
    """Extrahiert den rohen Identifier (Discord-ID oder Text nach 'Shop:') ohne Auflösung."""
    uid = _mention_id(content)
    if uid:
        return uid
    line = _shop_line(content)
    if line:
        return _normalize_identifier(line) or None
    return None


def resolve_shop(content: str, guild: discord.Guild) -> str:
    """
    Löst den Shop aus dem Nachrichteninhalt auf.

    Auflösungs-Reihenfolge:
      1. shop_mapping.csv
      2. Discord display_name (wenn URL-artig)
      3. Fuzzy-Match gegen bekannte Sheet-Shopnamen (nur plausible Treffer)
      4. → UnresolvableShop
    """
    # Variante A: Discord-Mention (@User)
    uid = _mention_id(content)
    if uid:
        hit = mapping_get(uid)
        if hit:
            return hit
        for name in shop_names_in(content, guild):
            hit = mapping_get(name)
            if hit:
                return hit
            if _is_url(name):
                return _domain_in(name) or clean_shop_name(name)
            fuzzy = _fuzzy(name)
            if fuzzy:
                return fuzzy
        raise UnresolvableShop(uid)

    # Variante B: "Shop: <name>"
    line = _shop_line(content)
    if line:
        raw = _normalize_identifier(line)
        hit = mapping_get(raw)
        if hit:
            return hit
        if _is_url(raw):
            return _domain_in(raw) or clean_shop_name(raw)
        fuzzy = _fuzzy(raw)
        if fuzzy:
            return fuzzy
        raise UnresolvableShop(raw)

    raise UnresolvableShop("(kein Shop erkannt)")
