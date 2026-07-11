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
utils/countries.py – Ländercode-Helfer (Flaggen-Emoji + englischer Name).

Genutzt von /sells und der gruppierten /usersetting shop_list-Ausgabe.
Ländercodes sind ISO-3166-1-alpha-2 (klein), wie in shops_data.json (Feld
"country") gespeichert. Namen sind bewusst Englisch (wie beim Vorbild-Bot);
unbekannte Codes fallen auf den Großbuchstaben-Code zurück.
"""

# ISO-2 (klein) → englischer Ländername. Fokus: EU + gängige Shop-Länder.
_COUNTRY_NAMES = {
    "at": "Austria", "au": "Australia", "be": "Belgium", "bg": "Bulgaria",
    "br": "Brazil", "ca": "Canada", "ch": "Switzerland", "cn": "China",
    "cy": "Cyprus", "cz": "Czechia", "de": "Germany", "dk": "Denmark",
    "ee": "Estonia", "es": "Spain", "fi": "Finland", "fr": "France",
    "gb": "United Kingdom", "uk": "United Kingdom", "gr": "Greece",
    "hr": "Croatia", "hu": "Hungary", "ie": "Ireland", "il": "Israel",
    "in": "India", "it": "Italy", "jp": "Japan", "lt": "Lithuania",
    "lu": "Luxembourg", "lv": "Latvia", "mt": "Malta", "mx": "Mexico",
    "nl": "Netherlands", "no": "Norway", "nz": "New Zealand", "pl": "Poland",
    "pt": "Portugal", "ro": "Romania", "rs": "Serbia", "ru": "Russia",
    "se": "Sweden", "si": "Slovenia", "sk": "Slovakia", "th": "Thailand",
    "tr": "Türkiye", "ua": "Ukraine", "us": "United States", "za": "South Africa",
}


def flag_emoji(code: str) -> str:
    """ISO-2-Code → Flaggen-Emoji (Regional-Indicator). Ungültig → weiße Flagge."""
    c = (code or "").strip().lower()
    if len(c) != 2 or not c.isalpha():
        return "🏳️"
    return chr(0x1F1E6 + ord(c[0]) - 97) + chr(0x1F1E6 + ord(c[1]) - 97)


def country_name(code: str) -> str:
    """ISO-2-Code → englischer Ländername; unbekannt → Code in Großbuchstaben."""
    c = (code or "").strip().lower()
    return _COUNTRY_NAMES.get(c, c.upper() if c else "?")


def country_label(code: str) -> str:
    """Überschrift wie '🇦🇹 Austria (AT)'. Leerer Code → '🏳️ Unknown'."""
    c = (code or "").strip().lower()
    if not c:
        return "🏳️ Unknown"
    return f"{flag_emoji(c)} {country_name(c)} ({c.upper()})"
