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
web/features/shops.py – Händlerübersicht und Händlerseiten.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import asyncio
from aiohttp import web
from utils.board_i18n import country_name, pick_lang, translate
from utils.currency import ensure_rates

from web.core.partners import _load_partners
from web.core.render import _FILTER_JS, _render
from web.core.security import _aam_member, _login_url


async def h_shops(req):
    from utils import directory as dirx
    lang = pick_lang(req)
    await ensure_rates()
    reviews = await asyncio.to_thread(dirx.loaded_reviews)
    shops = await asyncio.to_thread(dirx.all_shops, reviews)
    partners = {dirx.domain_of(p["url"]) for p in _load_partners()}
    items = []
    for dom, sh in shops.items():
        rv = reviews.get(dom) or {}
        if not sh["offers_count"] and not rv:
            continue
        items.append({"name": sh["name"], "domain": dom, "avg": rv.get("avg"), "n": rv.get("count", 0),
                      "species": sh["species_count"], "instock": sh["instock_count"],
                      "partner": dom in partners})
    items.sort(key=lambda x: x["name"].lower())
    return _render(req, "shops", title=translate(lang, "dir_shops_h"), items=items, filter_js=_FILTER_JS)


async def h_shop(req):
    from utils import directory as dirx
    from utils.sheet import warn_emoji
    lang = pick_lang(req)
    raw = req.match_info.get("domain", "")[:120]
    await ensure_rates()
    reviews = await asyncio.to_thread(dirx.loaded_reviews)
    sh = await asyncio.to_thread(dirx.find_shop, raw, reviews)
    if not sh:
        raise web.HTTPNotFound(text=translate(lang, "dir_shop_404"))
    dom = sh["domain"]
    if raw != dom:
        raise web.HTTPFound(f"/shop/{dom}?lang={lang}")
    rv = reviews.get(dom)
    member = _aam_member(req)
    ctx = dict(sh=sh, rv=rv, member=bool(member), partner=dirx.partner_of(dom),
               delivery=await dirx.delivery_of(req.app["bot"], dom),
               codes=await dirx.active_codes(req.app["bot"], dom),
               country=country_name(lang, sh["country"]) if sh.get("country") else "",
               login_url="" if member else _login_url(req), filter_js=_FILTER_JS,
               warns=[], years_labels=[], years_avg=[], years_n=[], dist_labels=[], dist_n=[])
    if member:
        ctx["warns"] = [dict(w, emoji=warn_emoji(w.get("level"))) for w in dirx.warnings_of(dom)]
        if rv:
            ctx["years_labels"] = [str(y) for y in rv["years"]]
            ctx["years_avg"] = [a for _, a in rv["years"].values()]
            ctx["years_n"] = [n for n, _ in rv["years"].values()]
            ctx["dist_labels"] = [str(k) for k in range(1, 11)]
            ctx["dist_n"] = [rv["dist"].get(k, 0) for k in range(1, 11)]
    instock_sp = {o["species"].lower() for o in sh["offers"] if o["in_stock"]}
    # Preis-Ampel je Art: günstigstes lagerndes Angebot dieses Shops gegen seine eigene Historie
    from utils import price_signal as PS
    from utils.localization import l10n
    sig = await asyncio.to_thread(PS.signals, [o["pid"] for o in sh["offers"] if o["in_stock"]])
    best: dict = {}
    for o in sh["offers"]:
        if o["in_stock"] and o["price"] is not None:
            k = o["species"].lower()
            if k not in best or o["price"] < best[k]["price"]:
                best[k] = o
    gmap: dict = {}
    any_sig = False
    for n in sh["species"]:
        st = n.lower() in instock_sp
        mark = PS.EMOJI.get(sig.get(best[n.lower()]["pid"]), "") if n.lower() in best else ""
        any_sig = any_sig or bool(mark)
        gmap.setdefault(n.split()[0], []).append(
            {"name": n, "instock": st,
             "note": (translate(lang, "dir_instock") + (" " + mark if mark else "")) if st else ""})
    ctx["sig_legend"] = l10n.get("price_sig_legend", lang) if any_sig else ""
    ctx["groups"] = sorted(gmap.items(), key=lambda g: g[0].lower())
    resp = _render(req, "shop", title=sh["name"], **ctx)
    if member:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _all_shops_safe() -> dict:
    try:
        from utils import directory as dirx
        return dirx.all_shops()
    except Exception:
        return {}
