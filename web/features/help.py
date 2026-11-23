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
web/features/help.py – Hilfeseite (/hilfe) und Einführungs-Tour (/intro.json).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import re
from aiohttp import web
from utils import account as ACC, board_swarm as SWB
from datetime import datetime
from utils.timez import BERLIN
from utils.board_i18n import pick_lang, translate
from config import MAP_GUILD_ID

from web.core.render import _render
from web.core.security import _aam_member, _hmac, _ip, _is_admin, _rate
from utils.web_keys import BOT_KEY

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


_HP_EXAMPLES = {
    "help_notification": "/notification species:Messor barbarus regions:de,at",
    "help_delete": "/delete_notifications ids:12,15",
    "help_offer_alert": "/offer_alert add schlagwort:Lasius niger, Formicarium",
    "help_digest": "/digest action:aktivieren",
    "help_sells": "/sells species:Lasius niger country:de",
    "help_offers": "/offers shop:antstore.net",
    "help_art": "/art name:Camponotus nicobarensis",
    "help_shop": "/shop name:antstore.net",
    "help_bewerten": "/bewerten shop:antstore.net",
    "help_track_price": "/track_price species:Lasius niger",
    "help_set_target": "/set_target mode:zusätzlich target_price:24.90",
    "help_codes": "/codes show_expired:True",
    "help_inat": "/inat_verknuepfen login:mein_inat_name",
    "help_rueckblick": "/rueckblick jahr:2026",
    "help_usersetting": "/usersetting language language:de",
}
_HP_BOARD = [("finder", "/art"), ("star", "/art"), ("card", "/art"), ("shops", "/shops"),
             ("konto", "/konto"), ("map", "/map"), ("swarm", "/schwarmflug"), ("yearreview", "/rueckblick"), ("inat", "/inat"),
             ("stats", "/stats"), ("chat", ""), ("intros", ""), ("board", "/")]
_HP_FAQ = ["nodm", "channel", "data", "ampel", "source", "review", "quiet", "intro"]
_MD_B = re.compile(r"\*\*(.+?)\*\*")
_MD_C = re.compile(r"`([^`]+)`")
_MD_I = re.compile(r"(?<![\w*])\*([^*]+)\*(?![\w*])")


def _md_html(text: str):
    """Discord-Markdown der Hilfetexte (fett, kursiv, Code) → sicheres HTML."""
    from markupsafe import Markup, escape
    h = str(escape(text))
    h = _MD_C.sub(r"<code>\1</code>", h)
    h = _MD_B.sub(r"<b>\1</b>", h)
    h = _MD_I.sub(r"<i>\1</i>", h)
    return Markup(h)


def _hp_groups(groups, lang, examples=True):
    from utils.localization import l10n
    out = []
    for head_key, keys in groups:
        head = re.sub(r"\*\*", "", l10n.get(head_key, lang))
        items = []
        for k in keys:
            txt = l10n.get(k, lang)
            if txt == k:
                continue
            items.append({"html": _md_html(txt), "q": re.sub(r"[*`_]", "", txt).lower(),
                          "ex": _HP_EXAMPLES.get(k, "") if examples else ""})
        if items:
            out.append((head, items))
    return out


async def h_hilfe(req):
    lang = pick_lang(req)
    try:
        from cogs.stats import StatsCog
        from config import AI_CHAT_PUBLIC
        groups = _hp_groups(StatsCog._HELP_USER_GROUPS, lang)
        if AI_CHAT_PUBLIC:
            groups += _hp_groups([("help_grp_ai", StatsCog._HELP_AI_KEYS)], lang)
        admin_groups = _hp_groups(StatsCog._HELP_ADMIN_GROUPS, lang, examples=False) if _is_admin(req) else []
    except Exception as e:
        logger.warning("❓ Hilfe: Befehle nicht geladen: %s", e)
        groups, admin_groups = [], []
    resp = _render(req, "hilfe", title=translate(lang, "hp_h"), groups=groups, admin_groups=admin_groups,
                   board=_HP_BOARD, faq=_HP_FAQ)
    if admin_groups:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ══ Anlass-Intros (/intro.json, Darstellung in static/intro.js) ════════════════
_INTRO_COOKIE = "board_intro"
_INTRO_TTL = 365 * 86400


def _member_joined(app, uid):
    """Beitrittsdatum zum AAM-Server (live von Discord, nicht gespeichert)."""
    try:
        g = app[BOT_KEY].get_guild(MAP_GUILD_ID) if MAP_GUILD_ID else None
        m = g.get_member(int(uid)) if g else None
        ja = getattr(m, "joined_at", None)
        return ja.astimezone(BERLIN).date() if ja else None
    except Exception:
        return None


def _intro_payload(lang: str, c: dict) -> dict:
    p = c.get("p") or {}
    sc = c["scene"]
    link = c.get("link") or ""
    if link:
        path, _, frag = link.partition("#")
        link = f"{path}?lang={lang}" + (f"#{frag}" if frag else "")
    tk = c.get("tk") or sc                      # eigener Text bei gleicher Szene (z. B. Saisonstart)
    key_s = f"intro_{tk}_s1" if sc in ("botbday", "aam", "join") and p.get("n") == 1 else f"intro_{tk}_s"
    title_k = f"intro_{tk}_t1" if sc in ("botbday", "aam", "join") and p.get("n") == 1 else f"intro_{tk}_t"
    if sc == "ach" and p.get("n", 1) > 1:
        key_s = "intro_ach_s_more"
    return {"show": True, "scene": sc,
            "title": translate(lang, title_k, **p), "sub": translate(lang, key_s, **p),
            "link": link, "link_label": translate(lang, f"intro_{tk}_link") if link else "",
            "skip": translate(lang, "intro_skip"), "p": p}


async def h_intro(req):
    """Welches Intro (wenn überhaupt) gerade dran ist. Höchstens eins pro Tag, jeder Anlass
    einmal je Jahr (Cookie board_intro). Admins können mit ?intro=<szene> jede Szene testen."""
    from utils import intro as IN
    lang = pick_lang(req)
    hdr = {"Cache-Control": "private, no-store"}
    none = web.json_response({"show": False}, headers=hdr)
    today = datetime.now(BERLIN).date()
    forced = (req.query.get("intro") or "")[:20]
    if forced and _is_admin(req):
        c = IN.demo(forced, today)
        return web.json_response(_intro_payload(lang, c) if c else {"show": False}, headers=hdr)
    if not _rate("intro:" + _hmac("intro", _ip(req)), 60, 60):
        return none
    uid = _aam_member(req)
    prefs = ACC.prefs(uid) if uid else None
    if prefs and not prefs["intros"]:
        return none
    tday = today.strftime("%Y%m%d")
    day, seen = IN.parse_cookie(req.cookies.get(_INTRO_COOKIE, ""))
    if day == tday:
        return none                                  # heute schon ein Intro gesehen
    bot = req.app[BOT_KEY]
    cands: list[dict] = []
    if uid:
        try:
            pers = await IN.personal(bot, uid, today, _member_joined(req.app, uid), prefs, lang)
        except Exception as e:
            logger.debug("intro personal: %s", e)
            pers = []
        for c in pers:
            if c.get("silent"):
                await ACC.set_prefs(bot, uid, **c["commit"])   # erster Besuch: Ist-Stand merken
        cands += [c for c in pers if not c.get("silent")]
    cands += IN.seasonal(today, SWB.season_info(lang))
    pick = next((c for c in cands if c["key"] not in seen), None)
    if not pick:
        return none
    if uid and pick.get("commit"):
        await ACC.set_prefs(bot, uid, **pick["commit"])
    resp = web.json_response(_intro_payload(lang, pick), headers=hdr)
    resp.set_cookie(_INTRO_COOKIE, IN.build_cookie(tday, seen, pick["key"]), max_age=_INTRO_TTL,
                    httponly=True, samesite="Lax", secure=req.secure)
    return resp
