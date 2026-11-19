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
utils/species_card.py – teilbare Artenkarte als PNG (Board-Download und /art-Button).

Zeigt Artname, Gattung, günstigsten Preis heute / vor 3 / vor 12 Monaten, Anzahl Shops
und lagernder Angebote, einen kleinen Preisverlauf, optional die Saison-Monate und die
Board-Adresse. Rendert lokal mit matplotlib (blockierend → per asyncio.to_thread aufrufen).
"""
from __future__ import annotations

import io

TXT = {
    "de": {"now": "heute ab", "m3": "vor 3 Monaten", "m12": "vor 12 Monaten", "shops": "Shops",
           "instock": "lagernde Angebote", "rare": "nur bei einem Shop", "trend": "Günstigster Preis je Monat",
           "season": "Meist lagernd", "none": "derzeit nicht lagernd",
           "noseason": "Saison-Verfügbarkeit: Daten werden noch gesammelt"},
    "en": {"now": "today from", "m3": "3 months ago", "m12": "12 months ago", "shops": "shops",
           "instock": "offers in stock", "rare": "only at one shop", "trend": "Lowest price per month",
           "season": "Mostly in stock", "none": "currently not in stock",
           "noseason": "Seasonal availability: data is still being collected"},
    "eo": {"now": "hodiaŭ ekde", "m3": "antaŭ 3 monatoj", "m12": "antaŭ 12 monatoj", "shops": "vendejoj",
           "instock": "haveblaj ofertoj", "rare": "nur ĉe unu vendejo", "trend": "Plej malalta prezo monate",
           "season": "Plejofte havebla", "none": "nun ne havebla",
           "noseason": "Sezona havebleco: datumoj ankoraŭ kolektataj"},
}


def _eur(v, lang: str) -> str:
    if v is None:
        return "–"
    s = f"{v:.2f} €"
    return s.replace(".", ",") if lang == "de" else s


def render(sp: dict, trend: dict, lang: str = "de", season: dict | None = None,
           months_short: list | None = None, url: str = "") -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = TXT.get(lang, TXT["de"])
    BG, FG, MUTED, GREEN, CARD = "#0d1117", "#e6edf3", "#8b949e", "#3fb950", "#161b22"
    instock = [o for o in sp.get("offers", []) if o.get("in_stock")]
    shops = len(sp.get("shops", []))

    fig = plt.figure(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor(BG)
    fig.text(0.04, 0.88, sp["name"], color=FG, fontsize=24, fontweight="bold", fontstyle="italic")
    fig.text(0.04, 0.81, f"{sp.get('genus', '')}  ·  {shops} {t['shops']}  ·  {len(instock)} {t['instock']}"
             + (f"  ·  {t['rare']}" if shops == 1 else ""), color=MUTED, fontsize=11)

    # Kennzahlen
    kpis = [(t["now"], trend.get("now")), (t["m3"], trend.get("m3")), (t["m12"], trend.get("m12"))]
    for i, (lab, val) in enumerate(kpis):
        x = 0.04 + i * 0.155
        fig.patches.append(matplotlib.patches.FancyBboxPatch(
            (x, 0.56), 0.14, 0.17, boxstyle="round,pad=0.004,rounding_size=0.01",
            transform=fig.transFigure, facecolor=CARD, edgecolor="#30363d"))
        fig.text(x + 0.012, 0.645, _eur(val, lang) if val is not None or i else t["none"],
                 color=GREEN if i == 0 else FG, fontsize=14 if val is not None else 9, fontweight="bold")
        fig.text(x + 0.012, 0.585, lab, color=MUTED, fontsize=8.5)

    # Preisverlauf
    vals = trend.get("values") or []
    if len([v for v in vals if v]) > 1:
        ax = fig.add_axes([0.53, 0.53, 0.43, 0.24])
        ax.set_facecolor(BG)
        xs = list(range(len(vals)))
        ax.step(xs, [v if v else float("nan") for v in vals], where="post", color=GREEN, linewidth=2)
        ax.set_xticks([xs[0], xs[-1]])
        ax.set_xticklabels([trend["labels"][0], trend["labels"][-1]], color=MUTED, fontsize=7)
        ax.tick_params(axis="y", colors=MUTED, labelsize=7)
        for s in ax.spines.values():
            s.set_color("#30363d")
        ax.set_title(t["trend"], color=MUTED, fontsize=8, loc="left")

    # Saison
    if season and months_short:
        ax2 = fig.add_axes([0.04, 0.16, 0.92, 0.22])
        ax2.set_facecolor(BG)
        ms = season.get("months") or [None] * 12
        heights = [0.05 if m is None else 0.05 + m for m in ms]
        colors = ["#21262d" if m is None else (GREEN if m >= 0.5 else "#3fb95055") for m in ms]
        ax2.bar(range(12), heights, color=colors, width=0.8)
        ax2.set_xticks(range(12))
        ax2.set_xticklabels(months_short, color=MUTED, fontsize=8)
        ax2.set_yticks([])
        ax2.set_ylim(0, 1.1)
        for s in ax2.spines.values():
            s.set_visible(False)
        ax2.set_title(t["season"], color=MUTED, fontsize=9, loc="left")

    else:
        fig.text(0.04, 0.3, t["noseason"], color=MUTED, fontsize=10)

    if url:
        fig.text(0.96, 0.04, url, color=MUTED, fontsize=8, ha="right")
    fig.text(0.04, 0.04, "AAM-Bot · antcheck.info", color=MUTED, fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()
