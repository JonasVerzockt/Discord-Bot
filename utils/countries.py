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
utils/countries.py – Ländercode-Helfer (Flaggen-Emoji + lokalisierter Name).

Ländernamen werden über Babel (CLDR-Daten) in der jeweiligen Nutzersprache
(de/en/eo) ausgegeben. Ist Babel nicht installiert oder fehlt ein Name, greift
ein englischer Fallback-Dict. Ländercodes sind ISO-3166-1-alpha-2 (klein), wie in
shops_data.json (Feld "country"). Unbekannte Codes fallen auf den
Großbuchstaben-Code zurück.
"""

try:
    from babel import Locale
    _BABEL = True
except Exception:  # Babel (noch) nicht installiert → Englisch-Fallback
    _BABEL = False

# Englischer Fallback (nur genutzt, wenn Babel fehlt oder keinen Namen liefert).
_COUNTRY_NAMES = {
    "at": "Austria", "au": "Australia", "be": "Belgium", "bg": "Bulgaria",
    "br": "Brazil", "ca": "Canada", "ch": "Switzerland", "cn": "China",
    "cy": "Cyprus", "cz": "Czechia", "de": "Germany", "dk": "Denmark",
    "ee": "Estonia", "es": "Spain", "fi": "Finland", "fr": "France",
    "gb": "United Kingdom", "uk": "United Kingdom", "gr": "Greece",
    "hk": "Hong Kong", "hr": "Croatia", "hu": "Hungary", "ie": "Ireland",
    "il": "Israel", "in": "India", "it": "Italy", "jp": "Japan",
    "lt": "Lithuania", "lu": "Luxembourg", "lv": "Latvia", "mt": "Malta",
    "mx": "Mexico", "my": "Malaysia", "nl": "Netherlands", "no": "Norway",
    "nz": "New Zealand", "pl": "Poland", "pt": "Portugal", "ro": "Romania",
    "rs": "Serbia", "ru": "Russia", "se": "Sweden", "sg": "Singapore",
    "si": "Slovenia", "sk": "Slovakia", "th": "Thailand", "tr": "Türkiye",
    "tw": "Taiwan", "ua": "Ukraine", "us": "United States", "vn": "Vietnam",
    "za": "South Africa",
}

_locale_cache: dict[str, object] = {}


def _locale(lang: str):
    """Gecachtes Babel-Locale für eine Sprache (oder None, wenn unbekannt)."""
    key = (lang or "en").split("-")[0].split("_")[0].lower()
    if key not in _locale_cache:
        try:
            _locale_cache[key] = Locale.parse(key)
        except Exception:
            _locale_cache[key] = None
    return _locale_cache[key]


def flag_emoji(code: str) -> str:
    """ISO-2-Code → Flaggen-Emoji (Regional-Indicator). Ungültig → weiße Flagge."""
    c = (code or "").strip().lower()
    if len(c) != 2 or not c.isalpha():
        return "🏳️"
    return chr(0x1F1E6 + ord(c[0]) - 97) + chr(0x1F1E6 + ord(c[1]) - 97)


def country_name(code: str, lang: str = "en") -> str:
    """ISO-2-Code → Ländername in der gewünschten Sprache (Babel/CLDR).
    Reihenfolge: Babel(lang) → Babel(en) → Englisch-Fallback → Code (Großbuchst.)."""
    c = (code or "").strip().lower()
    if not c:
        return "?"
    cc = c.upper()
    if _BABEL:
        loc = _locale(lang)
        if loc is not None:
            name = loc.territories.get(cc)
            if name:
                return name
        en = _locale("en")
        if en is not None:
            name = en.territories.get(cc)
            if name:
                return name
    return _COUNTRY_NAMES.get(c, cc)


def country_label(code: str, lang: str = "en") -> str:
    """Überschrift wie '🇦🇹 Österreich (AT)'. Leerer Code → '🏳️ Unknown'."""
    c = (code or "").strip().lower()
    if not c:
        return "🏳️ Unknown"
    return f"{flag_emoji(c)} {country_name(c, lang)} ({c.upper()})"
