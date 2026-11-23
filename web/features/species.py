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
web/features/species.py – Artensteckbriefe, Arten-Finder, iNat-Liste und Artenkarte (PNG).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import asyncio
import re
from aiohttp import web
from utils import board_swarm as SWB, shop_stats
from utils.db import execute_db
from urllib.parse import urlencode
from utils.board_i18n import country_name, pick_lang, translate
from utils.currency import ensure_rates
from config import BOARD_PUBLIC_URL

from web.core.render import _eur_fmt, _konto_flash, _render
from web.core.security import _aam_member, _hmac, _ip, _login_url, _member_csrf, _rate
from web.features.swarm import _SW_MONTHS

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


async def h_art_index(req):
    from utils import directory as dirx
    lang = pick_lang(req)
    q = (req.query.get("q") or "").strip()[:80]
    await ensure_rates()
    try:
        idx = await asyncio.to_thread(dirx.index)
    except Exception as e:
        logger.warning("🐜 Arten-Index fehlgeschlagen: %s", e)
        idx = {"species": {}}
    if q:
        hit = await asyncio.to_thread(dirx.find_species, q)
        if hit:
            raise web.HTTPFound(f"/art/{dirx.slug(hit['name'])}?lang={lang}")
    ql = q.lower()
    items = sorted(((sp["name"], len(sp["shops"]), sum(1 for o in sp["offers"] if o["in_stock"]))
                    for sp in idx["species"].values() if not ql or ql in sp["key"]),
                   key=lambda x: x[0].lower())
    groups: dict = {}
    for name, n, inst in items:                            # nach Gattung gruppiert (eingeklappt)
        note = translate(lang, "dir_shops_n", n=n) + (
            " · " + translate(lang, "dir_instock_n", n=inst) if inst else "")
        groups.setdefault(name.split()[0], []).append({"name": name, "note": note, "instock": inst > 0})
    fnd = await _finder(req, idx, lang)
    resp = _render(req, "art_index", title=translate(lang, "dir_art_index_h"), items=items, q=q,
                   groups=sorted(groups.items(), key=lambda g: g[0].lower()),
                   total=len(idx["species"]), fnd=fnd, flash=_konto_flash(req))
    if fnd["uid"]:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


_BACK_RE = re.compile(r"^/art(?:/[A-Za-z0-9_.%\-]{1,120})?(?:\?[A-Za-z0-9_.%&=+\-]{0,300})?$")


async def _my_watches(bot, uid) -> set[str]:
    rows = await execute_db(bot, "SELECT species FROM user_species_watch WHERE user_id=?", (str(uid),), fetch=True) or []
    return {str(r["species"]).lower() for r in rows}


async def _star_ctx(req, name: str) -> dict:
    from utils.availability import normalize_species_name
    uid = _aam_member(req)
    back = req.path
    if not uid:
        return {"uid": None, "login": _login_url(req), "back": back}
    w = await _my_watches(req.app["bot"], uid)
    genus = name.split()[0]
    return {"uid": uid, "csrf": _member_csrf(uid), "back": back,
            "watched": normalize_species_name(name).lower() in w,
            "genus_watched": normalize_species_name(genus).lower() in w}


def _fnum(raw):
    try:
        v = float(str(raw).replace(",", ".").strip())
        return v if 0 <= v < 100000 else None
    except (TypeError, ValueError):
        return None


async def _finder(req, idx: dict, lang: str) -> dict:
    """Filtert den Arten-Index nach Gattung, Preis (EUR), Shop-Land/CH-Lieferung und Lagerstand."""
    from utils.availability import normalize_species_name
    q = req.query
    g = (q.get("g") or "").strip()[:60]
    land = (q.get("land") or "").strip().lower()[:3]
    stock = q.get("stock") == "1"
    pmin_raw, pmax_raw = (q.get("pmin") or "").strip()[:10], (q.get("pmax") or "").strip()[:10]
    pmin, pmax = _fnum(pmin_raw), _fnum(pmax_raw)
    shops = idx.get("shops", {})
    genera = sorted({sp["genus"] for sp in idx["species"].values()}, key=str.lower)
    countries = sorted({sh.get("country") for sh in shops.values() if sh.get("country")})
    out = {"active": bool(g or land or stock or pmin is not None or pmax is not None),
           "g": g, "land": land, "stock": stock, "pmin_raw": pmin_raw, "pmax_raw": pmax_raw,
           "genera": genera, "countries": [(c, f"{country_name(lang, c)} ({c.upper()})") for c in countries],
           "rows": [], "more": 0, "uid": None}
    uid = _aam_member(req)
    if uid:
        out.update(uid=uid, csrf=_member_csrf(uid), back=req.path_qs if _BACK_RE.match(req.path_qs) else "/art")
    if not out["active"]:
        return out
    ch_ids = set()
    if land == "chd":
        ch_ids = {str(r["shop_id"]) for r in await execute_db(req.app["bot"],
                  "SELECT shop_id FROM ch_delivery_shops", fetch=True) or []}
    watched = await _my_watches(req.app["bot"], uid) if uid else set()

    def shop_ok(dom):
        if not land:
            return True
        sh = shops.get(dom) or {}
        if land == "chd":
            return sh.get("country") == "ch" or sh.get("id") in ch_ids
        return sh.get("country") == land

    rows = []
    for sp in idx["species"].values():
        if g and sp["genus"].lower() != g.lower():
            continue
        offs = [o for o in sp["offers"] if (not stock or o["in_stock"]) and shop_ok(o["domain"])
                and (pmin is None or (o["price"] is not None and o["price"] >= pmin))
                and (pmax is None or (o["price"] is not None and o["price"] <= pmax))]
        if not offs:
            continue
        prices = [o["price"] for o in offs if o["price"] is not None]
        nq = {"lang": lang, "nf": sp["name"]}
        if land == "chd":
            nq["ns"] = "1"
        elif land:
            nq["nr"] = land
        rows.append({"name": sp["name"], "shops": len({o["domain"] for o in offs}),
                     "instock": sum(1 for o in offs if o["in_stock"]),
                     "min": min(prices) if prices else None,
                     "price": (translate(lang, "fnd_from") + " " + _eur_fmt(min(prices))) if prices else "–",
                     "watched": normalize_species_name(sp["name"]).lower() in watched,
                     "nq": urlencode(nq)})
    rows.sort(key=lambda r: (r["min"] is None, r["min"] or 0, r["name"].lower()))
    out["more"] = max(0, len(rows) - 300)
    out["rows"] = rows[:300]
    return out


async def h_inat(req):
    """Öffentliche Artenliste der iNat-Statistik (nur Summen, keine Namen)."""
    from utils import inat as I
    lang = pick_lang(req)
    d = await I.community_species(req.app["bot"])
    groups: dict = {}
    for x in d["taxa"]:
        note = translate(lang, "inat_row_note", n=x["obs"], y=x["first"]) if x["first"] else ""
        groups.setdefault(x["name"].split()[0], []).append({"name": x["name"], "note": note, "instock": False})
    return _render(req, "inat_list", title=translate(lang, "inat_page_h"), d=d,
                   groups=sorted(groups.items(), key=lambda g: g[0].lower()))


async def h_art_card(req):
    """Teilbare Artenkarte als PNG (Download-Button auf der Artseite)."""
    from utils import directory as dirx, species_card, season as SEA
    lang = pick_lang(req)
    name = dirx.unslug(req.match_info.get("name", ""))[:120]
    if not _rate("card:" + _hmac("card", _ip(req)), 20, 60):
        raise web.HTTPTooManyRequests()
    await ensure_rates()
    sp = await asyncio.to_thread(dirx.find_species, name)
    if not sp:
        raise web.HTTPNotFound()
    trend = await asyncio.to_thread(dirx.price_trend, sp, 12)
    prof = await SEA.profile(req.app["bot"], sp["key"])
    url = (BOARD_PUBLIC_URL.split("://", 1)[-1] + f"/art/{dirx.slug(sp['name'])}") if BOARD_PUBLIC_URL else ""
    png = await asyncio.to_thread(species_card.render, sp, trend, lang, prof,
                                  SEA.MONTHS_SHORT.get(lang, SEA.MONTHS_SHORT["de"]), url)
    fn = dirx.slug(sp["name"]) + ".png"
    return web.Response(body=png, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600",
                                 "Content-Disposition": f'inline; filename="{fn}"'})


async def h_art(req):
    from utils import directory as dirx
    lang = pick_lang(req)
    name = dirx.unslug(req.match_info.get("name", ""))[:120]
    await ensure_rates()
    sp = await asyncio.to_thread(dirx.find_species, name)
    if not sp:
        canon = await asyncio.to_thread(dirx.canonical_name, name)
        from utils import species_profile
        resp = _render(req, "art_missing", title=name, art_name=canon or name, known=bool(canon),
                       ext=species_profile.links(canon or name))
        resp.set_status(404)
        return resp
    if dirx.unslug(sp["name"]) != name:                       # Synonym/Schreibweise → kanonische URL
        raise web.HTTPFound(f"/art/{dirx.slug(sp['name'])}?lang={lang}")
    trend = await asyncio.to_thread(dirx.price_trend, sp, 12)
    offers = sorted(sp["offers"], key=lambda o: (not o["in_stock"], o["price"] is None, o["price"] or 0))
    instock = [o for o in offers if o["in_stock"]]
    fetched = shop_stats._fmt_ts(dirx.index().get("fetched_at"))
    from utils import species_profile
    try:
        prof = species_profile.view(await species_profile.get(sp["name"]), lang)
    except Exception as e:
        logger.info("🐜 Steckbrief %s nicht verfügbar: %s", sp["name"], e)
        prof = {"wiki": None, "gbif": None}
    gb = prof.get("gbif") or {}
    gbif_top = [country_name(lang, c) for c, _ in (gb.get("countries") or [])[:5] if c]
    gbif_count = f"{gb.get('count', 0):,}".replace(",", ".") if lang == "de" else f"{gb.get('count', 0):,}"
    from utils import price_signal as PS, season as SEA
    from utils.localization import l10n
    sig = await asyncio.to_thread(PS.signals, [o["pid"] for o in instock])
    seas = None
    sprof = await SEA.profile(req.app["bot"], sp["key"])
    if sprof:
        seas = {"h": l10n.get("season_h", lang), "text": SEA.describe(sprof, lang),
                "months": sprof["months"], "names": SEA.MONTHS.get(lang, SEA.MONTHS["de"]),
                "short": SEA.MONTHS_SHORT.get(lang, SEA.MONTHS_SHORT["de"])}
    star = await _star_ctx(req, sp["name"])
    swarm = await SWB.art_box(req.app["bot"], sp["name"], lang)
    resp = _render(req, "art", title=sp["name"], sp=sp, offers=offers, instock=instock, star=star,
                   swarm=swarm, sw_short=[m[:3] for m in _SW_MONTHS.get(lang, _SW_MONTHS["de"])],
                   flash=_konto_flash(req),
                   trend=trend, rare=len(sp["shops"]) == 1, eur=_eur_fmt, price=dirx.fmt_price,
                   fetched=fetched, season=seas,
                   sig=sig, sig_any=any(sig.get(o["pid"]) for o in instock), sig_emoji=PS.EMOJI,
                   sig_txt={k: l10n.get(f"price_sig_{k}", lang) for k in PS.EMOJI},
                   sig_legend=l10n.get("price_sig_legend", lang),
                   prof=prof, ext=species_profile.links(sp["name"]),
                   gbif_top=gbif_top, gbif_count=gbif_count)
    if star["uid"]:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp
