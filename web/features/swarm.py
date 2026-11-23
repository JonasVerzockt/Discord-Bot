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
web/features/swarm.py – Schwarmflug: öffentliche Seite, Status-JSON und Admin-Korrekturen.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import asyncio
import re
from aiohttp import web
from utils import board_swarm as SWB, geo
from datetime import datetime
from utils.db import execute_db
from utils.board_i18n import country_name, pick_lang, translate

from web.core.render import _render
from web.core.security import (
    _aam_member, _admin_session, _csrf_ok, _csrf_token, _hmac, _ip, _is_admin, _login_url,
    _rate,
)
from utils.web_keys import BOT_KEY


_SW_MONTHS = {"de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
                     "Oktober", "November", "Dezember"],
              "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                     "October", "November", "December"],
              "eo": ["Januaro", "Februaro", "Marto", "Aprilo", "Majo", "Junio", "Julio", "Aŭgusto", "Septembro",
                     "Oktobro", "Novembro", "Decembro"]}


async def h_swarm_admin(req):
    """Admin: Meldungen ohne Land und Region (fließen nicht in die Auswertung ein)."""
    lang = pick_lang(req)
    if not _is_admin(req):
        raise web.HTTPFound(f"/admin/login?lang={lang}")
    from config import SWARM_CHANNEL_ID
    bot = req.app[BOT_KEY]
    unclear = [r for r in await SWB.S.rows(bot, include_indoor=True, scope="unclear") if not r["indoor"]]
    ch = bot.get_channel(SWARM_CHANNEL_ID) if SWARM_CHANNEL_ID else None
    gid = getattr(getattr(ch, "guild", None), "id", None)
    by: dict = {}
    for r in unclear:
        e = by.setdefault(r["message_id"], {"mid": r["message_id"], "flight_date": r["flight_date"],
                                            "place": r["place"], "labels": [], "state": r["geo_src"] or "",
                                            "link": f"https://discord.com/channels/{gid}/{SWARM_CHANNEL_ID}/{r['message_id']}"
                                            if gid else ""})
        e["labels"].append(r["label"])
    items = sorted(by.values(), key=lambda x: x["flight_date"], reverse=True)
    for e in items:
        e["labels"] = ", ".join(e["labels"])
    n_d = await execute_db(bot, "SELECT COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE "
                                "country IN ('de','at','ch','li')", fetch=True)
    n_a = await execute_db(bot, "SELECT COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE country<>'' AND "
                                "country NOT IN ('de','at','ch','li')", fetch=True)
    fixed = [dict(x) for x in await execute_db(bot,
        "SELECT raw_label, label, taxon_check, COUNT(DISTINCT message_id) AS n FROM swarm_reports "
        "WHERE raw_label IS NOT NULL GROUP BY raw_label, label, taxon_check "
        "ORDER BY taxon_check IN ('typo', 'fixed') DESC, n DESC LIMIT 300", fetch=True) or []]
    home = await execute_db(bot, "SELECT COUNT(DISTINCT message_id) AS n FROM swarm_reports WHERE geo_src='home'", fetch=True)
    # Karte Wetter-Abdeckung: t = zur Uhrzeit, e = geschätzte Stunde, o = noch offen, f = nicht abrufbar
    wc, cnt = [], {"st": {}, "src": {}}
    for r in await execute_db(bot, "SELECT lat, lon, flight_time, flight_date, w_state, w_src, label FROM swarm_reports "
                                    "WHERE indoor=0 AND country IN ('de','at','ch','li') AND lat IS NOT NULL",
                              fetch=True) or []:
        st = ("t" if r["flight_time"] else "e") if r["w_state"] == 1 else ("o" if r["w_state"] is None else "f")
        src = (r["w_src"] or "") if r["w_state"] == 1 else ""
        cnt["st"][st] = cnt["st"].get(st, 0) + 1
        cnt["src"][src] = cnt["src"].get(src, 0) + 1
        wc.append([round(r["lat"], 4), round(r["lon"], 4), st, src, r["label"],
                   datetime.strptime(r["flight_date"], "%Y-%m-%d").strftime("%d.%m.%Y") if r["flight_date"] else ""])
    regonly = await execute_db(bot, "SELECT COUNT(*) AS n FROM swarm_reports WHERE indoor=0 AND lat IS NULL AND "
                                    "region<>'' AND country IN ('de','at','ch','li')", fetch=True)
    # Karte Gelernt gegen gemeldet
    try:
        ev_taxa = [r["taxon"] for r in await execute_db(bot,
            "SELECT e.taxon FROM fc_eval e LEFT JOIN fc_models m ON m.taxon=e.taxon GROUP BY e.taxon "
            "ORDER BY MAX(m.n_pos) DESC", fetch=True) or []]
    except Exception:
        ev_taxa = []
    ev_taxon = req.query.get("et") if req.query.get("et") in ev_taxa else (ev_taxa[0] if ev_taxa else "")
    ev = [[r["lat"], r["lon"], 1 if r["kind"] == "hot" else 0, r["days"], r["n_hi"], r["n_hit"], r["n_rep"]]
          for r in await execute_db(bot, "SELECT e.*, p.lat, p.lon, p.kind FROM fc_eval e JOIN fc_points p "
                                          "ON p.key=e.key WHERE e.taxon=?", (ev_taxon,), fetch=True) or []] \
        if ev_taxon else []
    T = lambda k: translate(lang, k)
    amap = {"wc": wc, "cnt": cnt, "ev": ev,
            "t": {"st": {k: T("sw_wc_st_" + k) for k in "teof"}, "src": {k: T("sw_wc_src_" + k) for k in ("hf", "fc", "ifs")},
                  "nosrc": T("sw_wc_src_none"), "hot": T("sw_lrn_hot"), "grid": T("sw_lrn_grid"),
                  "days": T("sw_ev_t_days"), "hi": T("sw_ev_t_hi"), "hit": T("sw_ev_t_hit"), "rep": T("sw_ev_t_rep")}}
    manual = [dict(x) for x in await execute_db(bot, "SELECT * FROM swarm_override ORDER BY set_at DESC LIMIT 100",
                                                fetch=True) or []]
    resp = _render(req, "swarm_admin", title=translate(lang, "sw_unclear_h"), items=items,
                   n_dach=n_d[0]["n"] if n_d else 0, n_abroad=n_a[0]["n"] if n_a else 0,
                   n_home=home[0]["n"] if home else 0, fixed=fixed, manual=manual, csrf=_csrf_token(req),
                   amap=amap, wc_regonly=regonly[0]["n"] if regonly else 0, ev_taxa=ev_taxa, ev_taxon=ev_taxon,
                   n_guess=sum(1 for f in fixed if f["taxon_check"] in ("typo", "fixed")),
                   names_msg={"set": translate(lang, "sw_names_set_ok"), "bad": translate(lang, "sw_names_set_bad"),
                              "all": translate(lang, "sw_names_all_ok")}.get(req.query.get("nm", ""), ""),
                   regions=_sw_regions(lang), ok=req.query.get("ok", ""))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


async def h_swarm_status(req):
    """Live-Stand der Datensammlung (alle 10 s vom Kasten abgefragt)."""
    lang = pick_lang(req)
    if not _rate("swst:" + _hmac("swst", _ip(req)), 30, 60):
        raise web.HTTPTooManyRequests()
    prog = await SWB.progress(req.app[BOT_KEY], lambda key, **kw: translate(lang, key, **kw))
    return web.json_response(prog, headers={"Cache-Control": "no-store"})


async def h_swarm_fix(req):
    """Admin: Korrektur von Hand speichern (gilt bis zur Bearbeitung in Discord)."""
    lang = pick_lang(req)
    if not _is_admin(req):
        raise web.HTTPForbidden()
    form = await req.post()
    if not _csrf_ok(req, form):
        raise web.HTTPForbidden(text="bad csrf")
    from utils import swarm_resolve as R
    mid = re.sub(r"\D", "", str(form.get("mid") or ""))[:25]
    where = str(form.get("where") or "")
    c, _, r = where.partition("-")
    if not mid or (where and c not in geo.all_regions()):
        raise web.HTTPSeeOther(f"/admin/schwarmflug?lang={lang}")
    s = _admin_session(req)
    await R.set_override(req.app[BOT_KEY], mid, str(form.get("place") or "")[:160].strip(), c if where else "",
                         "" if c == "li" else r, str(form.get("taxa") or "")[:200].strip(), s[0] if s else "")
    await R.apply_override(req.app[BOT_KEY], mid)
    raise web.HTTPSeeOther(f"/admin/schwarmflug?lang={lang}&ok={mid}#m{mid}")


async def h_swarm_names(req):
    """Admin: vermutete Tippfehler-Korrektur bestätigen (✓) oder verwerfen (✗ → Gattung sp.)."""
    lang = pick_lang(req)
    if not _is_admin(req):
        raise web.HTTPForbidden()
    form = await req.post()
    if not _csrf_ok(req, form):
        raise web.HTTPForbidden(text="bad csrf")
    raw, label, act = (str(form.get(k) or "")[:200] for k in ("raw", "label", "act"))
    bot = req.app[BOT_KEY]
    cond = "raw_label=? AND label=? AND taxon_check IN ('typo', 'fixed')"
    back = f"/admin/schwarmflug?lang={lang}"
    if act == "all_ok":
        await execute_db(bot, "UPDATE swarm_reports SET taxon_check='typo_ok' WHERE raw_label IS NOT NULL AND "
                              "taxon_check IN ('typo', 'fixed')", commit=True)
        raise web.HTTPSeeOther(back + "&nm=all")
    if act == "set":
        from utils import species_catalog as SC
        name = re.sub(r"\s+", " ", str(form.get("name") or "")).strip()[:80]
        res = SC.check(name) if name else {"status": "empty"}
        canon = res.get("canonical") if res.get("status") in ("accepted", "synonym") else None
        if not canon:
            raise web.HTTPSeeOther(back + "&nm=bad")
        parts = canon.split()
        g, sp = parts[0], (parts[1].lower() if len(parts) > 1 else None)
        rows = await execute_db(bot, "SELECT id, qualifier FROM swarm_reports WHERE raw_label=? AND label=? AND "
                                     "taxon_check IN ('unknown', 'unknown_ai', 'rejected')", (raw, label),
                                fetch=True) or []
        for r in rows:
            q = "Gruppe" if "Gruppe" in (r["qualifier"] or "") and sp else ("sp." if not sp else "")
            t = {"genus": g, "species": sp, "qualifier": q}
            await execute_db(bot, "UPDATE OR REPLACE swarm_reports SET genus=?, species=?, qualifier=?, label=?, "
                                  "taxon_check='manual' WHERE id=?",
                             (g, sp, q, SWB.S.taxon_label(t), r["id"]), commit=True)
        raise web.HTTPSeeOther(back + "&nm=set")
    if act == "ok":
        await execute_db(bot, f"UPDATE swarm_reports SET taxon_check='typo_ok' WHERE {cond}", (raw, label), commit=True)
    elif act == "no":
        rows = await execute_db(bot, f"SELECT id, genus, qualifier FROM swarm_reports WHERE {cond}", (raw, label),
                                fetch=True) or []
        for r in rows:
            q = "cf." if "cf." in (r["qualifier"] or "") else ""
            t = {"genus": r["genus"], "species": None, "qualifier": q or "sp."}
            await execute_db(bot, "UPDATE OR REPLACE swarm_reports SET species=NULL, qualifier=?, label=?, "
                                  "taxon_check='rejected' WHERE id=?",
                             (t["qualifier"], SWB.S.taxon_label(t), r["id"]), commit=True)
    raise web.HTTPSeeOther(f"/admin/schwarmflug?lang={lang}")


async def h_swarm(req):
    lang = pick_lang(req)
    uid = _aam_member(req)
    try:
        from utils import directory as dirx
        idx = await asyncio.to_thread(dirx.index)
        known = {v["name"].lower(): v["name"] for v in idx.get("species", {}).values() if v.get("name")}
    except Exception:
        dirx, known = None, {}

    def art_slug(name):
        hit = known.get(name.lower())
        return dirx.slug(hit) if hit and dirx else None
    ctx = await SWB.page(req.app[BOT_KEY], req.query.get("z", ""), (req.query.get("g") or "").strip()[:40],
                         bool(uid), lang, art_slug, _SW_MONTHS.get(lang, _SW_MONTHS["de"]),
                         lambda key, **kw: translate(lang, key, **kw), q=dict(req.query))
    resp = _render(req, "schwarmflug", title=translate(lang, "sw_h"), login_url=_login_url(req), **ctx)
    resp.headers["Cache-Control"] = "private, no-store" if uid else "public, max-age=300"
    return resp


def _swarm_on() -> int:
    from config import SWARM_CHANNEL_ID
    return SWARM_CHANNEL_ID


def _fc_on() -> bool:
    from config import SWARM_FORECAST
    return bool(SWARM_FORECAST)


def _sw_regions(lang: str) -> list[tuple[str, str]]:
    """Auswahl für den Schwarmflug-Alarm: überall, ein Land oder eine Region."""
    out = [("", "🌍 " + translate(lang, "konto_sw_anywhere"))]
    for c, regs in geo.all_regions().items():
        out.append((c, f"{country_name(lang, c)} ({translate(lang, 'konto_sw_whole')})"))
        if c != "li":
            out += [(f"{c}-{code}", f"  · {name}") for code, name in sorted(regs.items(), key=lambda x: x[1])]
    return out


def _sw_where(lang: str, country: str, region: str) -> str:
    if not country:
        return translate(lang, "konto_sw_anywhere")
    if not region or country == "li":
        return country_name(lang, country)
    return SWB.S.region_name(country, region)
