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
web/features/map.py – Halter-Karte: Seite, Regionen, Pins, Liste, Statistik.

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

from aiohttp import web
from utils import geo, map_tags
from datetime import datetime, timezone
from utils.db import execute_db
from utils.board_i18n import country_name, pick_lang, translate
from config import DISCORD_INVITE_URL, MAP_ENABLED, MAP_MAX_ZOOM, VERSION

from web.core.partners import _load_partners
from web.core.render import _render, STATIC_DIR
from web.core.security import (
    _aam_member, _contact_url, _hmac, _map_rate_ok, _member_csrf, _member_name,
)
from web.features.events import _approved_events, _ics_calendar_url, _occ_date, _rsvp_info
from utils.web_keys import BOT_KEY


_DACH = ("de", "at", "ch", "li")


def _map_lang_redirect(path: str, lang: str) -> web.Response:
    # Redirect-Ziel aus internen Literalen + Whitelist-Sprache (CodeQL url-redirection safe)
    return web.HTTPFound(f"{path}?lang={lang}")


def _map_asset_v() -> str:
    """Cache-Busting für map.js/leaflet.js: Bot-Version + letzte Änderungszeit der Dateien.
    So holt der Browser nach einem Update sofort die neue Datei (statt bis zu 24 h Cache)."""
    try:
        m = max(int((STATIC_DIR / f).stat().st_mtime) for f in ("map.js", "map_stats.js", "leaflet.js", "map_cities.json")
                if (STATIC_DIR / f).is_file())
    except ValueError:
        m = 0
    return f"{VERSION}.{m}"


# Texte für die Statistik-Box (map_stats.js), Platzhalter {n}/{km}/… füllt das Front-End.
_MAP_ST_KEYS = ('map_st_title', 'map_st_loading', 'map_st_error', 'map_st_privacy', 'map_st_keepers', 'map_st_countries', 'map_st_regions', 'map_st_events', 'map_st_intl', 'map_st_top', 'map_st_growth', 'map_st_growth_note', 'map_st_ctry', 'map_st_evtypes', 'map_st_next', 'map_st_none', 'map_st_login', 'map_st_members', 'map_st_tags', 'map_st_tags_note', 'map_st_near', 'map_st_near_note', 'map_st_near_none', 'map_st_within', 'map_st_band', 'map_st_contact', 'map_st_contact_all', 'map_st_contact_reg', 'map_st_topev', 'map_st_myev', 'map_st_myev_none', 'map_st_empty', 'map_st_empty_note', 'map_st_empty_none')


async def h_map(req):
    lang = pick_lang(req)
    if not MAP_ENABLED:
        return _render(req, "map", title=translate(lang, "map_h"),
                       flash=translate(lang, "map_disabled"),
                       v=_map_asset_v(), member=False, max_zoom=MAP_MAX_ZOOM)
    ics_url = _ics_calendar_url(req)
    webcal_url = "webcal://" + ics_url.split("://", 1)[-1]
    tag_groups = [{"label": (g["label"].get(lang) or g["label"].get("de")),
                   "tags": [(c, (l.get(lang) or l.get("de") or c)) for c, l in g["tags"]]}
                  for g in map_tags.TAG_GROUPS]
    return _render(req, "map", title=translate(lang, "map_h"),
                   v=_map_asset_v(), member=bool(_aam_member(req)), max_zoom=MAP_MAX_ZOOM,
                   ics_url=ics_url, webcal_url=webcal_url, tag_groups=tag_groups,
                   st_labels={k: translate(lang, k) for k in _MAP_ST_KEYS},
                   partners=_load_partners(),
                   invite_url=(DISCORD_INVITE_URL if DISCORD_INVITE_URL.startswith("https://") else ""),
                   member_csrf=(_member_csrf(_aam_member(req)) if _aam_member(req) else ""))


async def _map_extras_for(app, uids=None):
    """„Auch aktiv in“-Regionen: {user_id: [(Land, Regionscode, Name), …]} (nur Mitglieder-Ansichten)."""
    rows = await execute_db(app[BOT_KEY],
        "SELECT user_id, country, region_code FROM map_entry_regions", fetch=True) or []
    allr = geo.all_regions()
    out: dict = {}
    for r in rows:
        if uids is not None and r["user_id"] not in uids:
            continue
        name = allr.get(r["country"], {}).get(r["region_code"])
        if name:
            out.setdefault(r["user_id"], []).append((r["country"], r["region_code"], name))
    return out


async def _map_tags_for(app, uids):
    """tag_codes je user_id (dict uid->list)."""
    if not uids:
        return {}
    rows = await execute_db(app[BOT_KEY],
        "SELECT user_id, tag_code FROM map_entry_tags", fetch=True) or []
    out: dict = {}
    for r in rows:
        if r["user_id"] in uids:
            out.setdefault(r["user_id"], []).append(r["tag_code"])
    return out


# Datenschutz: Öffentlich (ohne Login) werden kleine Zahlen nicht exakt gezeigt, damit man
# in dünn besiedelten Gegenden niemanden zurückverfolgen kann. 1–2 -> "< 3".
MAP_PUBLIC_MIN = 3


def _mask_entry(e: dict) -> dict:
    """Öffentliche Ausgabe: count < MAP_PUBLIC_MIN -> count=1 + few=True (gleiche Farbe/Größe)."""
    if 0 < e.get("count", 0) < MAP_PUBLIC_MIN:
        e["count"], e["few"] = 1, True
    return e


def _mask_num(n: int, member: bool):
    """Zahl für die Anzeige: Mitglieder exakt, öffentlich 1–2 als "< 3"."""
    return n if member or n == 0 or n >= MAP_PUBLIC_MIN else f"< {MAP_PUBLIC_MIN}"


async def h_map_regions(req):
    """ÖFFENTLICH: nur aggregierte Zahlen (keine Identitäten)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app[BOT_KEY]
    # ALLE Opt-in-Einträge zählen (auch U18 anonym) – die Zahlen sind aggregiert und
    # enthalten keine Identitäten. Einzel-Pins/-Liste bleiben getrennt auf show_entry=1.
    rows = await execute_db(bot,
        "SELECT country, region_code, region_name, plz_prefix FROM map_entries",
        fetch=True) or []
    by_state, by_plz, by_country = {}, {}, {}
    for r in rows:
        c = r["country"]
        by_country[c] = by_country.get(c, 0) + 1
        if c in _DACH and r["region_code"]:
            # Codes vereinheitlichen (ISO wie in den Umrissen; auch für ältere Einträge)
            rc, rn = geo.canon_region(c, r["region_code"], r["region_name"] or "")
            if c == "li":
                rn = "Liechtenstein"
            k = f'{c}:{rc}'
            e = by_state.setdefault(k, {"country": c, "region_code": rc,
                                        "region_name": rn, "count": 0})
            e["count"] += 1
        if c in _DACH and r["plz_prefix"]:
            k = f'{c}:{r["plz_prefix"]}'
            e = by_plz.setdefault(k, {"country": c, "plz_prefix": r["plz_prefix"], "count": 0})
            e["count"] += 1
    # PLZ-Ebene als Blasen: Zentroid je PLZ-Gebiet aus dem GeoNames-Datensatz anhängen
    # (keine PLZ-Polygon-Datei nötig). Einträge ohne Zentroid werden vorne weggelassen.
    cents = geo.plz_prefix_centroids()
    plz_out = []
    for e in by_plz.values():
        ll = cents.get((e["country"], e["plz_prefix"]))
        if ll:
            e["lat"], e["lon"] = ll
            plz_out.append(e)
    member = bool(_aam_member(req))
    if member:
        # „Auch aktiv in“ (nur Mitglieder): zusätzlich aktive Halter je Region. Zählt NICHT
        # in count/Färbung (Wohnort bleibt maßgeblich, keine Doppelzählung).
        vis = {r["user_id"] for r in (await execute_db(bot,
            "SELECT user_id FROM map_entries WHERE show_entry=1", fetch=True) or [])}
        for uid, lst in (await _map_extras_for(req.app, vis)).items():
            if not _member_name(req.app, uid):
                continue
            for c, rc, rn in lst:
                e = by_state.setdefault(f"{c}:{rc}", {"country": c, "region_code": rc,
                                                      "region_name": rn, "count": 0})
                e["also"] = e.get("also", 0) + 1
    states = list(by_state.values())
    countries = [{"country": k, "count": v} for k, v in sorted(by_country.items())]
    if not member:                                 # öffentlich: kleine Zahlen maskieren
        for e in states + plz_out + countries:
            _mask_entry(e)
    return web.json_response({
        "bundesland": states,
        "plz": plz_out,
        "countries": countries,
    }, headers={"Cache-Control": "no-store", "Vary": "Cookie"})


async def h_map_pins(req):
    """NUR eingeloggte Mitglieder: gefuzzte DACH-Pins (kleine Menge)."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    if not _aam_member(req):
        raise web.HTTPForbidden(text="login required")
    bot = req.app[BOT_KEY]
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, first_name, show_name, contact_ok, coarse, "
        "lat_fuzzed, lon_fuzzed FROM map_entries WHERE show_entry=1 "
        "AND lat_fuzzed IS NOT NULL LIMIT 2000", fetch=True) or []
    tags = await _map_tags_for(req.app, {r["user_id"] for r in rows})
    extras = await _map_extras_for(req.app, {r["user_id"] for r in rows})
    lang = pick_lang(req)
    anon = translate(lang, "map_anon")
    out = []
    for r in rows:
        name = _member_name(req.app, r["user_id"])
        if not name:                                 # kein Mitglied mehr -> auslassen
            continue
        if r["show_name"]:
            disp = f'{r["first_name"]} ({name})' if r["first_name"] else name
        else:
            disp = anon                              # Name ausgeblendet (nur anonym)
        out.append({
            "ref": _hmac("mapref", r["user_id"])[:12],   # opak, korreliert Pin ↔ Listenzeile
            "lat": r["lat_fuzzed"], "lon": r["lon_fuzzed"], "name": disp,
            "country": r["country"],
            "region": geo.canon_region(r["country"], r["region_code"] or "", r["region_name"] or "")[1],
            "contact": bool(r["contact_ok"]),
            # Profil-Link NUR bei aktivem Opt-in "Kontakt über Discord" (nur für eingeloggte Mitglieder).
            "contact_url": _contact_url(r["user_id"]) if r["contact_ok"] else "",
            "coarse": bool(r["coarse"]),             # Pin = Mitte des groben PLZ-Gebiets
            "also": [n for _c, _k, n in extras.get(r["user_id"], [])],   # „Auch aktiv in“
            "tags": map_tags.labels(tags.get(r["user_id"], []), lang),
            "tag_codes": [c for c in tags.get(r["user_id"], []) if map_tags.is_valid(c)],
        })
    return web.json_response({"pins": out})


async def h_map_list(req):
    """Mitglieder: Einzel-Einträge (DACH + International). Öffentlich: nur Länder-Zahlen."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app[BOT_KEY]
    member = bool(_aam_member(req))
    if not member:
        # Öffentlich: nur Länder-Zahlen (alle Opt-in-Einträge inkl. U18 anonym).
        rows = await execute_db(bot,
            "SELECT country, COUNT(*) AS n FROM map_entries GROUP BY country",
            fetch=True) or []
        return web.json_response({"member": False,
            "counts": [{"country": r["country"],
                        "country_name": country_name(pick_lang(req), r["country"]),
                        "count": _mask_num(r["n"], False)} for r in rows]},
            headers={"Cache-Control": "no-store", "Vary": "Cookie"})
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, first_name, show_name, contact_ok "
        "FROM map_entries WHERE show_entry=1 LIMIT 5000", fetch=True) or []
    tags = await _map_tags_for(req.app, {r["user_id"] for r in rows})
    extras = await _map_extras_for(req.app, {r["user_id"] for r in rows})
    lang = pick_lang(req)
    anon = translate(lang, "map_anon")
    items = []
    for r in rows:
        name = _member_name(req.app, r["user_id"])
        if not name:
            continue
        if r["show_name"]:
            disp = f'{r["first_name"]} ({name})' if r["first_name"] else name
        else:
            disp = anon
        items.append({
            "ref": _hmac("mapref", r["user_id"])[:12],   # opak, korreliert Listenzeile ↔ Pin
            "name": disp, "country": r["country"],
            "country_name": country_name(lang, r["country"]),
            "region": geo.canon_region(r["country"], r["region_code"] or "", r["region_name"] or "")[1],
            "dach": r["country"] in _DACH,
            "also": [n for _c, _k, n in extras.get(r["user_id"], [])],   # „Auch aktiv in“
            "contact": bool(r["contact_ok"]),
            # Profil-Link NUR bei aktivem Opt-in "Kontakt über Discord" (nur für eingeloggte Mitglieder).
            "contact_url": _contact_url(r["user_id"]) if r["contact_ok"] else "",
            "tags": map_tags.labels(tags.get(r["user_id"], []), lang),
            "tag_codes": [c for c in tags.get(r["user_id"], []) if map_tags.is_valid(c)],
        })
    items.sort(key=lambda x: (x["country_name"], x["name"].lower()))
    return web.json_response({"member": True, "items": items})


def _km(lat1, lon1, lat2, lon2) -> float:
    """Luftlinie in km (Haversine)."""
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


async def h_map_stats(req):
    """Statistiken zur Karte. ÖFFENTLICH: nur Summen (kleine Zahlen maskiert).
    MITGLIEDER zusätzlich: Tags, Umkreis, Kontakt, Teilnahme, Regionen ohne Halter.
    Unter-18-Einträge (show_entry=0) zählen nur in den Gesamtsummen mit."""
    if not _map_rate_ok(req):
        raise web.HTTPTooManyRequests(text="rate limited")
    bot = req.app[BOT_KEY]
    lang = pick_lang(req)
    me = _aam_member(req)
    member = bool(me)
    rows = await execute_db(bot,
        "SELECT user_id, country, region_code, region_name, lat_fuzzed, lon_fuzzed, "
        "show_entry, contact_ok, consent_at FROM map_entries", fetch=True) or []

    # ── Kennzahlen ──
    by_country: dict = {}
    by_state: dict = {}
    for r in rows:
        c = r["country"] or "??"
        by_country[c] = by_country.get(c, 0) + 1
        if c in _DACH and r["region_code"]:
            rc, rn = geo.canon_region(c, r["region_code"], r["region_name"] or "")
            if c == "li":
                rc, rn = "LI", "Liechtenstein"
            k = (c, rc)
            e = by_state.setdefault(k, {"country": c, "code": rc, "name": rn, "count": 0})
            e["count"] += 1
    evs = await _approved_events(req.app)
    total = len(rows)
    out = {
        "member": member,
        "min": MAP_PUBLIC_MIN,
        "totals": {"keepers": _mask_num(total, member),
                   "countries": len([c for c in by_country if c != "??"]),
                   "regions": len(by_state), "events": len(evs)},
    }

    # ── Top-Regionen (5) ──
    top = sorted(by_state.values(), key=lambda e: (-e["count"], e["name"]))[:5]
    out["top_regions"] = [{"name": e["name"], "country": e["country"],
                           "count": _mask_num(e["count"], member),
                           "n": e["count"] if (member or e["count"] >= MAP_PUBLIC_MIN) else 0}
                          for e in top]

    # ── Wachstum: aktuelle Einträge nach Eintragsmonat (letzte 12 Monate) ──
    now = datetime.now(timezone.utc)
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    per = {k: 0 for k in months}
    for r in rows:
        k = str(r["consent_at"] or "")[:7]
        if k in per:
            per[k] += 1
    out["growth"] = [{"month": k, "count": per[k]} for k in months]

    # ── Ländervergleich DACH (+ International) ──
    comp = []
    for c in _DACH:
        n = by_country.get(c, 0)
        show = member or n == 0 or n >= MAP_PUBLIC_MIN
        comp.append({"country": c, "name": country_name(lang, c),
                     "count": _mask_num(n, member),
                     "share": round(100 * n / total) if (total and show) else None})
    intl = sum(v for k, v in by_country.items() if k not in _DACH)
    show = member or intl == 0 or intl >= MAP_PUBLIC_MIN
    comp.append({"country": "intl", "name": "International",
                 "count": _mask_num(intl, member),
                 "share": round(100 * intl / total) if (total and show) else None})
    out["countries"] = comp

    # ── Termine: kommende je Art + nächster Termin ──
    by_type: dict = {}
    for _n, r in evs:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    out["events_by_type"] = [{"type": k, "count": v}
                             for k, v in sorted(by_type.items(), key=lambda t: -t[1])]
    if evs:
        nxt, r = evs[0]
        out["next_event"] = {"id": r["id"], "title": r["title"], "type": r["type"],
                             "next": nxt.isoformat(), "all_day": bool(r["all_day"]),
                             "venue": r["venue"] or ""}

    if not member:
        return web.json_response(out, headers={"Cache-Control": "no-store", "Vary": "Cookie"})

    # ════ Ab hier nur Mitglieder ════
    # Sichtbare Einträge (18+, show_entry=1) von aktuellen Server-Mitgliedern.
    vis = [r for r in rows if r["show_entry"] and _member_name(req.app, r["user_id"])]
    nvis = len(vis)
    out["visible"] = nvis

    # Beliebteste Tags (Anteil der sichtbaren Einträge)
    tagrows = await execute_db(bot, "SELECT user_id, tag_code FROM map_entry_tags",
                               fetch=True) or []
    vis_ids = {r["user_id"] for r in vis}
    tcount: dict = {}
    for t in tagrows:
        if t["user_id"] in vis_ids and map_tags.is_valid(t["tag_code"]):
            tcount[t["tag_code"]] = tcount.get(t["tag_code"], 0) + 1
    out["tags"] = [{"code": c, "label": map_tags.label(c, lang), "count": n,
                    "pct": round(100 * n / nvis) if nvis else 0}
                   for c, n in sorted(tcount.items(), key=lambda t: -t[1])[:10]]

    # Kontaktbereitschaft (gesamt + eigene Region)
    own = next((r for r in rows if str(r["user_id"]) == str(me)), None)
    contact_total = sum(1 for r in vis if r["contact_ok"])
    out["contact"] = {"count": contact_total,
                      "pct": round(100 * contact_total / nvis) if nvis else 0}
    if own and own["country"] in _DACH and own["region_code"]:
        orc, orn = geo.canon_region(own["country"], own["region_code"], own["region_name"] or "")
        same = [r for r in vis if r["country"] == own["country"] and str(r["user_id"]) != str(me)
                and geo.canon_region(r["country"], r["region_code"] or "", "")[0] == orc]
        out["contact"]["region"] = {"name": "Liechtenstein" if own["country"] == "li" else orn,
                                    "keepers": len(same),
                                    "count": sum(1 for r in same if r["contact_ok"])}

    # In deiner Nähe (Luftlinie zwischen den verschobenen Pins, ohne dich selbst).
    # Entfernungsringe statt kumuliert: jeder Halter zählt genau einmal (0–25, 25–50, 50–100 km).
    if own and own["lat_fuzzed"] is not None and own["lon_fuzzed"] is not None:
        bands = [(0, 25), (25, 50), (50, 100)]
        near = [0] * len(bands)
        for r in vis:
            if str(r["user_id"]) == str(me) or r["lat_fuzzed"] is None:
                continue
            d = _km(own["lat_fuzzed"], own["lon_fuzzed"], r["lat_fuzzed"], r["lon_fuzzed"])
            for i, (a, b) in enumerate(bands):
                if (d > a or a == 0) and d <= b:
                    near[i] += 1
                    break
        out["nearby"] = [{"from": a, "to": b, "count": n} for (a, b), n in zip(bands, near)]
    else:
        out["nearby"] = None                        # kein eigener Eintrag -> Hinweis /map_join

    # Teilnahme: beliebteste kommende Termine + eigene Zusagen
    rsvp = await _rsvp_info(req.app, [(r["id"], _occ_date(n)) for n, r in evs], me)
    ranked = []
    mine = []
    for n, r in evs:
        info = rsvp[(r["id"], _occ_date(n))]
        if info["count"]:
            ranked.append({"id": r["id"], "title": r["title"], "next": n.isoformat(),
                           "all_day": bool(r["all_day"]), "count": info["count"]})
        if info["going"]:
            mine.append({"id": r["id"], "title": r["title"], "next": n.isoformat(),
                         "all_day": bool(r["all_day"])})
    ranked.sort(key=lambda e: (-e["count"], e["next"]))
    out["top_events"] = ranked[:5]
    out["my_events"] = mine

    # Weiße Flecken: DACH-Regionen ohne Halter
    have = set(by_state.keys())
    empty = []
    for c, table in geo.all_regions().items():
        for code, name in table.items():
            if (c, code) not in have:
                empty.append({"country": c, "name": name})
    out["empty_regions"] = sorted(empty, key=lambda e: (_DACH.index(e["country"]), e["name"]))
    return web.json_response(out, headers={"Cache-Control": "no-store", "Vary": "Cookie"})
