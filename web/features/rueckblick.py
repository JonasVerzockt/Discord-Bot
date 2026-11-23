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
web/features/rueckblick.py – Jahresrückblick (/rueckblick).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import asyncio
import time
from aiohttp import web
from datetime import datetime
from utils.board_i18n import pick_lang, translate

from web.core.render import _render
from web.core.security import _aam_member, _login_url, _member_name
from utils.web_keys import BOT_KEY


_YR_CACHE: dict = {}


async def h_rueckblick(req):
    """Öffentlicher Jahresrückblick (nur Summen). 30-min-Cache je Jahr."""
    from utils import year_review as YR
    from utils import directory as dirx
    from utils.localization import l10n
    lang = pick_lang(req)
    ry = YR.review_year()
    fy = await YR.first_year(req.app[BOT_KEY])
    years = list(range(ry, fy - 1, -1))
    raw_year = req.match_info.get("year")
    if not YR.is_open() and not raw_year:
        # Außerhalb 1. Nov bis 31. Jan: Hinweisseite mit Ameisen-Fakten (Quellen verlinkt).
        # Abgeschlossene Jahre bleiben über die Kacheln abrufbar.
        from utils import ant_facts
        facts = ant_facts.all_for(lang)
        start = ant_facts.pick(lang)["index"]
        resp = _render(req, "rueckblick_wait", title=translate(lang, "yr_wait_h"),
                       facts=facts, start=start, opens=YR.next_open().strftime("%d.%m.%Y"),
                       ry=YR.next_open().year, years=years)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    year = int(raw_year or ry)
    if year < fy or year > ry:
        raise web.HTTPFound(f"/rueckblick?lang={lang}" if not YR.is_open()
                            else f"/rueckblick/{ry}?lang={lang}")
    hit = _YR_CACHE.get(year)
    if hit and time.time() - hit[0] < 1800:
        d = hit[1]
    else:
        d = await YR.community(req.app[BOT_KEY], year)
        _YR_CACHE[year] = (time.time(), d)
    names = {dom: sh["name"] for dom, sh in (await asyncio.to_thread(dirx.all_shops)).items()}
    # Persönlicher Teil: nur für eingeloggte AAM-Mitglieder, nie gecacht, nur eigene Daten.
    me = None
    uid = _aam_member(req)
    if uid:
        me = await YR.personal(req.app[BOT_KEY], uid, year)
        me["name"] = _member_name(req.app, uid) or ""
        me["has"] = bool(me["commands"] or me["achievements"] or me["reviews"]
                         or me["notifications"] or me["watches"] or me["tracked"]
                         or me.get("inat", {}).get("obs") or me.get("swarm"))
        try:
            from utils.achievements import ACHIEVEMENTS
            emo = {a.id: a.emoji for a in ACHIEVEMENTS}
        except Exception:
            emo = {}
        me["ach"] = [(emo.get(a, "🏅"), l10n.get(f"ach_{a}_t", lang)) for a in me["achievements"]]
        for k in ("first", "reviews_first"):
            try:
                me[k] = datetime.fromisoformat(me[k]).strftime("%d.%m.%Y") if me[k] else None
            except ValueError:
                me[k] = None
    tp = lambda key, **kw: l10n.get(key, lang, **kw)
    resp = _render(req, "rueckblick", title=translate(lang, "yr_page_h", y=year), d=d, names=names,
                   me=me, tp=tp, login_url="" if uid else _login_url(req),
                   years=years)
    if uid:
        resp.headers["Cache-Control"] = "private, no-store"   # persönliche Daten nie zwischenspeichern
    return resp
