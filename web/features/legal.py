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
web/features/legal.py – Rechtsseiten: Impressum, Datenschutz, Partner, Gewinnspiel, Kontaktformular, Partner-Logos.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import discord
from aiohttp import web
from utils.board_i18n import flash_text, pick_lang, translate
from config import BOARD_OWNER_ID

from web.core.partners import _load_partners, _PARTNER_CT, LEGAL_DIR, PARTNER_DIR
from web.core.render import _render
from web.core.security import _captcha_ok, _ip, _make_captcha, _rate
from utils.web_keys import BOT_KEY

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


_LEGAL_PAGES = {"impressum", "datenschutz", "partner", "gewinnspiel"}


async def _send_contact_dm(app, message: str, name: str, email: str, tel: str) -> bool:
    """Kontaktformular-Nachricht als Discord-DM an den Owner (BOARD_OWNER_ID)."""
    bot = app.get(BOT_KEY)
    if not BOARD_OWNER_ID or bot is None:
        logger.warning("✉️ Kontaktformular: BOARD_OWNER_ID/Bot fehlt – Nachricht verworfen.")
        return False
    try:
        user = await bot.fetch_user(BOARD_OWNER_ID)
        e = discord.Embed(title="✉️ Board-Kontaktformular", description=message[:4000], color=0x1F6FEB)
        e.add_field(name="Name", value=name or "—")
        e.add_field(name="E-Mail", value=email or "—")
        if tel:
            e.add_field(name="Telefon", value=tel, inline=False)
        await user.send(embed=e)
        return True
    except Exception as ex:  # noqa: BLE001
        logger.error("✉️ Kontaktformular-DM fehlgeschlagen: %s", ex)
        return False


async def h_partner_logo(req):
    """Liefert NUR Logos, die in partners.json eingetragen sind (Allowlist)."""
    want = req.match_info.get("name", "")
    for p in _load_partners():
        if p["logo"] and p["logo"] == want:
            path = PARTNER_DIR / p["logo"]          # Name stammt aus der geprüften Liste
            ext = p["logo"].rsplit(".", 1)[-1].lower()
            return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400",
                                                   "Content-Type": _PARTNER_CT[ext],
                                                   "X-Content-Type-Options": "nosniff"})
    raise web.HTTPNotFound()


def _legal_content(name: str) -> tuple[str, bool]:
    """Lädt den HTML-Body einer Rechtsseite. Rückgabe: (html, is_example).
    Reihenfolge: legal/<name>.html (echt) > legal/<name>.example.html (Vorlage)."""
    if name not in _LEGAL_PAGES:
        return ("", True)
    for path, is_example in ((LEGAL_DIR / f"{name}.html", False),
                             (LEGAL_DIR / f"{name}.example.html", True)):
        try:
            return (path.read_text(encoding="utf-8"), is_example)
        except OSError:
            continue
    return ("<p>[[Seite noch nicht konfiguriert]]</p>", True)


async def h_impressum(req):
    """Impressum (§ 5 DDG) – Inhalt aus legal/impressum(.example).html + Kontaktformular."""
    lang = pick_lang(req)
    body, is_example = _legal_content("impressum")
    q, sig = _make_captcha()
    flash = flash_text(lang, req.query.get("m", ""))
    return _render(req, "legal", title=translate(lang, "nav_impressum"),
                   heading=translate(lang, "nav_impressum"), body=body, is_example=is_example,
                   show_contact=bool(BOARD_OWNER_ID), captcha_q=q, captcha_sig=sig, flash=flash)


async def h_datenschutz(req):
    """Datenschutzerklärung (Art. 13 DSGVO) – Inhalt aus legal/datenschutz(.example).html."""
    lang = pick_lang(req)
    body, is_example = _legal_content("datenschutz")
    return _render(req, "legal", title=translate(lang, "nav_privacy"),
                   heading=translate(lang, "nav_privacy"), body=body, is_example=is_example,
                   show_contact=False)


async def h_partner_page(req):
    """„Partner werden“ – Inhalt aus legal/partner(.example).html."""
    lang = pick_lang(req)
    body, is_example = _legal_content("partner")
    return _render(req, "legal", title=translate(lang, "nav_partner"),
                   heading=translate(lang, "nav_partner"), body=body, is_example=is_example,
                   show_contact=False)


async def h_gewinnspiel(req):
    """Teilnahmebedingungen für Gewinnspiele – Inhalt aus legal/gewinnspiel(.example).html."""
    lang = pick_lang(req)
    body, is_example = _legal_content("gewinnspiel")
    return _render(req, "legal", title=translate(lang, "nav_giveaway"),
                   heading=translate(lang, "nav_giveaway"), body=body, is_example=is_example,
                   show_contact=False)


async def h_impressum_contact(req):
    """Kontaktformular der Impressum-Seite -> Discord-DM an den Owner.
    Schutz: Honeypot, Rate-Limit, Rechenaufgabe (Captcha). Pflicht: Name, E-Mail, Nachricht."""
    lang = pick_lang(req)
    d = await req.post()
    if (d.get("website") or "").strip():                          # Honeypot -> still „ok"
        raise web.HTTPFound(f"/impressum?m=contact_sent&lang={lang}")
    if not _rate("contact:" + _ip(req), 3, 3600):
        raise web.HTTPFound(f"/impressum?m=contact_toomany&lang={lang}")
    if not _captcha_ok(d):
        raise web.HTTPFound(f"/impressum?m=contact_captcha&lang={lang}")
    name = (d.get("name") or "").strip()[:80]
    email = (d.get("email") or "").strip()[:120]
    message = (d.get("message") or "").strip()[:2000]
    if not name or not message or "@" not in email:
        raise web.HTTPFound(f"/impressum?m=contact_empty&lang={lang}")
    ok = await _send_contact_dm(req.app, message, name, email, (d.get("tel") or "").strip()[:40])
    raise web.HTTPFound(f"/impressum?m={'contact_sent' if ok else 'contact_fail'}&lang={lang}")
