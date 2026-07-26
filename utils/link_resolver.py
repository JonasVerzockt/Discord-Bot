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
utils/link_resolver.py – Kurzlinks/Weiterleitungs-Links zur echten Zieladresse
auflösen.

Motivation: In Rabattcode-Posts stehen oft geteilte Kurzlinks (z.B.
https://share.google/… oder bit.ly/…) statt der echten Shop-Adresse. Diese
werden hier per Redirect zur finalen URL aufgelöst und um Tracking-Parameter
(srsltid, utm_*, gclid, …) bereinigt, sodass z.B.
    https://share.google/CtvpgPmdpWYi4DnQP
zu  https://www.estheticants.com/  wird.

Design:
- Nur bekannte Kurzlink-Dienste lösen einen Netzwerk-Aufruf aus (kein Traffic
  für normale Links).
- Google-„/url?q="-Wrapper werden ohne Netzwerk direkt entpackt.
- Alle Ergebnisse werden von Tracking-Parametern befreit.
- Ergebnisse werden prozessweit gecacht (ein Kurzlink → ein Netzwerk-Aufruf).
- Robuste Fehlerbehandlung: schlägt die Auflösung fehl, wird die (nur um
  Tracking bereinigte) Ausgangs-URL unverändert zurückgegeben.
"""
import re
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, parse_qsl, urlencode

import requests

logger = logging.getLogger(__name__)

# Bekannte URL-Verkürzer / Weiterleitungsdienste (Host ohne führendes "www.").
_SHORTENERS = {
    "share.google", "g.co", "goo.gl", "bit.ly", "t.co", "tinyurl.com",
    "ow.ly", "buff.ly", "cutt.ly", "rebrand.ly", "is.gd", "s.id", "tiny.cc",
    "shorturl.at", "rb.gy", "t.ly", "lnkd.in", "fb.me", "amzn.to",
}

# Reine Tracking-Parameter, die aus der finalen URL entfernt werden. Bewusst
# konservativ – nur eindeutig funktionslose Marketing-/Tracking-Parameter.
_TRACKING = {
    "srsltid", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "gclid", "gbraid", "wbraid", "fbclid",
    "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "vero_id", "yclid",
}

_UA = "Mozilla/5.0 (compatible; AntCheckBot/1.0; +https://antcheck.info)"

# Prozessweiter Cache: Ausgangs-URL → aufgelöste URL.
_cache: dict[str, str] = {}


def _host(url: str) -> str:
    """Host in Kleinbuchstaben ohne führendes 'www.'."""
    return urlparse(url).netloc.lower().removeprefix("www.")


def _strip_tracking(url: str) -> str:
    """Entfernt bekannte Tracking-Query-Parameter; behält funktionale Parameter."""
    p = urlparse(url)
    if not p.query:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING
    ]
    return urlunparse(p._replace(query=urlencode(kept)))


def _unwrap_google(url: str) -> str:
    """Google-Weiterleitung '…/url?q=<ziel>' → <ziel> (ohne Netzwerk)."""
    p = urlparse(url)
    if "google." in p.netloc.lower() and p.path.rstrip("/").endswith("/url"):
        q = parse_qs(p.query)
        for k in ("q", "url"):
            if q.get(k):
                return q[k][0]
    return url


def is_shortlink(url: str) -> bool:
    """True, wenn der Host ein bekannter Kurzlink-/Weiterleitungsdienst ist."""
    return _host(url or "") in _SHORTENERS


def resolve_shop_url(url: str | None, timeout: float = 6.0) -> str | None:
    """
    Löst einen (evtl. gekürzten) Link zur echten Zieladresse auf und entfernt
    Tracking-Parameter. Nicht-Kurzlinks werden nur bereinigt (kein Netzwerk).
    Bei Fehlern wird die bereinigte Ausgangs-URL zurückgegeben (nie None außer
    bei leerer Eingabe).
    """
    if not url or not str(url).strip():
        return url
    u = str(url).strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u

    # 1) Google-„/url?q="-Wrapper direkt entpacken (kein Netzwerk nötig).
    unwrapped = _unwrap_google(u)
    if unwrapped != u:
        return _strip_tracking(unwrapped)

    # 2) Normale Links: nur Tracking entfernen, kein Netzwerk.
    if _host(u) not in _SHORTENERS:
        return _strip_tracking(u)

    # 3) Bekannter Kurzlink → Redirects folgen (mit Cache).
    if u in _cache:
        return _cache[u]
    final = u
    try:
        resp = requests.get(
            u, allow_redirects=True, timeout=timeout,
            headers={"User-Agent": _UA}, stream=True,
        )
        final = resp.url or u
        resp.close()
    except Exception as e:
        logger.info("🔗 Kurzlink nicht auflösbar (%s): %s", u, e)
        _cache[u] = u
        return u
    final = _strip_tracking(final)
    _cache[u] = final
    logger.info("🔗 Kurzlink aufgelöst: %s → %s", u, final)
    return final
