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
web/features/stats.py – Öffentliche Statistik-Seite (/stats).

Aus cogs/board.py herausgelöst (5.0.0); Logik unverändert.
"""

import logging
import asyncio
from utils.board_i18n import country_name, pick_lang, translate
from utils.currency import ensure_rates
from utils import shop_stats
from config import SHOPS_DATA_FILE, VERSION

from web.core.render import _render, STATIC_DIR
from utils.web_keys import BOT_KEY

logger = logging.getLogger("cogs.board")   # Name wie vor dem Umbau (Log-Filter/-Auswertung)


# Zuordnung Diagramm-Canvas -> i18n-Erklärungsschlüssel (für Hover-Tooltips, via stats.js).
_CHART_EXP = {
    "chCountries": "exp_countries", "chStock": "exp_stock",
    "chGenera": "exp_genera", "chReach": "exp_reach", "chLongtail": "exp_longtail",
    "chShopOffers": "exp_shop_offers", "chShopBreadth": "exp_shop_breadth",
    "chShopExclusive": "exp_shop_exclusive", "chShopScatter": "exp_shop_scatter",
    "chPriceHist": "exp_price_hist", "chPriceGenus": "exp_price_genus", "chPriceSpread": "exp_price_spread",
    "chPriceSpreadSmall": "exp_price_spread_small",
    "chAvGenus": "exp_av_genus", "chAvCountry": "exp_av_country", "chAvShopBest": "exp_av_shop_best",
    "chAvShopWorst": "exp_av_shop_worst", "chAvHardest": "exp_av_hardest",
    "chDqShopUncanon": "exp_dq_shop_uncanon", "chDqShopAdjusted": "exp_dq_shop_adjusted",
    "chDqVariants": "exp_dq_variants",
    "chTrPrice": "exp_tr_price", "chTrChanges": "exp_tr_changes", "chTrDrops": "exp_tr_drops",
    "chTrIncreases": "exp_tr_increases", "chTrAvail": "exp_tr_avail",
}


def _top10(pairs, other_label=None):
    """Aus [(label, wert), …] die Top 10 als (labels, values). Ist *other_label*
    gesetzt und gibt es einen Rest, wird dieser als eine „übrige"-Position summiert."""
    top = pairs[:10]
    labels = [k for k, _ in top]
    values = [v for _, v in top]
    if other_label is not None:
        rest = sum(v for _, v in pairs[10:])
        if rest:
            labels.append(other_label)
            values.append(rest)
    return labels, values


def _stats_l10n(lang: str, data: dict, ts: dict = None) -> dict:
    """Sprachabhängige Beschriftungen für die JS-Diagramme (die Rohzahlen in `data`
    sind sprachneutral). Wird als eigene JSON-Insel `STATS_L` an die Seite gegeben.
    Ranglisten: Top 10, Rest wo sinnvoll als „übrige" gruppiert."""
    ov = data["overview"]
    other = translate(lang, "lbl_other")

    # Block 1: Länder (lokalisierte Namen), Top 10 + übrige
    named = [(country_name(lang, iso), n) for iso, n in ov.get("countries", [])]
    c_labels, c_values = _top10(named, other)
    out = {
        "countries": {
            "title": translate(lang, "ch_countries_title"),
            "axis": translate(lang, "ch_countries_axis"),
            "labels": c_labels, "values": c_values,
        },
        "stock": {
            "title": translate(lang, "ch_stock_title"),
            "labels": [translate(lang, "lbl_instock"), translate(lang, "lbl_outstock")],
            "values": [ov.get("instock_live", 0), ov.get("out_of_stock_live", 0)],
        },
    }

    # ── Block 2: Arten & Gattungen ──────────────────────────────────────────
    sp = data.get("species")
    if sp:
        gtop = sp["genera"][:10]
        grest = sum(n for _, n in sp["genera"][10:])
        gdata = [{"g": g, "v": n} for g, n in gtop]
        if grest:
            gdata.append({"g": other, "v": grest})
        out["genera"] = {"title": translate(lang, "sp_genera_title"), "data": gdata}
        r_labels, r_values = _top10(sp["reach"])           # Arten: kein „übrige"
        out["reach"] = {"title": translate(lang, "sp_reach_title"),
                        "axis": translate(lang, "lbl_shops"),
                        "labels": r_labels, "values": r_values}
        out["longtail"] = {
            "title": translate(lang, "sp_longtail_title"),
            "x": translate(lang, "sp_longtail_x"),
            "y": translate(lang, "sp_longtail_y"),
            "labels": [str(k) for k, _ in sp["longtail"]],
            "values": [n for _, n in sp["longtail"]],
        }

    # ── Block 3: Shop-Vergleich ─────────────────────────────────────────────
    sh = data.get("shops")
    if sh:
        o_labels, o_values = _top10(sh["by_offers"], other)
        out["shop_offers"] = {"title": translate(lang, "sh_offers_title"),
                              "axis": translate(lang, "lbl_offers"),
                              "labels": o_labels, "values": o_values}
        b_labels, b_values = _top10(sh["by_breadth"])      # Breite: kein sinnvoller Summen-Rest
        out["shop_breadth"] = {"title": translate(lang, "sh_breadth_title"),
                               "axis": translate(lang, "lbl_species"),
                               "labels": b_labels, "values": b_values}
        e_labels, e_values = _top10(sh["by_exclusive"], other)
        out["shop_exclusive"] = {"title": translate(lang, "sh_exclusive_title"),
                                 "axis": translate(lang, "lbl_species"),
                                 "labels": e_labels, "values": e_values}
        out["shop_scatter"] = {
            "title": translate(lang, "sh_scatter_title"),
            "x": translate(lang, "sh_scatter_x"),
            "y": translate(lang, "sh_scatter_y"),
            "points": [{"x": p["species"], "y": p["offers"], "label": p["shop"]}
                       for p in sh["scatter"]],
        }

    # ── Block 4: Preise ─────────────────────────────────────────────────────
    pr = data.get("prices")
    if pr and pr.get("stats", {}).get("n"):
        out["price_hist"] = {"title": translate(lang, "pr_hist_title"),
                             "x": translate(lang, "pr_hist_x"),
                             "y": translate(lang, "pr_hist_y"),
                             "labels": pr["hist"]["labels"], "values": pr["hist"]["counts"]}
        out["price_genus"] = {"title": translate(lang, "pr_genus_title"),
                              "axis": translate(lang, "pr_genus_axis"),
                              "labels": [g for g, _ in pr["genus_median"]],
                              "values": [v for _, v in pr["genus_median"]]}
        _shops_lbl = translate(lang, "lbl_shops")
        out["price_spread"] = {"title": translate(lang, "pr_spread_title"),
                               "axis": translate(lang, "pr_spread_axis"),
                               "labels": [f"{s[0]} ({s[4]} {_shops_lbl})" for s in pr["spread"]],
                               "ranges": [[s[1], s[2]] for s in pr["spread"]]}
        out["price_spread_small"] = {"title": translate(lang, "pr_spread_small_title"),
                                     "axis": translate(lang, "pr_spread_axis"),
                                     "labels": [f"{s[0]} ({s[4]} {_shops_lbl})" for s in pr.get("spread_small", [])],
                                     "ranges": [[s[1], s[2]] for s in pr.get("spread_small", [])]}

    # ── Block 5: Verfügbarkeit ──────────────────────────────────────────────
    av = data.get("availability")
    if av:
        rate_axis = translate(lang, "lbl_instock_rate")
        out["av_genus"] = {"title": translate(lang, "av_genus_title"), "axis": rate_axis,
                           "labels": [g for g, _ in av["by_genus"]],
                           "values": [r for _, r in av["by_genus"]]}
        out["av_country"] = {"title": translate(lang, "av_country_title"), "axis": rate_axis,
                             "labels": [country_name(lang, iso) for iso, _, _ in av["by_country"]],
                             "values": [r for _, r, _ in av["by_country"]]}
        out["av_shop_best"] = {"title": translate(lang, "av_shop_best_title"), "axis": rate_axis,
                               "labels": [s for s, _, _ in av["shop_best"]],
                               "values": [r for _, r, _ in av["shop_best"]]}
        out["av_shop_worst"] = {"title": translate(lang, "av_shop_worst_title"), "axis": rate_axis,
                                "labels": [s for s, _, _ in av["shop_worst"]],
                                "values": [r for _, r, _ in av["shop_worst"]]}
        _shops_lbl2 = translate(lang, "lbl_shops")
        out["av_hardest"] = {"title": translate(lang, "av_hardest_title"), "axis": rate_axis,
                             "labels": [f"{s} ({sh} {_shops_lbl2})" for s, r, sh, of in av["hardest"]],
                             "values": [r for s, r, sh, of in av["hardest"]]}

    # ── Block 6: Datenqualität ──────────────────────────────────────────────
    q = data.get("quality")
    if q:
        out["dq_shop_uncanon"] = {"title": translate(lang, "dq_shop_uncanon_title"),
                                  "axis": translate(lang, "dq_shop_uncanon_axis"),
                                  "labels": [s for s, _ in q["shop_uncanon"]],
                                  "values": [n for _, n in q["shop_uncanon"]]}
        out["dq_shop_adjusted"] = {"title": translate(lang, "dq_shop_adjusted_title"),
                                   "axis": translate(lang, "dq_shop_adjusted_axis"),
                                   "labels": [f"{s} ({ac})" for s, r, ac, cn in q["shop_adjusted"]],
                                   "values": [r for s, r, ac, cn in q["shop_adjusted"]]}
        out["dq_variants"] = {"title": translate(lang, "dq_variants_title"),
                              "axis": translate(lang, "dq_variants_axis"),
                              "labels": [s for s, _ in q["variants"]],
                              "values": [n for _, n in q["variants"]]}

    # ── Block 7: Zeitverläufe ───────────────────────────────────────────────
    if ts and ts.get("available"):
        pot = ts.get("price_over_time", [])
        out["tr_price"] = {"title": translate(lang, "tr_price_title"),
                           "note": translate(lang, "tr_price_note"),
                           "axis": translate(lang, "tr_price_axis"),
                           "x": translate(lang, "tr_month_axis"),
                           "labels": [m for m, _, _ in pot], "values": [v for _, v, _ in pot]}
        ch = ts.get("changes_per_month", [])
        out["tr_changes"] = {"title": translate(lang, "tr_changes_title"),
                             "x": translate(lang, "tr_month_axis"),
                             "y": translate(lang, "tr_count_axis"),
                             "down_label": translate(lang, "tr_changes_down"),
                             "up_label": translate(lang, "tr_changes_up"),
                             "labels": [m for m, _, _ in ch],
                             "down": [d for _, d, _ in ch], "up": [u for _, _, u in ch]}

        def _dr(items):
            return {"labels": [i[0] for i in items], "values": [i[3] for i in items],
                    "info": [f"{i[1]} € → {i[2]} €" for i in items]}
        out["tr_drops"] = {"title": translate(lang, "tr_drops_title"),
                           "axis": translate(lang, "tr_pct_change"), **_dr(ts.get("price_drops", []))}
        out["tr_increases"] = {"title": translate(lang, "tr_increases_title"),
                               "axis": translate(lang, "tr_pct_change"), **_dr(ts.get("price_increases", []))}
        av = ts.get("avail_over_time") or []
        out["tr_avail"] = {"title": translate(lang, "tr_avail_title"),
                           "axis": translate(lang, "lbl_instock_rate"),
                           "x": translate(lang, "tr_month_axis"),
                           "labels": [m for m, _, _ in av], "values": [r for _, r, _ in av],
                           "empty": not ts.get("has_stock")}

    # Hover-Erklärungen je Diagramm (Canvas-ID -> Text); stats.js hängt das ⓘ an die Überschrift.
    out["exp"] = {cid: translate(lang, key) for cid, key in _CHART_EXP.items()}
    return out


def _stats_asset_v() -> str:
    """Cache-Busting für stats.js: Bot-Version + Änderungszeit (sonst bis zu 24 h alter Stand)."""
    try:
        return f"{VERSION}.{int((STATIC_DIR / 'stats.js').stat().st_mtime)}"
    except OSError:
        return VERSION


async def h_stats(req):
    """Öffentliche Statistik-Seite. Aggregiert live aus shops_data.json (15-min-Cache).
    Währungskurse werden zuvor sichergestellt (für die späteren EUR-Preisblöcke)."""
    lang = pick_lang(req)
    range_key = req.query.get("range", "12")
    if range_key not in shop_stats.RANGE_MONTHS:
        range_key = "12"
    data = l10n = ts = None
    try:
        await ensure_rates()                                   # EZB/Frankfurter + Fallback
        data = await asyncio.to_thread(shop_stats.compute)     # Datei-I/O + CPU im Thread
        ts = await asyncio.to_thread(shop_stats.compute_timeseries, range_key)
    except FileNotFoundError:
        logger.warning("📊 Stats: shops_data.json nicht gefunden (%s)", SHOPS_DATA_FILE)
    except Exception as e:
        logger.warning("📊 Stats-Aggregation fehlgeschlagen: %s", e, exc_info=True)
    if data:
        l10n = _stats_l10n(lang, data, ts)
    usage = None
    try:
        from utils import usage_stats
        usage = await usage_stats.collect(req.app[BOT_KEY])
    except Exception as e:
        logger.warning("📈 Nutzungsstatistik nicht verfügbar: %s", e)
    resp = _render(req, "stats", title=translate(lang, "nav_stats"), data=data, l10n=l10n, usage=usage,
                   ts_available=bool(ts and ts.get("available")), ts_range=range_key, ver=_stats_asset_v())
    resp.headers["Cache-Control"] = "no-store"
    return resp
