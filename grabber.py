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
grabber.py - AntCheck API Datenabholer.

Wird als Cron-Job oder per Hand ausgefuehrt (NICHT Teil des Bots selbst).
Lädt Shops + Produkte von der AntCheck API und speichert das Ergebnis
als shops_data.json im DATA_DIRECTORY.

Typischer Aufruf (crontab):
  0 * * * * cd /opt/discord-bot && .venv/bin/python grabber.py

Umgebungsvariablen:
  ANTCHECK_API_KEY   - API-Key (Pflicht)
  ANTCHECK_API_URL   - Basis-URL (Standard: https://antcheck.info)
  ANTCHECK_VERIFY_SSL- SSL-Zertifikat prüfen (Standard: true)
  DATA_DIRECTORY     - Zielverzeichnis für shops_data.json
"""
import json
import logging
import os
import sqlite3
import sys
import time
import re
import html
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("grabber")

# ── Konfiguration ─────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY         = os.getenv("ANTCHECK_API_KEY", "")
API_BASE        = os.getenv("ANTCHECK_API_URL", "https://antcheck.info").rstrip("/")
API_TIMEOUT     = int(os.getenv("ANTCHECK_TIMEOUT", "30"))
API_RETRIES     = int(os.getenv("ANTCHECK_RETRIES", "3"))
API_RETRY_DELAY = float(os.getenv("ANTCHECK_RETRY_DELAY", "5"))
API_VERIFY_SSL  = os.getenv("ANTCHECK_VERIFY_SSL", "true").lower() not in ("0", "false", "no")
# Datenordner: zentral in data/ (wie der Bot). Über DATA_DIR/DATA_DIRECTORY
# verlegbar; DATA_DIR=. behält das alte Root-Layout.
_BASE_DIR         = Path(__file__).parent
DATA_DIRECTORY    = os.getenv("DATA_DIRECTORY", os.getenv("DATA_DIR", str(_BASE_DIR / "data")))
Path(DATA_DIRECTORY).mkdir(parents=True, exist_ok=True)
OUTPUT_FILE       = Path(DATA_DIRECTORY) / "shops_data.json"
PRICE_HISTORY_DB  = Path(DATA_DIRECTORY) / "price_history.db"

# Bestehende Root-Dateien einmalig nach data/ verschieben (idempotent), damit
# der Grabber unabhängig vom Bot-Start dieselben (historischen) Daten nutzt.
try:
    from utils.paths import migrate_legacy_files as _migrate_grabber
    _migrate_grabber({
        _BASE_DIR / "shops_data.json":  OUTPUT_FILE,
        _BASE_DIR / "price_history.db": PRICE_HISTORY_DB,
        _BASE_DIR / "ant_species.json": Path(DATA_DIRECTORY) / "ant_species.json",
    })
except Exception as _e:
    logging.warning("⚠️ Grabber-Daten-Migration übersprungen: %s", _e)

SHOPS_URL    = f"{API_BASE}/api/v2/ecommerce/shops?online=true&crawler_active=true&page=0&limit=-1&api_key={API_KEY}"
PRODUCTS_URL = f"{API_BASE}/api/v2/ecommerce/products?shop_id={{shop_id}}&product_type=ants&page=0&limit=-1&api_key={API_KEY}"
# Varianten werden global (nicht pro Shop) geladen und nach product_id gruppiert.
VARIANTS_URL = f"{API_BASE}/api/v2/ecommerce/variants?page=0&limit=-1&api_key={API_KEY}"

if not API_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── HTTP-Helfer ───────────────────────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text) -> str:
    """Entfernt HTML-Tags/Entities aus Shop-Texten (Titel/Beschreibung)."""
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", str(text))
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# ── Artenliste (optional): kanonischen Artnamen mitschreiben ──────────────────
# Lädt data/ant_species.json (von tools/build_ant_species.py erzeugt) eigenständig
# – bewusst OHNE den Bot-Stack zu importieren. Fehlt die Datei, bleibt
# canonical_species schlicht None (kein Fehler).
_SPECIES_FILE = str(Path(DATA_DIRECTORY) / "ant_species.json")
_SP_LOADED = False
_SP_ACCEPTED: dict[str, str] = {}
_SP_SYNONYMS: dict[str, str] = {}
_SP_GENERA: dict[str, str] = {}          # Gattung (lower) -> Anzeigename (für Gattungs-Fallback)
_SP_BY_GENUS: dict[str, list[str]] = {}   # Gattung -> akzeptierte Epitheta (für Fuzzy)
_SP_EPITHETS: set[str] = set()            # ALLE akzeptierten Epitheta (gattungsübergreifend)


def _load_species_catalog() -> None:
    global _SP_LOADED, _SP_ACCEPTED, _SP_SYNONYMS, _SP_GENERA, _SP_BY_GENUS, _SP_EPITHETS
    if _SP_LOADED:
        return
    _SP_LOADED = True
    try:
        with open(_SPECIES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _SP_ACCEPTED = {k.lower(): v for k, v in data.get("accepted", {}).items()}
        _SP_SYNONYMS = {k.lower(): v for k, v in data.get("synonyms", {}).items()}
        _SP_GENERA = {k.lower(): v for k, v in data.get("genera", {}).items()}
        by: dict[str, set] = {}
        for key in _SP_ACCEPTED:                       # "gattung epitheton"
            g, _, ep = key.partition(" ")
            if ep:
                by.setdefault(g, set()).add(ep)
                _SP_EPITHETS.add(ep)
        _SP_BY_GENUS = {g: sorted(s) for g, s in by.items()}
        logging.info("🐜 Artenliste geladen: %d Arten (canonical_species aktiv)", len(_SP_ACCEPTED))
    except FileNotFoundError:
        logging.info("🐜 Keine Artenliste (data/ant_species.json) – canonical_species=null")
    except Exception as e:
        logging.warning("🐜 Artenliste nicht lesbar: %s", e)


# Max. erlaubte Editierdistanz für die Tippfehler-Korrektur im Shop-Artnamen.
_MAX_EDITS = 2

# Commerce-/Kasten-Wörter, die KEIN Epitheton sind und als Token entfernt werden
# (sonst würde z.B. „Pheidole colony" fälschlich auf „Pheidole dolon" gefuzzt).
# Bewusst OHNE „major"/„minor" – das SIND echte Epitheta. Alle Einträge sind gegen
# den AntCat-Katalog als Nicht-Epitheton geprüft.
_NOISE_WORDS = {
    "colony", "colonies", "colonie", "colonia", "kolonie", "kolonien",
    "queen", "queens", "gyne", "gynes", "worker", "workers", "workerless",
    "soldier", "soldiers", "majors", "minors", "starter", "kit", "kits",
    "set", "sets", "bundle", "nanitic", "nanitics", "founding",
}

# Manuelle Overrides für Fälle, die der automatische Fuzzy nicht sicher auflösen kann
# (z.B. mehrdeutige Tippfehler oder bekannte Fehlzuordnungen). Key = „gattung epitheton"
# (kleingeschrieben). Wert = akzeptierter Name -> ERZWINGT diese Korrektur; None -> BLOCKT
# jede Fuzzy-Korrektur (bleibt canonical_species=null). Hier bei Bedarf ergänzen.
_OVERRIDES: dict[str, str | None] = {
    # Shop-Tippfehler bestätigt (Beschreibung: „Monomorium chinense", Ostasien/China);
    # per Distanz mehrdeutig zu „chilense", daher fest zugeordnet:
    "monomorium chiense": "Monomorium chinense",
    # „Dendrolasius" ist eine Untergattung von Lasius -> Lasius fuliginosus:
    "dendrolasius fuliginosus": "Lasius fuliginosus",
    # Gattung für Auto-Fuzzy mehrdeutig, Epitheton „astutus" akzeptiert nur bei Ectomomyrmex:
    "ectomyrmex astutus": "Ectomomyrmex astutus",
    # „C." abgekürzt; „fedtschenkoi" akzeptiert nur bei Camponotus (AntWiki: C. fedtschenkoi):
    "c fedschenkoi": "Camponotus fedtschenkoi",
}


def _is_ending_variant(a: str, b: str) -> bool:
    """Unterscheiden sich a und b nur in der ENDUNG (gemeinsamer Präfix ≥ Länge − 2)?
    Deckt Genus-/Endungsangleichungen ab (niger↔nigra, hispanicus↔hispanica) und grenzt
    sie von echten Art-Wechseln ab (chinensis↔chilensis: gemeinsamer Präfix nur 3)."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= max(len(a), len(b)) - 2


def _osa(a: str, b: str) -> int:
    """Optimal-String-Alignment-Distanz (Damerau-Levenshtein mit Nachbar-Dreher als
    EIN Schritt). Kleine Strings -> volle DP-Matrix ist völlig ausreichend."""
    la, lb = len(a), len(b)
    if a == b:
        return 0
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)   # benachbarte Umstellung
    return d[la][lb]


def _canonical_species(species_name: str) -> str | None:
    """Zieht aus dem (evtl. verrauschten) Artnamen das enthaltene bekannte Binomen
    und gibt den akzeptierten Namen zurück (Synonyme aufgelöst). Sonst None.

    Zweistufig:
      1) EXAKT: bekanntes Binomen (accepted/synonym) im Namen.
      2) FUZZY-Fallback (konservativ), falls (1) nichts findet – fängt Tippfehler im
         SHOP-eigenen Artnamen ab (z.B. „Monomorium chiense" -> „Monomorium chinense",
         „Lasius nigar/nigre" -> „Lasius niger"): Gattung muss EXAKT eine bekannte
         Gattung sein, das Epitheton (≥ 4 Zeichen) darf max. _MAX_EDITS Editierschritte
         (Ersetzen/Einfügen/Löschen/Nachbar-Dreher) abweichen. Korrigiert wird nur auf
         den EINDEUTIG nächsten Treffer – teilen sich zwei die kleinste Distanz, bleibt
         es unkorrigiert."""
    _load_species_catalog()
    if not _SP_ACCEPTED:
        return None
    # Klammer-Inhalte werden NICHT entfernt: manche Shops schreiben die echte Art in
    # Klammern (z.B. „Lesser Red Carpenter Ant (Camponotus decipiens)") – die soll
    # gefunden werden. Reine Kommentare in Klammern („(Tococa)", „(helle Variante)")
    # schaden nicht, weil sie kein bekanntes Binomen ergeben bzw. via „sp." nur zur
    # Gattung führen.
    raw = species_name or ""
    # „sp."/„spp."/„ssp." = Art UNBESTIMMT -> es darf KEIN Epitheton bestimmt werden,
    # nur die Gattung (z.B. „Crematogaster sp. (Tococa)" -> Gattung, nicht „…torosa").
    # „cf."/„aff." meinen dagegen eine konkrete Art -> normal weiter auflösen.
    genus_only = re.search(r"(?<!\w)(sp|spp|ssp|subsp)\.?(?!\w)", raw, re.IGNORECASE) is not None
    # Bestimmungs-Qualifier als GANZE Token entfernen; nur eigenständige Token –
    # „affinis"/„affiche" bleiben unangetastet (identisch zu utils.normalize_species_name).
    cleaned = re.sub(r"(?<!\w)(cf|aff|sp|spp|ssp|subsp)\.?(?!\w)", " ", raw, flags=re.IGNORECASE)
    toks = [t for t in re.sub(r"[^A-Za-zÀ-ÿ ]", " ", cleaned).lower().split()
            if t.isalpha() and t not in _NOISE_WORDS]
    # 0) Manuelle Overrides: erzwingen (Wert=Name) oder blocken (Wert=None).
    blocked = set()
    for i in range(len(toks) - 1):
        pair = f"{toks[i]} {toks[i + 1]}"
        if pair in _OVERRIDES:
            val = _OVERRIDES[pair]
            if val:
                logging.info("🐜 canonical_species Override: %r -> %r", pair, val)
                return val
            blocked.add(pair)                      # explizit nicht korrigieren
    # 1) Exakt (nur wenn eine Art gemeint ist – nicht bei „sp.")
    if not genus_only:
      for i in range(len(toks) - 1):
        cand = f"{toks[i]} {toks[i + 1]}"
        if cand in _SP_ACCEPTED:
            return _SP_ACCEPTED[cand]
        if cand in _SP_SYNONYMS:   # auch Synonym-Gattungen
            return _SP_SYNONYMS[cand]
    # 2) Fuzzy-Fallback: exakte Gattung + Epitheton max. _MAX_EDITS Schritte, eindeutig nächster.
    for i in (range(len(toks) - 1) if not genus_only else range(0)):
        g, ep = toks[i], toks[i + 1]
        if f"{g} {ep}" in blocked:                 # per Override von Fuzzy ausgenommen
            continue
        cands = _SP_BY_GENUS.get(g)
        if not cands or len(ep) < 4:               # zu kurze Epitheta nicht raten
            continue
        scored = []
        for acc in cands:
            if abs(len(acc) - len(ep)) > _MAX_EDITS:   # Längendiff macht Distanz unmöglich klein
                continue
            dist = _osa(ep, acc)
            if dist <= _MAX_EDITS:
                scored.append((dist, acc))
        if not scored:
            continue
        scored.sort()
        best_dist = scored[0][0]
        closest = [acc for dist, acc in scored if dist == best_dist]
        if len(closest) != 1:                      # nicht eindeutig -> keine Korrektur
            logging.info("🐜 canonical_species Fuzzy mehrdeutig (Distanz %d), keine Korrektur: %r -> %s",
                         best_dist, f"{g} {ep}", closest)
            continue
        # Schutz vor Art-Verwechslung: Ist das Epitheton SELBST ein echtes (akzeptiertes)
        # Epitheton – nur in einer anderen Gattung – dann NICHT auf eine fremde Art
        # umbiegen; erlaubt bleibt nur eine reine Endungs-/Gender-Variante.
        if ep in _SP_EPITHETS and not _is_ending_variant(ep, closest[0]):
            logging.info("🐜 canonical_species: %r ist ein echtes Epitheton (andere Gattung), "
                         "keine Art-Korrektur zu %r", f"{g} {ep}", closest[0])
            continue
        corrected = _SP_ACCEPTED[f"{g} {closest[0]}"]
        logging.info("🐜 canonical_species Tippfehler-Korrektur (Distanz %d): %r -> %r",
                     best_dist, f"{g} {ep}", corrected)
        return corrected
    # 3) Gattungs-Fuzzy: die GATTUNG selbst ist verschrieben -> nächste bekannte Gattung
    #    (eindeutig, Damerau ≤ _MAX_EDITS, ab 5 Zeichen), dann die Art innerhalb dieser
    #    korrigierten Gattung bestimmen (exakt/Fuzzy) bzw. wenigstens die Gattung setzen.
    if toks:
        gtok = toks[0]
        if gtok not in _SP_GENERA and len(gtok) >= 5:
            gs = sorted((_osa(gtok, gg), gg) for gg in _SP_GENERA
                        if abs(len(gg) - len(gtok)) <= _MAX_EDITS)
            gs = [(d, gg) for d, gg in gs if d <= _MAX_EDITS]
            if gs and len([gg for d, gg in gs if d == gs[0][0]]) == 1:
                gbest, g2 = gs[0]
                ep = toks[1] if len(toks) >= 2 else None
                result = None
                if ep and not genus_only:            # bei „sp." keine Art bestimmen
                    key = f"{g2} {ep}"
                    if key in _SP_ACCEPTED:
                        result = _SP_ACCEPTED[key]
                    elif key in _SP_SYNONYMS:
                        result = _SP_SYNONYMS[key]
                    else:
                        cands = _SP_BY_GENUS.get(g2)
                        if cands and len(ep) >= 4:
                            es = sorted((_osa(ep, a), a) for a in cands
                                        if abs(len(a) - len(ep)) <= _MAX_EDITS)
                            es = [(d, a) for d, a in es if d <= _MAX_EDITS]
                            if es and len([a for d, a in es if d == es[0][0]]) == 1:
                                cand_ep = es[0][1]
                                if not (ep in _SP_EPITHETS and not _is_ending_variant(ep, cand_ep)):
                                    result = _SP_ACCEPTED[f"{g2} {cand_ep}"]
                # Aus einer NUR gefuzzten Gattung nicht blind eine Gattung raten: nur
                # übernehmen, wenn dabei eine echte Art herauskam ODER „sp." vorlag
                # (unbestimmte Art). Sonst würden Nicht-Arten wie „Lesser …" oder
                # „Formicarium" fälschlich zu Gattungen (Messor/Formicium) gebogen.
                if result is None and genus_only:
                    result = _SP_GENERA[g2]          # explizit „sp." -> korrigierte Gattung
                if result is not None:
                    logging.info("🐜 canonical_species Gattungs-Korrektur (Distanz %d): %r -> %r",
                                 gbest, (species_name or "").strip(), result)
                    return result
    # 4) Gattungs-Fallback: kein Binomen bestimmbar (unbekannte/mehrdeutige Art oder
    #    „sp." ohne Epitheton), aber die Gattung ist EXAKT bekannt -> wenigstens die
    #    GATTUNG setzen. Das Rohfeld zeigt weiterhin cf./sp./aff. (Unsicherheit sichtbar).
    for t in toks:
        disp = _SP_GENERA.get(t)
        if disp:
            return disp
    return None


def _fetch_json(url: str) -> dict | list:
    """Holt JSON von der URL mit Retry-Logik."""
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=API_TIMEOUT, verify=API_VERIFY_SSL)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"⚠️ Versuch {attempt}/{API_RETRIES} fehlgeschlagen: {e}")
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_DELAY)
    raise RuntimeError(f"API nach {API_RETRIES} Versuchen nicht erreichbar: {url.split('?')[0]}")


# ── Datenverarbeitung ─────────────────────────────────────────────────────────

def build_shop_map(shops_raw: list) -> dict:
    """Baut die Shop-Map aus der Shops-API-Antwort auf."""
    result = {}
    for shop in shops_raw:
        sid = str(shop.get("id", ""))
        if not sid:
            continue
        result[sid] = {
            "id":             sid,
            "name":           shop.get("name", ""),
            "country":        (shop.get("country") or shop.get("country_code") or "").lower(),
            "url":            shop.get("url") or shop.get("website") or "",
            "average_rating": shop.get("rating") or shop.get("average_rating"),
            "products":       [],
        }
    return result


def _variant_entry(v: dict, product_currency: str) -> dict:
    """Normalisiert einen Varianten-Datensatz aus /ecommerce/variants."""
    price = v.get("price")
    if price is None:
        price = v.get("min_price") or v.get("amount") or "0"
    return {
        "id":           v.get("id"),
        "title":        _strip_html(v.get("title") or ""),
        "description":  _strip_html(v.get("description") or ""),
        "price":        str(price),
        "currency_iso": v.get("currency_iso") or v.get("currency") or product_currency,
        "url":          v.get("url") or v.get("antcheck_url") or "",
        "in_stock":     bool(v.get("in_stock", False)),
        "is_active":    bool(v.get("is_active", False)),
    }


def _variant_span(variants: list, fb_min: float, fb_max: float):
    """min/max aus lagernden, aktiven Varianten mit Preis>0; sonst Fallback (AntCheck)."""
    prices = []
    for v in variants:
        if not (v.get("in_stock") and v.get("is_active")):
            continue
        try:
            p = float(str(v.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if p > 0:
            prices.append(p)
    if prices:
        return min(prices), max(prices)
    return fb_min, fb_max


def fetch_variants_by_product() -> dict:
    """
    Holt alle Produkt-Varianten global (/ecommerce/variants?limit=-1) und
    gruppiert sie nach product_id. Faellt der Endpoint aus, wird eine leere Map
    zurueckgegeben (Produkte werden trotzdem geschrieben – abwaertskompatibel).
    """
    try:
        raw = _fetch_json(VARIANTS_URL)
    except Exception as e:
        logger.warning(f"⚠️ Varianten-Abruf fehlgeschlagen (nicht kritisch): {e}")
        return {}
    if not isinstance(raw, list):
        raw = raw.get("data", raw.get("variants", []))
    by_pid: dict = {}
    for v in raw:
        if not isinstance(v, dict):
            continue
        pid = v.get("product_id")
        if pid is None:
            continue
        by_pid.setdefault(pid, []).append(v)
    return by_pid


def add_products(shop_map: dict, shop_id: str, products_raw: list,
                 variants_by_pid: dict | None = None) -> None:
    """Fuegt Produkte (inkl. Varianten) zu einem Shop in der Map hinzu."""
    if shop_id not in shop_map:
        return
    variants_by_pid = variants_by_pid or {}
    for p in products_raw:
        species_name = (
            p.get("species_name") or p.get("name") or p.get("title") or ""
        ).strip()
        # Varianteninfo: description/comment falls vorhanden, sonst Artname
        description = _strip_html(p.get("description") or p.get("comment") or "")
        product_title = _strip_html(p.get("name") or p.get("title") or species_name)
        genus = species_name.split()[0] if " " in species_name else species_name
        currency = p.get("currency_iso") or p.get("currency") or "EUR"
        pid = p.get("id")
        variants = [_variant_entry(v, currency) for v in variants_by_pid.get(pid, [])]
        try:
            _fb_min = float(p.get("min_price") or p.get("price") or 0)
        except (TypeError, ValueError):
            _fb_min = 0.0
        try:
            _fb_max = float(p.get("max_price") or p.get("price") or 0)
        except (TypeError, ValueError):
            _fb_max = 0.0
        # Preisspanne bevorzugt aus lagernden Varianten (schließt 0€/ausverkauft aus)
        _span_min, _span_max = _variant_span(variants, _fb_min, _fb_max)
        shop_map[shop_id]["products"].append({
            "id":            pid,
            "species":       species_name,
            "canonical_species": _canonical_species(species_name),
            "title":         product_title,
            "description":   description,
            "genus":         genus,
            "min_price":     str(_span_min),
            "max_price":     str(_span_max),
            "currency_iso":  currency,
            "antcheck_url":  p.get("antcheck_url") or p.get("url") or "",
            "shop_url":      p.get("product_url") or p.get("shop_url") or "",
            "in_stock":      bool(p.get("in_stock", False)),
            "is_active":     bool(p.get("is_active", False)),
            "variants":      variants,
        })


# ── Preis-Tracking ────────────────────────────────────────────────────────────

_PRICE_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    min_price    REAL    NOT NULL,
    max_price    REAL    NOT NULL,
    currency_iso TEXT    NOT NULL DEFAULT 'EUR',
    recorded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pph_product
    ON product_price_history(product_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS variant_price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id   INTEGER NOT NULL,
    product_id   INTEGER NOT NULL,
    price        REAL    NOT NULL,
    currency_iso TEXT    NOT NULL DEFAULT 'EUR',
    recorded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vph_variant
    ON variant_price_history(variant_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS variant_snapshot (
    product_id INTEGER NOT NULL,
    variant_id INTEGER NOT NULL,
    title      TEXT,
    price      REAL,
    PRIMARY KEY (product_id, variant_id)
);

CREATE TABLE IF NOT EXISTS product_price_reason (
    product_id    INTEGER PRIMARY KEY,
    recorded_at   TEXT,
    direction     TEXT,
    code          TEXT,
    variant_title TEXT,
    old_price     REAL,
    new_price     REAL,
    currency_iso  TEXT
);
"""

def _instock_variants(product: dict) -> dict:
    """{variant_id: (title, price)} nur lagernde, aktive Varianten mit Preis>0."""
    out = {}
    for v in product.get("variants", []):
        if not (v.get("in_stock") and v.get("is_active")):
            continue
        vid = v.get("id")
        if vid is None:
            continue
        try:
            price = float(str(v.get("price")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if price > 0:
            out[vid] = ((v.get("title") or v.get("description") or "").strip(), price)
    return out


def _write_snapshot(cur, pid, product) -> None:
    cur.execute("DELETE FROM variant_snapshot WHERE product_id=?", (pid,))
    rows = [(pid, vid, t, pr) for vid, (t, pr) in _instock_variants(product).items()]
    if rows:
        cur.executemany(
            "INSERT INTO variant_snapshot (product_id, variant_id, title, price) VALUES (?,?,?,?)",
            rows,
        )


def _classify_reason(cur, pid, product, old_min, old_max, new_min, new_max):
    """
    Bestimmt den Grund einer Spannen-Aenderung durch Diff des lagernden
    Varianten-Satzes (alt=Snapshot, neu=aktuell). Rueckgabe (code, title, old, new) oder None.
    """
    cur.execute("SELECT variant_id, title, price FROM variant_snapshot WHERE product_id=?", (pid,))
    old = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    if not old:
        return None
    new = _instock_variants(product)
    if not new:
        return None
    common = set(old) & set(new)
    up = (new_min + new_max) > (old_min + old_max)
    if up:
        inc = [(vid, new[vid][0], old[vid][1], new[vid][1]) for vid in common if new[vid][1] > old[vid][1] + 1e-6]
        if inc:
            vid, title, op, np = max(inc, key=lambda x: x[3] - x[2])
            return ("price_up", title, op, np)
        cheapest = min(old.items(), key=lambda kv: kv[1][1])
        if cheapest[0] not in new:
            return ("cheapest_gone", cheapest[1][0], None, None)
        newcomers = [vid for vid in new if vid not in old]
        if newcomers:
            vid = max(newcomers, key=lambda v: new[v][1])
            return ("new_expensive", new[vid][0], None, None)
        return None
    else:
        dec = [(vid, new[vid][0], old[vid][1], new[vid][1]) for vid in common if new[vid][1] < old[vid][1] - 1e-6]
        if dec:
            vid, title, op, np = min(dec, key=lambda x: x[3] - x[2])
            return ("price_down", title, op, np)
        newcomers = [vid for vid in new if vid not in old]
        if newcomers:
            vid = min(newcomers, key=lambda v: new[v][1])
            return ("new_cheaper", new[vid][0], None, None)
        dearest = max(old.items(), key=lambda kv: kv[1][1])
        if dearest[0] not in new:
            return ("expensive_gone", dearest[1][0], None, None)
        return None


def _store_reason(cur, pid, reason, currency) -> None:
    code, title, op, np = reason
    direction = "down" if code in ("price_down", "new_cheaper", "expensive_gone") else "up"
    cur.execute(
        "INSERT OR REPLACE INTO product_price_reason "
        "(product_id, recorded_at, direction, code, variant_title, old_price, new_price, currency_iso) "
        "VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?)",
        (pid, direction, code, title, op, np, currency),
    )


def _track_prices(shop_map: dict) -> tuple[int, int]:
    """
    Vergleicht aktuelle Preise mit dem letzten Eintrag in price_history.db.
    Schreibt nur einen neuen Eintrag wenn sich der Preis geaendert hat.
    Produkte mit Preis 0 werden ignoriert.
    Gibt (neue Einträge, gecheckte Produkte) zurück.
    """
    conn = sqlite3.connect(PRICE_HISTORY_DB)
    try:
        conn.executescript(_PRICE_HISTORY_SCHEMA)
        conn.commit()
        cur = conn.cursor()

        new_entries = 0
        checked = 0
        new_variant_entries = 0
        checked_variants = 0

        for shop in shop_map.values():
            for p in shop.get("products", []):
                pid = p.get("id")
                if pid is None:
                    continue
                try:
                    min_p = float(p.get("min_price") or 0)
                    max_p = float(p.get("max_price") or 0)
                except (TypeError, ValueError):
                    continue
                # 0€ ignorieren
                if min_p == 0.0 and max_p == 0.0:
                    continue

                currency = p.get("currency_iso") or "EUR"
                checked += 1

                # Letzten Eintrag holen
                cur.execute(
                    "SELECT min_price, max_price FROM product_price_history "
                    "WHERE product_id=? ORDER BY recorded_at DESC LIMIT 1",
                    (pid,),
                )
                last = cur.fetchone()

                if last is None:
                    cur.execute(
                        "INSERT INTO product_price_history "
                        "(product_id, min_price, max_price, currency_iso) VALUES (?,?,?,?)",
                        (pid, min_p, max_p, currency),
                    )
                    _write_snapshot(cur, pid, p)
                    new_entries += 1
                elif last[0] != min_p or last[1] != max_p:
                    cur.execute(
                        "INSERT INTO product_price_history "
                        "(product_id, min_price, max_price, currency_iso) VALUES (?,?,?,?)",
                        (pid, min_p, max_p, currency),
                    )
                    reason = _classify_reason(cur, pid, p, last[0], last[1], min_p, max_p)
                    if reason:
                        _store_reason(cur, pid, reason, currency)
                    _write_snapshot(cur, pid, p)
                    new_entries += 1
                else:
                    # Unveraenderte Produkte: Snapshot einmalig seeden (Erststart
                    # nach Deploy), damit schon die ERSTE kuenftige Aenderung einen
                    # Grund liefern kann. Existiert bereits einer -> Baseline behalten.
                    cur.execute("SELECT 1 FROM variant_snapshot WHERE product_id=? LIMIT 1", (pid,))
                    if cur.fetchone() is None:
                        _write_snapshot(cur, pid, p)

                # Varianten-Historie (Einzelpreise) – nur bei Preisaenderung
                for v in p.get("variants", []):
                    vid = v.get("id")
                    if vid is None:
                        continue
                    try:
                        vprice = float(v.get("price") or 0)
                    except (TypeError, ValueError):
                        continue
                    if vprice == 0.0:
                        continue
                    vcur = v.get("currency_iso") or currency
                    checked_variants += 1
                    cur.execute(
                        "SELECT price FROM variant_price_history "
                        "WHERE variant_id=? ORDER BY recorded_at DESC LIMIT 1",
                        (vid,),
                    )
                    vlast = cur.fetchone()
                    if vlast is None or vlast[0] != vprice:
                        cur.execute(
                            "INSERT INTO variant_price_history "
                            "(variant_id, product_id, price, currency_iso) VALUES (?,?,?,?)",
                            (vid, pid, vprice, vcur),
                        )
                        new_variant_entries += 1

        conn.commit()
        return new_entries, checked, new_variant_entries, checked_variants
    finally:
        conn.close()


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        logger.error("❌ ANTCHECK_API_KEY ist nicht gesetzt – abbruch.")
        sys.exit(1)

    start = time.monotonic()
    logger.info(f"🚀 Starte AntCheck Grabber – Ziel: {OUTPUT_FILE}")

    try:
        # 1. Shops laden
        logger.info("🏪 Lade Shops...")
        shops_raw = _fetch_json(SHOPS_URL)
        if not isinstance(shops_raw, list):
            shops_raw = shops_raw.get("data", shops_raw.get("shops", []))
        shop_map = build_shop_map(shops_raw)
        logger.info(f"✅ {len(shop_map)} Shops gefunden")

        # 1b. Varianten global laden (nach product_id gruppiert)
        logger.info("🔖 Lade Produkt-Varianten...")
        variants_by_pid = fetch_variants_by_product()
        total_variants  = sum(len(v) for v in variants_by_pid.values())
        logger.info(f"✅ {total_variants} Varianten für {len(variants_by_pid)} Produkte")

        # 2. Produkte pro Shop laden
        total_products = 0
        for i, (shop_id, shop) in enumerate(shop_map.items(), 1):
            try:
                url = PRODUCTS_URL.format(shop_id=shop_id)
                products_raw = _fetch_json(url)
                if not isinstance(products_raw, list):
                    products_raw = products_raw.get("data", products_raw.get("products", []))
                add_products(shop_map, shop_id, products_raw, variants_by_pid)
                count = len(shop_map[shop_id]["products"])
                total_products += count
                logger.info(f"  📦 [{i}/{len(shop_map)}] Shop {shop['name']}: {count} Produkte")
            except Exception as e:
                logger.warning(f"  ⚠️ Shop {shop_id} Produkte fehlgeschlagen: {e}")

        # 3. Ausgabe schreiben
        output = {
            "_meta": {
                "fetched_at":    datetime.now(timezone.utc).isoformat(),
                "shop_count":    len(shop_map),
                "product_count": total_products,
                "variant_count": total_variants,
            },
            **shop_map,
        }
        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUTPUT_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUTPUT_FILE)

        # 4. Preis-History tracken
        try:
            new_entries, checked, v_new, v_checked = _track_prices(shop_map)
            logger.info(
                f"💶 Preis-Tracking: {checked} Produkte ({new_entries} neu), "
                f"{v_checked} Varianten ({v_new} neu) -> {PRICE_HISTORY_DB}"
            )
            # Heartbeat: mtime nach JEDEM erfolgreichen Preis-Lauf aktualisieren.
            # Ohne dies bewegt sich die mtime nur bei tatsächlichen Preisänderungen
            # (SQLite schreibt sonst nichts) – dann wirkt price_history.db fälschlich
            # „veraltet", obwohl der Grabber erfolgreich lief. Mit touch() ist die
            # Datei-Aktualität ein verlässliches „letzter erfolgreicher Lauf"-Signal
            # (und ein echter Ausfall des Preis-Schritts bleibt so erkennbar).
            try:
                PRICE_HISTORY_DB.touch()
            except OSError as e:
                logger.warning(f"⚠️ price_history.db touch fehlgeschlagen: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Preis-Tracking fehlgeschlagen (nicht kritisch): {e}")

        elapsed = time.monotonic() - start
        logger.info(
            f"✅ Fertig: {len(shop_map)} Shops / {total_products} Produkte "
            f"-> {OUTPUT_FILE} ({elapsed:.1f}s)"
        )

    except Exception as e:
        logger.error(f"❌ Grabber fehlgeschlagen: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
