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
web/core/partners.py – Partner/Sponsoren-Daten (legal/partners.json + Logos) und Pfad der Rechtsseiten.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import json
import re
from config import BASE_DIR


# Rechtsseiten-Inhalte unter <repo>/legal/. Echte Datei (z.B. impressum.html) liegt
# NICHT im Git (gitignored) und wird bevorzugt geladen; sonst die .example-Vorlage.
LEGAL_DIR = BASE_DIR / "legal"


# legal/partners.json: [{"name": "...", "url": "https://...", "text": "...", "logo": "datei.png",
#                        "art": "verein" | "shop", "code": "AAM10"}]
#   art "verein": Vereine/Projekte, gegenseitige Verlinkung -> ohne „Anzeige“.
#   art "shop" (Standard): Shops/Hersteller gegen Community-Vorteil -> als „Anzeige“ gekennzeichnet.
#   code: optionaler Community-Rabattcode (nur bei Shops angezeigt).
# Logos liegen SELBST GEHOSTET in legal/partners/ (keine fremden Server -> keine IP-Weitergabe).
# Nur PNG/JPG/WebP (kein SVG: könnte Skripte enthalten). Fehlt die Datei -> Platzhalter.
PARTNER_DIR = LEGAL_DIR / "partners"
_PARTNER_LOGO_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}\.(png|jpe?g|webp)$")
_PARTNER_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


_partner_cache: dict = {"key": None, "data": []}


def _load_partners() -> list[dict]:
    """Geprüfte Partnerliste (max. 6). Ungültige Einträge werden still übersprungen.
    Wird auf jeder Seite gebraucht -> nach Änderungszeit der Datei/des Logo-Ordners gecacht."""
    f = LEGAL_DIR / "partners.json"
    try:
        key = (f.stat().st_mtime_ns, PARTNER_DIR.stat().st_mtime_ns if PARTNER_DIR.is_dir() else 0)
    except OSError:
        return []
    if _partner_cache["key"] == key:
        return _partner_cache["data"]
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()[:60]
        url = str(e.get("url") or "").strip()
        if not name or not url.startswith("https://"):
            continue
        logo = str(e.get("logo") or "").strip()
        if not (_PARTNER_LOGO_RE.match(logo) and (PARTNER_DIR / logo).is_file()):
            logo = ""
        art = "verein" if str(e.get("art") or "").strip().lower() == "verein" else "shop"
        code = str(e.get("code") or "").strip()[:30]
        if art != "shop" or not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", code or ""):
            code = ""
        out.append({"name": name, "url": url[:300],
                    "text": str(e.get("text") or "").strip()[:140], "logo": logo,
                    "ad": art == "shop", "code": code})
    _partner_cache.update(key=key, data=out[:6])
    return out[:6]
