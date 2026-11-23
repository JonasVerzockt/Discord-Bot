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
utils/board_i18n.py – Sprachkatalog & Helfer für die Web-Oberfläche (web/).

Die Texte selbst liegen seit 5.0.0 in locales/board/{de,en,eo}.json.

Drei Sprachen: Deutsch (de, Standard), Englisch (en), Esperanto (eo).
Umschaltung ausschließlich über den URL-Parameter ``?lang=xx`` (KEIN Cookie);
auf jeder Seite steht ein Flaggen-Umschalter im Header. Fällt der Parameter weg,
wird die Browser-Sprache (Accept-Language) herangezogen, sonst Deutsch.

Bewusst NICHT lokalisiert: die einzelnen Health-Kacheln des Status-Panels
(Name + Detailtext). Deren Name dient zugleich als stabiler Schlüssel für die
Vorfall-Historie (board_incidents.check_key) und die Kachel-Links
(/status/check/{key}); eine Übersetzung würde diese Verknüpfung brechen.
Lokalisiert werden hier nur die Gesamt-Ampel, die Sektions-Titel/-Notizen und
alle leserseitigen Texte der Seiten.

Nutzung (in board.py):
    from utils.board_i18n import LANGS, pick_lang, translate, type_label, flash_text
    lang = pick_lang(req)
    t = lambda key, **kw: translate(lang, key, **kw)
"""
from __future__ import annotations

import json
from pathlib import Path

# Reihenfolge = Anzeigereihenfolge der Flaggen im Header.
LANGS = ("de", "en", "eo")
DEFAULT_LANG = "de"

# Flaggen für den Umschalter als Inline-SVG (KEINE Emoji-Flaggen: die rendern u.a.
# unter Windows nur als Buchstaben "DE"/"GB"). Jeweils (SVG, Kürzel). Das SVG wird
# im Template mit |safe ausgegeben. de = Bundesflagge, en = Union Jack (vereinfacht),
# eo = Esperanto-Flagge (grün mit weißem Kanton + grünem Stern „verda stelo").
_SVG_DE = ('<svg class=fl viewBox="0 0 5 3" preserveAspectRatio="none">'
           '<rect width="5" height="3" fill="#000"/>'
           '<rect y="1" width="5" height="1" fill="#D00"/>'
           '<rect y="2" width="5" height="1" fill="#FFCE00"/></svg>')
_SVG_EN = ('<svg class=fl viewBox="0 0 60 30" preserveAspectRatio="none">'
           '<rect width="60" height="30" fill="#012169"/>'
           '<path d="M0,0 60,30 M60,0 0,30" stroke="#fff" stroke-width="6"/>'
           '<path d="M0,0 60,30 M60,0 0,30" stroke="#C8102E" stroke-width="2.5"/>'
           '<path d="M30,0 V30 M0,15 H60" stroke="#fff" stroke-width="10"/>'
           '<path d="M30,0 V30 M0,15 H60" stroke="#C8102E" stroke-width="6"/></svg>')
_SVG_EO = ('<svg class=fl viewBox="0 0 60 30" preserveAspectRatio="none">'
           '<rect width="60" height="30" fill="#009900"/>'
           '<rect width="15" height="15" fill="#fff"/>'
           '<polygon fill="#009900" points="7.5,1.3 8.97,5.48 13.4,5.58 9.88,8.27 '
           '11.14,12.52 7.5,10 3.86,12.52 5.12,8.27 1.6,5.58 6.03,5.48"/></svg>')
FLAGS = {
    "de": (_SVG_DE, "DE"),
    "en": (_SVG_EN, "EN"),
    "eo": (_SVG_EO, "EO"),
}
FLAG_TITLE = {
    "de": "Deutsch",
    "en": "English",
    "eo": "Esperanto",
}


def pick_lang(req) -> str:
    """Ermittelt die Sprache: ?lang= (falls gültig) > Accept-Language > Standard (de).

    Gibt bewusst die WHITELIST-Konstante aus LANGS zurück (nicht den rohen User-Wert):
    So ist der Rückgabewert nachweislich untainted – er fließt in Redirect-Locations
    (`?lang=…`) ein, und diese Konstruktion verhindert Open-Redirect/Header-Injection
    (CodeQL py/url-redirection) unabhängig von der Eingabe."""
    q = (req.query.get("lang") or "").lower().strip()
    for code in LANGS:
        if q == code:
            return code                      # Literal aus LANGS -> untainted
    accept = (req.headers.get("Accept-Language") or "").lower()
    # Grobe, robuste Auswertung: erste passende Sprache im Header gewinnt.
    for part in accept.replace(" ", "").split(","):
        want = part.split(";")[0].split("-")[0]
        for code in LANGS:
            if want == code:
                return code                  # ebenfalls Literal aus LANGS
    return DEFAULT_LANG


# Sprachkatalog: locales/board/<sprache>.json (Schlüssel → Text), je Sprache eine Datei.
# Hier zusammengeführt zu {key: {"de": …, "en": …, "eo": …}} – dieselbe Struktur wie
# früher als Python-Literal, damit translate()/flash_text() und alle Aufrufer gleich bleiben.
# Neue Texte: in ALLEN drei Dateien ergänzen (fehlt eine Sprache, greift Deutsch).
_CATALOG_DIR = Path(__file__).resolve().parent.parent / "locales" / "board"


def _load_catalog() -> dict[str, dict[str, str]]:
    per_lang: dict[str, dict[str, str]] = {}
    for lang in LANGS:
        with open(_CATALOG_DIR / f"{lang}.json", encoding="utf-8") as f:
            per_lang[lang] = json.load(f)
    keys = list(per_lang[DEFAULT_LANG])
    for lang in LANGS:
        keys += [k for k in per_lang[lang] if k not in per_lang[DEFAULT_LANG] and k not in keys]
    return {k: {lang: per_lang[lang][k] for lang in LANGS if k in per_lang[lang]} for k in keys}


T: dict[str, dict[str, str]] = _load_catalog()


def translate(lang: str, key: str, **kw) -> str:
    """Übersetzt *key* in *lang* (Fallback: Deutsch, dann der Key selbst).
    Platzhalter werden via str.format eingesetzt."""
    entry = T.get(key)
    if not entry:
        return key
    text = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


# Anzeige-Label für die Einreichungs-Typen (bug/feature/idea) – lokalisiert,
# der gespeicherte Wert bleibt der englische Schlüssel.
_TYPE_KEYS = {"bug": "type_bug", "feature": "type_feature", "idea": "type_idea"}


def type_label(lang: str, typ: str) -> str:
    return translate(lang, _TYPE_KEYS.get((typ or "").lower(), "type_idea"))


# Lokalisierte Ländernamen (Babel/CLDR) – wie bei /shop_list. Cache pro Sprache.
_LOCALE_CACHE: dict = {}


def _locale(lang: str):
    if lang not in _LOCALE_CACHE:
        try:
            from babel import Locale
            _LOCALE_CACHE[lang] = Locale.parse(lang)
        except Exception:
            try:
                from babel import Locale
                _LOCALE_CACHE[lang] = Locale.parse("en")
            except Exception:
                _LOCALE_CACHE[lang] = None
    return _LOCALE_CACHE[lang]


def country_name(lang: str, iso: str) -> str:
    """ISO-Ländercode -> lokalisierter Name (Fallback: Großbuchstaben-Code)."""
    code = (iso or "").upper()
    if not code or code == "??":
        return "?"
    loc = _locale(lang)
    if loc is None:
        return code
    try:
        return loc.territories.get(code, code)
    except Exception:
        return code


def flash_text(lang: str, code: str, n: str = "", s: str = "") -> str:
    """Übersetzt einen Flash-Code (?m=code). Unbekannte Codes werden unverändert
    zurückgegeben (Abwärtskompatibilität: alte Volltext-Meldungen bleiben lesbar)."""
    if not code:
        return ""
    if code == "imported":
        skipped = translate(lang, "flash_skipped", s=s) if s and s != "0" else ""
        return translate(lang, "flash_imported", n=n or "0", skipped=skipped)
    key = "flash_" + code
    if key in T:
        return translate(lang, key)
    return code   # unbekannt -> unverändert anzeigen (z.B. alter Volltext)
