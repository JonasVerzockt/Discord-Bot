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
web/features/account.py – „Mein Konto“: Einstellungen, Benachrichtigungen, Datenexport, persönlicher Kalender.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import asyncio
import hmac
import json
import re
import time
from aiohttp import web
from utils import account as ACC, geo, map_tags
from datetime import datetime, timedelta
from utils.db import execute_db
from utils.timez import BERLIN
from urllib.parse import urlencode
from utils.board_i18n import country_name, pick_lang, T as _BI18N_T, translate
from config import BOARD_PUBLIC_URL, DISCORD_INVITE_URL, MAP_GUILD_ID

from web.core.render import _render
from web.core.security import (
    _aam_member, _ADMIN_COOKIE, _hmac, _is_member, _login_url, _map_rate_ok, _MEMBER_COOKIE,
    _member_csrf, _member_name, _rate,
)
from web.features.events import _ics_build, _naive
from web.features.shops import _all_shops_safe
from web.features.species import _BACK_RE
from web.features.swarm import _fc_on, _sw_regions, _sw_where, _swarm_on

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


_KONTO_DANGER = {"preis/entfernen", "beobachtung/entfernen", "blacklist/entfernen", "schlagwort/entfernen",
                 "termin/absagen", "karte/loeschen", "loeschen", "schwarm/entfernen"}
_KONTO_STATUSES = ("active", "paused", "completed", "pending_feedback", "expired", "failed")
_KONTO_SPECIES_CACHE: dict = {"at": 0.0, "names": []}
_MD_STRIP = re.compile(r"(\*\*|__|`+)")


def _konto_species_names() -> list[str]:
    """Vorschläge für die Eingabefelder: alle angebotenen Arten + ihre Gattungen (10 min Cache)."""
    if time.time() - _KONTO_SPECIES_CACHE["at"] < 600 and _KONTO_SPECIES_CACHE["names"]:
        return _KONTO_SPECIES_CACHE["names"]
    try:
        from utils import directory as dirx
        names = {v["name"] for v in dirx.index()["species"].values() if v.get("name")}
    except Exception:
        names = set()
    genera = {n.split()[0] for n in names if " " in n}
    out = sorted(genera) + sorted(names)
    _KONTO_SPECIES_CACHE.update(at=time.time(), names=out[:4000])
    return _KONTO_SPECIES_CACHE["names"]


def _ics_personal_url(uid: str, salt: str) -> str:
    base = BOARD_PUBLIC_URL or ""
    return f"{base}/konto/kalender/{uid}/{salt}.ics"


async def _konto_page(req, uid, flash: str = "", form: dict | None = None, open_form: str = "",
                      status: int = 200):
    from utils.availability import load_shop_data, strip_html
    from utils.localization import l10n
    lang = pick_lang(req)
    bot = req.app["bot"]
    d = await ACC.collect(bot, uid, lang)
    shop_data = await load_shop_data(bot)
    shops = sorted(((sid, sd.get("name") or sid) for sid, sd in shop_data.items()), key=lambda x: x[1].lower())
    names = dict(shops)
    countries = sorted({(sd.get("country") or "").lower() for sd in shop_data.values() if sd.get("country")})
    regions = [("eu", "🇪🇺 " + translate(lang, "konto_region_eu"))] + \
              [(c, f"{country_name(lang, c)} ({c.upper()})") for c in countries]
    year = datetime.now(BERLIN).year
    # Produktsuche (GET ?ps=…)
    ps = (req.query.get("ps") or "").strip()[:80]
    ps_results, ps_more = [], 0
    if ps:
        from utils.price_tracking import _find_products_for_tracking
        have = {(int(p["product_id"]), int(p["variant_id"] or 0)) for p in d["tracked"]}
        found = await _find_products_for_tracking(bot, ps)
        for sid, blk in found.items():
            for p in blk["products"]:
                try:
                    lo, hi = float(p.get("min_price") or 0), float(p.get("max_price") or 0)
                except (TypeError, ValueError):
                    lo = hi = 0.0
                price = "?" if p.get("_no_price") else (f"{lo:.2f}" + (f"–{hi:.2f}" if hi and hi != lo else "")
                                                        + f" {p.get('currency_iso') or ''}")
                ps_results.append({"key": f"{sid}|{p.get('id')}", "shop": blk["shop_info"].get("name", sid),
                                   "title": strip_html(p.get("title") or p.get("species") or "")[:140],
                                   "price": price, "stock": bool(p.get("in_stock")),
                                   "tracked": (int(p.get("id") or 0), 0) in have})
        ps_results.sort(key=lambda r: (not r["stock"], r["shop"].lower(), r["title"].lower()))
        if len(ps_results) > 80:
            ps_more = len(ps_results) - 80
            ps_results = ps_results[:80]
    salt = ACC.prefs(uid)["ics_salt"]
    ics_url = _ics_personal_url(uid, salt) if salt else ""
    tag_groups = [{"label": (g["label"].get(lang) or g["label"].get("de")),
                   "tags": [(c, (l.get(lang) or l.get("de") or c)) for c, l in g["tags"]]}
                  for g in map_tags.TAG_GROUPS]
    try:
        from cogs.offer_alerts import MAX_KEYWORDS
        from config import OFFER_CHANNEL_ID
    except Exception:
        MAX_KEYWORDS, OFFER_CHANNEL_ID = 25, 0
    from config import COMMAND_LOG_RETENTION_DAYS
    ach_cats = [(c, _MD_STRIP.sub("", l10n.get("ach_cat_" + c, lang))) for c in
                ("avail", "price", "community", "usage", "hidden")]
    form = form or {}
    form.setdefault("regions", [])
    resp = _render(req, "konto", title=translate(lang, "konto_h"), flash=flash, uid=uid, d=d,
                   member_name=_member_name(req.app, uid) or "", csrf=_member_csrf(uid),
                   year=year, sts=_KONTO_STATUSES,
                   n_active=sum(1 for n in d["notifications"] if n["status"] == "active"),
                   n_reviews_year=sum(1 for r in d["reviews"] if str(r["review_date"]).startswith(str(year))),
                   quiet_now=ACC.is_quiet(uid), regions=regions, form=form, open_form=open_form,
                   shops=[s for s in shops if s[0] not in set(d["blacklist"])],
                   blacklist=[(sid, names.get(sid, sid)) for sid in d["blacklist"]],
                   shop_names={dom: sh["name"] for dom, sh in (await asyncio.to_thread(_all_shops_safe)).items()},
                   kw_max=MAX_KEYWORDS, kw_enabled=bool(OFFER_CHANNEL_ID),
                   ps=ps, ps_results=ps_results, ps_more=ps_more,
                   ics_url=ics_url, webcal_url=("webcal://" + ics_url.split("://", 1)[-1]) if ics_url else "",
                   map_country=(country_name(lang, d["map"]["country"]) if d["map"] and d["map"].get("country") else ""),
                   tag_groups=tag_groups, ach_cats=ach_cats,
                   dm_days=ACC.DM_LOG_DAYS, dm_max=ACC.DM_LOG_MAX,
                   areas=list(ACC.AREAS), log_days=COMMAND_LOG_RETENTION_DAYS,
                   species_names=_konto_species_names(),
                   sw_enabled=bool(_swarm_on()), sw_regions=_sw_regions(lang),
                   sw_start_on=any(a["taxon"] == "*" for a in d["swarm_alerts"]),
                   fc_enabled=_fc_on(),
                   sw_where=lambda c, r: _sw_where(lang, c, r),
                   sw_default=(f"{d['map']['country']}-{d['map'].get('region_code') or ''}".rstrip("-")
                               if d["map"] and d["map"].get("country") else ""),
                   login_url="")
    resp.headers["Cache-Control"] = "private, no-store"
    resp.set_status(status)
    return resp


async def h_konto(req):
    lang = pick_lang(req)
    uid = _aam_member(req)
    m = req.query.get("m", "")
    flash = translate(lang, "konto_m_" + m, n=req.query.get("n", "")) if m and ("konto_m_" + m) in _BI18N_T else ""
    if not uid:
        former = _is_member(req)                    # gültige Sitzung, aber nicht mehr auf dem Server
        from config import COMMAND_LOG_RETENTION_DAYS
        resp = _render(req, "konto", title=translate(lang, "konto_h"), flash=flash, uid=None,
                       former=former, csrf=_member_csrf(former) if former else "",
                       invite_url=(DISCORD_INVITE_URL if DISCORD_INVITE_URL.startswith("https://") else ""),
                       log_days=COMMAND_LOG_RETENTION_DAYS,
                       login_url="" if former else _login_url(req))
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    nf = (req.query.get("nf") or "").strip()[:80]
    if nf:                                            # aus dem Arten-Finder: Formular vorbefüllen
        nr = (req.query.get("nr") or "").strip().lower()
        pre = {"kind": "species" if " " in nf else "genus", "name": nf,
               "regions": [nr] if re.fullmatch(r"[a-z]{2}", nr) else [],
               "swiss": req.query.get("ns") == "1"}
        return await _konto_page(req, uid, flash=flash, form=pre, open_form="notif")
    return await _konto_page(req, uid, flash=flash)


def _konto_back(req, code: str, anchor: str = "", n="") -> web.HTTPSeeOther:
    lang = pick_lang(req)
    q = {"lang": lang, "m": code}
    if n != "":
        q["n"] = str(n)
    return web.HTTPSeeOther(f"/konto?{urlencode(q)}" + (f"#{anchor}" if anchor else ""))


def _num(v, cast=int):
    try:
        return cast(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


async def h_konto_post(req):
    """Alle Änderungen aus „Mein Konto“ (Formular-POST, CSRF + Rate-Limit, nur eigene Daten)."""
    lang = pick_lang(req)
    uid = _aam_member(req)
    if not uid:
        # Nicht mehr auf dem Server: nur noch „alles löschen“ (danach abmelden).
        former = _is_member(req)
        if former and req.match_info["action"] == "loeschen":
            form = await req.post()
            if hmac.compare_digest(str(form.get("csrf", "")), _member_csrf(former)) \
                    and form.get("area") == "all" and form.get("sure") \
                    and _rate("konto:" + _hmac("konto", former), 40, 300):
                await ACC.delete_area(req.app["bot"], former, "all")
                resp = web.HTTPSeeOther(f"/konto?lang={lang}&m=left_deleted")
                resp.del_cookie(_MEMBER_COOKIE)
                resp.del_cookie(_ADMIN_COOKIE)
                logger.info("👤 Konto: Daten nach Server-Austritt gelöscht (%s)", former)
                raise resp
        raise web.HTTPSeeOther(f"/konto?lang={lang}")
    form = await req.post()
    if not hmac.compare_digest(str(form.get("csrf", "")), _member_csrf(uid)):
        raise web.HTTPForbidden(text="bad csrf")
    if not _rate("konto:" + _hmac("konto", uid), 40, 300):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app["bot"]
    act = req.match_info["action"]
    T = lambda key, **kw: translate(lang, key, **kw)
    # Löschen nur mit freigeschaltetem Schalter „Löschen freischalten“ (Seite oben).
    if (act in _KONTO_DANGER or (act == "benachrichtigung" and form.get("do") == "delete")) \
            and form.get("unlock") != "1":
        raise _konto_back(req, "locked", "")

    # ── Benachrichtigungen ────────────────────────────────────────────────────
    if act == "benachrichtigung":
        nid, do = _num(form.get("id")), form.get("do")
        if nid is None:
            raise _konto_back(req, "error", "benachrichtigungen")
        if do == "delete":
            await execute_db(bot, "DELETE FROM notifications WHERE id=? AND user_id=?", (nid, uid), commit=True)
            raise _konto_back(req, "deleted", "benachrichtigungen")
        if do == "pause":
            await execute_db(bot, "UPDATE notifications SET status='paused' WHERE id=? AND user_id=? "
                                  "AND status='active'", (nid, uid), commit=True)
            raise _konto_back(req, "paused", "benachrichtigungen")
        if do == "resume":
            rows = await execute_db(bot, "SELECT species, regions, excluded_species FROM notifications "
                                         "WHERE id=? AND user_id=?", (nid, uid), fetch=True)
            if rows:
                await execute_db(bot, "UPDATE notifications SET status='active', created_at=CURRENT_TIMESTAMP, "
                                      "pending_feedback_until=NULL WHERE id=? AND user_id=?", (nid, uid), commit=True)
                r = rows[0]
                _konto_check_now(bot, uid, r["species"], r["regions"], r["excluded_species"])
            raise _konto_back(req, "resumed", "benachrichtigungen")
        raise _konto_back(req, "error", "benachrichtigungen")

    if act == "benachrichtigung/neu":
        return await _konto_notif_add(req, uid, form)

    # ── Preise ────────────────────────────────────────────────────────────────
    if act == "preis/entfernen":
        pid, vid = _num(form.get("pid")), _num(form.get("vid")) or 0
        await execute_db(bot, "DELETE FROM user_price_tracking WHERE user_id=? AND product_id=? AND variant_id=?",
                         (uid, pid, vid), commit=True)
        raise _konto_back(req, "deleted", "preise")

    if act == "zielpreis":
        pid, vid = _num(form.get("pid")), _num(form.get("vid")) or 0
        raw = (form.get("price") or "").strip()
        mode = form.get("mode") if form.get("mode") in ("additional", "replace") else "additional"
        if not raw:
            await execute_db(bot, "UPDATE user_price_tracking SET target_price=NULL, target_mode=NULL "
                                  "WHERE user_id=? AND product_id=? AND variant_id=?", (uid, pid, vid), commit=True)
            raise _konto_back(req, "target_off", "preise")
        price = _num(raw, float)
        if price is None or price <= 0 or price > 100000:
            raise _konto_back(req, "bad_price", "preise")
        await execute_db(bot, "UPDATE user_price_tracking SET target_price=?, target_mode=? "
                              "WHERE user_id=? AND product_id=? AND variant_id=?",
                         (round(price, 2), mode, uid, pid, vid), commit=True)
        raise _konto_back(req, "target_set", "preise")

    if act == "preis/neu":
        from utils.price_tracking import _find_products_for_tracking, save_tracking
        species = (form.get("species") or "").strip()[:80]
        sel = {str(x) for x in form.getall("sel", [])}
        if not species or not sel:
            raise _konto_back(req, "nothing", "preise")
        found = await _find_products_for_tracking(bot, species)
        saved = 0
        for sid, blk in found.items():
            for p in blk["products"]:
                if f"{sid}|{p.get('id')}" in sel and p.get("id") is not None:
                    if await save_tracking(bot, uid, sid, blk["shop_info"].get("name", sid), p):
                        saved += 1
        raise _konto_back(req, "track_added", "preise", n=saved)

    if act == "beobachtung/neu":
        from utils.availability import normalize_species_name
        from utils import species_catalog
        from utils.price_tracking import init_species_watch_seen
        raw = (form.get("species") or "").strip()[:80]
        if len(raw) < 3:
            raise _konto_back(req, "nothing", "preise")
        canon = species_catalog.canonical(raw)
        norm = normalize_species_name(canon or raw)
        is_genus = 0 if " " in norm.strip() else 1
        rc = await execute_db(bot, "INSERT INTO user_species_watch (user_id, species, is_genus) VALUES (?,?,?) "
                                   "ON CONFLICT(user_id, species) DO NOTHING", (uid, norm, is_genus), commit=True)
        if rc:
            await init_species_watch_seen(bot, uid, norm, is_genus)
        back = str(form.get("back") or "")
        if _BACK_RE.match(back):                         # Stern auf /art: zurück zur Seite
            path, _, qs_ = back.partition("?")
            qd = dict(p.split("=", 1) for p in qs_.split("&") if "=" in p) if qs_ else {}
            qd = {k: v for k, v in qd.items() if k not in ("m", "n", "lang")}
            from urllib.parse import unquote_plus
            qd = {k: unquote_plus(v) for k, v in qd.items()}
            qd.update(lang=lang, m="watch_added" if rc else "exists")
            raise web.HTTPSeeOther(path + "?" + urlencode(qd))
        raise _konto_back(req, "watch_added" if rc else "exists", "preise")

    if act == "beobachtung/entfernen":
        sp = (form.get("species") or "").strip()
        for sql in ("DELETE FROM user_species_watch WHERE user_id=? AND species=?",
                    "DELETE FROM user_species_watch_seen WHERE user_id=? AND watched_species=?",
                    "DELETE FROM user_species_watch_variant_seen WHERE user_id=? AND watched_species=?",
                    "DELETE FROM pending_variant_removed WHERE user_id=? AND watched_species=?"):
            await execute_db(bot, sql, (uid, sp), commit=True)
        raise _konto_back(req, "deleted", "preise")

    # ── Einstellungen ─────────────────────────────────────────────────────────
    if act == "einstellungen":
        lng = form.get("language") or ""
        if lng in ("de", "en", "eo"):
            await execute_db(bot, "DELETE FROM user_settings WHERE user_id=?", (uid,), commit=True)
            await execute_db(bot, "INSERT INTO user_settings (user_id, language) VALUES (?, ?) "
                                  "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language",
                             (int(uid), lng), commit=True)
        elif lng == "":
            for v in (int(uid), uid):
                await execute_db(bot, "DELETE FROM user_settings WHERE user_id=?", (v,), commit=True)
        if form.get("digest"):
            await execute_db(bot, "INSERT OR IGNORE INTO digest_subscribers (user_id) VALUES (?)", (uid,), commit=True)
        else:
            await execute_db(bot, "DELETE FROM digest_subscribers WHERE user_id=?", (uid,), commit=True)
        qs_, qe_ = _num(form.get("qs")), _num(form.get("qe"))
        quiet = bool(form.get("quiet")) and qs_ is not None and qe_ is not None \
            and 0 <= qs_ < 24 and 0 <= qe_ < 24 and qs_ != qe_
        await ACC.set_prefs(bot, uid, rsvp_reminder=1 if form.get("rsvp") else 0,
                            intros=1 if form.get("intros") else 0,
                            quiet_start=qs_ if quiet else None, quiet_end=qe_ if quiet else None)
        raise _konto_back(req, "saved", "einstellungen")

    if act == "blacklist/neu":
        from utils.availability import load_shop_data
        sid = str(form.get("shop") or "")
        if sid in await load_shop_data(bot):
            await execute_db(bot, "INSERT OR IGNORE INTO user_shop_blacklist (user_id, shop_id) VALUES (?, ?)",
                             (uid, sid), commit=True)
        raise _konto_back(req, "saved", "einstellungen")

    if act == "blacklist/entfernen":
        await execute_db(bot, "DELETE FROM user_shop_blacklist WHERE user_id=? AND shop_id=?",
                         (uid, str(form.get("shop") or "")), commit=True)
        raise _konto_back(req, "deleted", "einstellungen")

    if act == "schlagwort/neu":
        from cogs.offer_alerts import _plain, MAX_KEYWORDS
        cnt = await execute_db(bot, "SELECT COUNT(*) AS n FROM offer_keywords WHERE user_id=?", (uid,), fetch=True)
        have = cnt[0]["n"] if cnt else 0
        added = []
        for part in re.split(r"[;,]", form.get("kw") or ""):
            raw = part.strip()
            kw = _plain(raw)
            if not (2 <= len(kw) <= 80) or have >= MAX_KEYWORDS:
                continue
            rc = await execute_db(bot, "INSERT OR IGNORE INTO offer_keywords (user_id, keyword, keyword_raw) "
                                       "VALUES (?,?,?)", (uid, kw, raw), commit=True)
            if rc:
                have += 1
                added.append((kw, raw))
        cog = bot.cogs.get("OfferAlerts") if hasattr(bot, "cogs") else None
        if cog and added:
            async def _bf():
                try:
                    user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
                    for kw, raw in added:
                        await cog._backfill(user, uid, kw, raw)
                except Exception as e:
                    logger.info("👤 Konto: Rückblick für Schlagworte fehlgeschlagen: %s", e)
            asyncio.create_task(_bf())
        raise _konto_back(req, "kw_added", "einstellungen", n=len(added))

    if act == "schlagwort/entfernen":
        await execute_db(bot, "DELETE FROM offer_keywords WHERE user_id=? AND keyword=?",
                         (uid, str(form.get("kw") or "")), commit=True)
        await execute_db(bot, "DELETE FROM quiet_offer_queue WHERE user_id=? AND keyword=?",
                         (uid, str(form.get("kw") or "")), commit=True)
        raise _konto_back(req, "deleted", "einstellungen")

    # ── Schwarmflug-Alarm ─────────────────────────────────────────────────────
    if act == "schwarm/neu":
        from utils import swarm as SW
        if str(form.get("taxon") or "") == "*":            # PN beim Saisonstart
            await execute_db(bot, "INSERT OR IGNORE INTO swarm_alerts (user_id, taxon, country, region) "
                                  "VALUES (?,?,?,?)", (uid, "*", "", ""), commit=True)
            raise _konto_back(req, "sw_added", "schwarmflug")
        taxa = SW.parse_taxa(str(form.get("taxon") or "")[:60])
        if not taxa:
            raw = str(form.get("taxon") or "").strip().split()
            g = SW._genus_of(raw[0]) if raw else None
            taxa = [{"genus": g, "species": None}] if g else []
        where = str(form.get("where") or "")
        c, _, r = where.partition("-")
        ok_where = where == "" or (c in geo.all_regions() and (not r or r in geo.all_regions()[c]))
        cnt = await execute_db(bot, "SELECT COUNT(*) AS n FROM swarm_alerts WHERE user_id=?", (uid,), fetch=True)
        if not taxa or not ok_where or (cnt and cnt[0]["n"] >= 20):
            raise _konto_back(req, "sw_bad", "schwarmflug")
        tx = taxa[0]
        taxon = tx["genus"] + (" " + tx["species"] if tx.get("species") else "")
        await execute_db(bot, "INSERT OR IGNORE INTO swarm_alerts (user_id, taxon, country, region) VALUES (?,?,?,?)",
                         (uid, taxon, c, "" if c == "li" else r), commit=True)
        raise _konto_back(req, "sw_added", "schwarmflug")

    if act == "schwarm/prognose":
        lv = str(form.get("level") or "")
        if lv not in ("off", "rare", "normal", "often"):
            raise _konto_back(req, "error", "schwarmflug")
        await execute_db(bot, "INSERT INTO fc_pref (user_id, level) VALUES (?,?) ON CONFLICT(user_id) "
                              "DO UPDATE SET level=excluded.level", (uid, lv), commit=True)
        raise _konto_back(req, "fc_saved", "schwarmflug")

    if act == "schwarm/entfernen":
        await execute_db(bot, "DELETE FROM swarm_alerts WHERE user_id=? AND taxon=? AND country=? AND region=?",
                         (uid, str(form.get("taxon") or ""), str(form.get("country") or ""),
                          str(form.get("region") or "")), commit=True)
        raise _konto_back(req, "deleted", "schwarmflug")

    # ── Termine & Kalender ────────────────────────────────────────────────────
    if act == "termin/absagen":
        await execute_db(bot, "DELETE FROM map_event_rsvp WHERE event_id=? AND occ_date=? AND user_id=?",
                         (_num(form.get("eid")), str(form.get("occ") or "")[:10], uid), commit=True)
        raise _konto_back(req, "rsvp_removed", "termine")

    if act == "kalender/neu":
        await ACC.ics_salt(bot, uid, rotate=True)
        raise _konto_back(req, "ics_new", "termine")

    # ── Karte ─────────────────────────────────────────────────────────────────
    if act == "karte/tags":
        have = await execute_db(bot, "SELECT 1 FROM map_entries WHERE user_id=?", (uid,), fetch=True)
        if have:
            tags = [c for c in form.getall("tag", []) if map_tags.is_valid(c)]
            await execute_db(bot, "DELETE FROM map_entry_tags WHERE user_id=?", (uid,), commit=True)
            for c in dict.fromkeys(tags):
                await execute_db(bot, "INSERT OR IGNORE INTO map_entry_tags (user_id, tag_code) VALUES (?,?)",
                                 (uid, c), commit=True)
        raise _konto_back(req, "saved", "karte")

    if act == "karte/loeschen":
        for sql in ("DELETE FROM map_entry_tags WHERE user_id=?", "DELETE FROM map_entry_regions WHERE user_id=?",
                    "DELETE FROM map_entries WHERE user_id=?"):
            await execute_db(bot, sql, (uid,), commit=True)
        raise _konto_back(req, "map_deleted", "karte")

    # ── iNaturalist ───────────────────────────────────────────────────────────
    if act in ("inat/start", "inat/pruefen", "inat/trennen"):
        from utils import inat as I
        from utils.localization import l10n
        if act == "inat/trennen":
            await I.unlink(bot, uid)
            raise _konto_back(req, "inat_unlinked", "inat")
        if act == "inat/start":
            login = (form.get("login") or "").strip().lstrip("@")
            if not I.LOGIN_RE.match(login):
                return await _konto_page(req, uid, flash=_MD_STRIP.sub("", l10n.get("inat_link_bad_login", lang)))
            await I.start_link(bot, uid, login)
            raise _konto_back(req, "inat_code", "inat")
        status, u = await I.verify_link(bot, uid)
        if status == "ok":
            st = await I.user_stats(bot, uid)
            msg = l10n.get("inat_link_ok", lang, login=u["login"], obs=st["obs"], species=st["species"])
        else:
            msg = l10n.get(f"inat_link_{status}", lang)
        return await _konto_page(req, uid, flash=_MD_STRIP.sub("", msg))

    # ── Sitzungen & Datenschutz ───────────────────────────────────────────────
    if act == "abmelden":
        await ACC.set_prefs(bot, uid, logout_before=ACC.now_epoch())
        resp = web.HTTPSeeOther(f"/konto?lang={lang}&m=logout")
        resp.del_cookie(_MEMBER_COOKIE)
        resp.del_cookie(_ADMIN_COOKIE)
        logger.info("👤 Konto: überall abgemeldet (%s)", uid)
        raise resp

    if act == "loeschen":
        area = str(form.get("area") or "")
        if area == "all" and not form.get("sure"):
            raise _konto_back(req, "error", "daten")
        if area != "all" and area not in ACC.AREAS:
            raise _konto_back(req, "error", "daten")
        await ACC.delete_area(bot, uid, area)
        raise _konto_back(req, "area_deleted", "daten")

    raise web.HTTPNotFound()


def _konto_check_now(bot, uid, species, regions, excluded) -> None:
    """Sofort-Prüfung nach Anlegen/Fortsetzen (wie /notification), außer in der Ruhezeit."""
    cog = bot.cogs.get("Notifications") if hasattr(bot, "cogs") else None
    if not cog or ACC.is_quiet(uid):
        return
    ex = {s.strip().lower() for s in (excluded or "").split(",") if s.strip()}
    asyncio.create_task(cog.trigger_availability_check(uid, species, regions,
                                                       ch_mode=(regions == "ch"), excluded_species_list=ex))


async def _konto_notif_add(req, uid, form):
    """Neue Verfügbarkeits-Benachrichtigung, gleiche Regeln wie /notification."""
    from utils.availability import load_shop_data, expand_regions, species_exists
    from utils import species_catalog
    lang = pick_lang(req)
    bot = req.app["bot"]
    T = lambda key, **kw: translate(lang, key, **kw)
    kind = "genus" if form.get("kind") == "genus" else "species"
    name = " ".join((form.get("name") or "").split())[:80]
    exclude = (form.get("exclude") or "").strip()[:300]
    regs = [r for r in form.getall("region", []) if re.fullmatch(r"[a-z]{2}", r)]
    swiss = bool(form.get("swiss"))
    force = bool(form.get("force"))
    keep = {"kind": kind, "name": name, "exclude": exclude, "regions": regs, "swiss": swiss}

    def fail(msg):
        return _konto_page(req, uid, flash=_MD_STRIP.sub("", msg), form=keep, open_form="notif")

    if not name:
        return await fail(T("konto_err_name"))
    if kind == "species" and " " not in name:
        return await fail(T("konto_err_species_format"))
    if kind == "genus" and " " in name:
        name = name.split()[0]
    if not force:
        msg = species_catalog.validation_message(species_catalog.check(name), lang)
        if msg:
            return await fail(msg.replace("`force`", T("konto_force")))
    term = species_catalog.canonical(name) or name
    shop_data = await load_shop_data(bot)
    avail = {(sd.get("country") or "").lower() for sd in shop_data.values() if sd.get("country")}
    if swiss:
        valid = ["ch"]
    else:
        valid = sorted(r for r in await expand_regions(bot, regs) if r in avail)
        if not valid:
            return await fail(T("konto_err_regions"))
    if not force and not await species_exists(bot, term):
        return await fail(T("konto_err_unknown", term=term))
    excluded = exclude.lower() if (kind == "genus" and exclude) else None
    regions_str = ",".join(valid)
    await execute_db(bot,
        """INSERT INTO notifications (user_id, species, regions, status, excluded_species, server_id)
           VALUES (?, ?, ?, 'active', ?, ?)
           ON CONFLICT(user_id, species, regions)
           DO UPDATE SET created_at=CURRENT_TIMESTAMP, status='active',
               excluded_species=excluded.excluded_species, server_id=excluded.server_id""",
        (uid, term, regions_str, excluded, MAP_GUILD_ID or None), commit=True)
    if MAP_GUILD_ID:
        await execute_db(bot, "INSERT OR IGNORE INTO server_user_mappings (user_id, server_id) VALUES (?, ?)",
                         (uid, MAP_GUILD_ID), commit=True)
    _konto_check_now(bot, uid, term, regions_str, excluded)
    raise _konto_back(req, "notif_added", "benachrichtigungen")


async def h_konto_export(req):
    """Alle eigenen Daten als JSON (Art. 15/20 DSGVO)."""
    uid = _aam_member(req)
    if not uid:
        raise web.HTTPSeeOther(f"/konto?lang={pick_lang(req)}")
    if not _rate("kontoexp:" + _hmac("kontoexp", uid), 5, 300):
        raise web.HTTPTooManyRequests(text="rate limited")
    data = await ACC.export_user(req.app["bot"], uid)
    body = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    return web.Response(body=body, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": f"attachment; filename=aam-meine-daten-{uid}.json",
        "Cache-Control": "private, no-store"})


async def h_konto_ics(req):
    """Persönlicher Kalender: nur die eigenen Zusagen. Geheimer Link (neu erzeugen = alter ungültig)."""
    uid, salt = req.match_info["uid"], req.match_info["salt"]
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    cur = ACC.prefs(uid)["ics_salt"] or ""
    if not (uid.isdigit() and cur and hmac.compare_digest(cur, salt)):
        raise web.HTTPNotFound()
    since = (datetime.now(BERLIN) - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = await execute_db(req.app["bot"],
        "SELECT r.occ_date, e.* FROM map_event_rsvp r JOIN map_events e ON e.id=r.event_id "
        "WHERE r.user_id=? AND r.occ_date>=? AND e.status='approved' ORDER BY r.occ_date",
        (uid, since), fetch=True) or []
    occ_rows = []
    for r in rows:
        r = dict(r)
        st = _naive(r["start_at"])
        if st is None:
            continue
        try:
            day = datetime.strptime(r["occ_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        nst = datetime.combine(day, st.time())
        en = _naive(r["end_at"])
        r["start_at"] = nst.isoformat()
        r["end_at"] = (nst + (en - st)).isoformat() if en and en > st else None
        r["rrule"], r["exdates"] = None, None
        r["id"] = f"{r['id']}-{r['occ_date'].replace('-', '')}"
        occ_rows.append(r)
    lang = pick_lang(req)
    body = _ics_build(req, occ_rows, feed=True, name=translate(lang, "konto_ics_name"),
                      source=_ics_personal_url(uid, salt))
    return web.Response(body=body, headers={"Content-Type": "text/calendar; charset=utf-8",
                                            "Content-Disposition": "inline; filename=aam-meine-termine.ics",
                                            "Cache-Control": "private, max-age=900"})
