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
utils/board_swarm.py – Board-Seite /schwarmflug (Template + Daten) und der Kasten auf /art/<Art>.

Öffentlich: Zahlen je Region, Saisonverlauf, Steckbriefe, letzte Meldungen ohne Ort.
Eingeloggte AAM-Mitglieder: zusätzlich genaue Punkte, Ortstext und Link zur Discord-Nachricht.
Namen der Meldenden werden nirgends angezeigt.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from utils import swarm as S
from utils.db import execute_db
from utils.timez import BERLIN

RANGES = ("h48", "d7", "saison", "alle")
# Okabe-Ito (farbsehschwächen-tauglich), wie auf der Halter-Karte
PALETTE = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#CC79A7", "#D55E00", "#0072B2"]

TEMPLATE = """{% extends "base" %}{% block body %}
<link rel="stylesheet" href="/static/leaflet.css?v={{ v }}">
<style>
 .swgrid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:14px;align-items:start}
 .swgrid>*{min-width:0} @media(max-width:860px){.swgrid{grid-template-columns:1fr}}
 @media(max-width:700px){ #swmap{height:420px !important}}
 #swmap{height:520px;border-radius:10px;border:1px solid #21262d;background:#0f141a}
 .leaflet-container{background:#0f141a}
 .leaflet-bar a,.leaflet-bar a:hover{background:#161b22;color:#e6edf3;border-bottom-color:#30363d}
 .leaflet-control-attribution{background:#161b22cc !important;color:#8b949e} .leaflet-control-attribution a{color:#58a6ff}
 .leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#161b22;color:#e6edf3;border:1px solid #30363d}
 .leaflet-popup-content a{color:#58a6ff} a.leaflet-popup-close-button{color:#8b949e}
 .now{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px}
 .now span{background:#3fb95018;border:1px solid #3fb95055;border-radius:20px;padding:3px 10px;font-size:13px}
 .now span i{color:#e6edf3} .now b{color:#3fb950}
 .swlegend{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:#8b949e;margin-top:6px;align-items:center}
 .swlegend i{display:inline-block;width:14px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}
 .swt td,.swt th{font-size:13px;padding:5px 6px} .swt th{color:#8b949e;font-weight:600;font-size:12px}
 .bars{display:grid;grid-template-columns:repeat(12,1fr);gap:2px;align-items:end;height:22px;min-width:90px}
 .bars div{background:#3fb950;border-radius:2px;min-height:2px}
 .feed{max-height:520px;overflow:auto;scrollbar-width:thin;scrollbar-color:#30363d transparent}
 .feed .it{padding:7px 4px;border-bottom:1px solid #21262d;font-size:13px}
 .feed .it .w{color:#8b949e;font-size:12px}
 .mlock{background:#1f6feb14;border:1px dashed #1f6feb88;border-radius:8px;padding:8px 10px;font-size:13px;margin-top:8px}
 .twobox{display:grid;grid-template-columns:1fr 1fr;gap:12px} @media(max-width:700px){.twobox{grid-template-columns:1fr}}
 select.gsel{width:auto;padding:4px 8px;font-size:13px}
 .tl{display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap}
 .tl input[type=range]{flex:1;min-width:160px;padding:0;accent-color:#3fb950}
 .tl .lbl{font-size:13px;color:#c9d1d9;min-width:170px}
 .idf{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end}
 .idf>div{flex:1;min-width:130px} .idf label{margin:0 0 3px}
 .cand{display:grid;grid-template-columns:minmax(140px,1.2fr) 3fr 60px;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid #21262d;font-size:13px}
 .cand .bar{height:10px;background:#21262d;border-radius:5px;overflow:hidden} .cand .bar i{display:block;height:100%;background:#3fb950}
 .cand .why{grid-column:1/-1;color:#8b949e;font-size:12px;margin-top:-2px}
 .fctabs{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
 .fctabs a{border:1px solid #30363d;border-radius:20px;padding:3px 10px;font-size:13px}
 .fctabs a.on{border-color:#E69F00;color:#e6edf3;background:#E69F0022}
 #fcmap{height:420px;border-radius:10px;border:1px solid #21262d;background:#0f141a}
 .fcl .it{padding:6px 2px;border-bottom:1px solid #21262d;font-size:13px}
 .lv{display:inline-block;font-size:11px;border-radius:20px;padding:0 7px;margin-left:4px;color:#0d1117;font-weight:600}
 .lv1{background:#F0E442} .lv2{background:#E69F00} .lv3{background:#D55E00;color:#fff}
 @media(max-width:700px){ #fcmap{height:340px !important}}
 .pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
 .pbar{height:8px;background:#21262d;border-radius:4px;overflow:hidden;margin-top:3px} .pbar i{display:block;height:100%;background:#58a6ff;transition:width .6s}
 .sbanner{background:#3fb95018;border:1px solid #3fb95066;border-radius:10px;padding:10px 14px;margin-bottom:12px}
</style>
<h2 style="margin:0 0 2px">🪽 {{ t('sw_h') }}</h2>
<p class=muted style="margin:0 0 10px">{{ t('sw_intro', since=first_year) }}</p>
<div class=rangesw>
 {% for r in ranges %}<a class="{{ 'on' if r==z }}" href="?z={{ r }}{% if g %}&g={{ g }}{% endif %}&lang={{ lang }}">{{ t('sw_z_' ~ r, y=season_year) }}</a>{% endfor %}
 <span class=muted style="font-size:12px">{{ t('sw_z_year') }}</span>{% for y in year_list %}<a class="{{ 'on' if y==z }}" href="?z={{ y }}{% if g %}&g={{ g }}{% endif %}&lang={{ lang }}" style="padding:2px 8px">{{ y }}</a>{% endfor %}
 <form method=get style="display:inline;margin-left:auto"><input type=hidden name=z value="{{ z }}"><input type=hidden name=lang value="{{ lang }}">
 <select name=g class=gsel onchange="this.form.submit()"><option value="">{{ t('sw_all_genera') }}</option>{% for gg in genera %}<option value="{{ gg }}" {{ 'selected' if gg==g }}>{{ gg }}</option>{% endfor %}</select></form>
</div>
{% if season %}<div class=sbanner>🎉 <b>{{ t('sw_season_open', y=season.year) }}</b> {{ t('sw_season_first', label=season.label, d=season.flight_date|dmy, where=season.where) }}</div>{% endif %}
{% if flying %}<div class=chartbox style="margin-top:0"><h4>🟢 {{ t('sw_now_h') }}</h4><div class=now>{% for f in flying %}<span><i>{{ f.label }}</i> <b>{{ f.n }}</b>{% if f.regions %} · {{ f.regions|join(', ') }}{% endif %}</span>{% endfor %}</div></div>
{% else %}<p class=muted style="margin:0 0 8px">💤 {{ t('sw_now_none', d=last_date|dmy) }}</p>{% endif %}
{% if fc.show %}<div class=chartbox id=prognose style="border-color:#E69F0066"><h4>🔮 {{ t('fc_h') }} <span class=info title="{{ t('fc_exp') }}">ⓘ</span></h4>
{% if fc.active %}
<div class=fctabs>{% for d in fc.days %}<a class="{{ 'on' if loop.index0 == fc.di }}" href="?z={{ z }}{% if g %}&g={{ g }}{% endif %}&fd={{ loop.index0 }}{% if fc.taxon %}&ft={{ fc.taxon|urlencode }}{% endif %}&lang={{ lang }}#prognose">{{ t('fc_today') if loop.index0 == 0 else t('fc_tomorrow') }} · {{ d|dmy }}</a>{% endfor %}
 <form method=get action="/schwarmflug#prognose" style="display:inline;margin-left:auto"><input type=hidden name=z value="{{ z }}"><input type=hidden name=fd value="{{ fc.di }}"><input type=hidden name=lang value="{{ lang }}">
 <select name=ft class=gsel onchange="this.form.submit()"><option value="">{{ t('fc_all_taxa') }}</option>{% for x in fc.taxa %}<option value="{{ x }}" {{ 'selected' if x == fc.taxon }}>{{ x }}</option>{% endfor %}</select></form></div>
<div class=swgrid><div><div id=fcmap></div>
 <div class=swlegend><span>{{ t('fc_legend') }}</span><span><i style="background:#F0E442"></i>{{ t('fc_lv_1') }}</span><span><i style="background:#E69F00"></i>{{ t('fc_lv_2') }}</span><span><i style="background:#D55E00"></i>{{ t('fc_lv_3') }}</span></div>
 {% if not member %}<div class=mlock>🔒 {{ t('fc_members_cells') }}{% if login_url %} <a href="{{ login_url }}">{{ t('dir_login_btn') }}</a>{% endif %}</div>{% endif %}</div>
 <div class=fcl>{% for e in fc.list %}<div class=it><i>{{ e.taxon }}</i><span class="lv lv{{ e.level }}">{{ t('fc_lv_' ~ e.level) }}</span>
  <div class=muted style="font-size:12px">{{ e.regions|join(', ') }}{% if e.h_from is not none %} · {{ t('fc_hours', a=e.h_from, b=e.h_to) }}{% endif %}{% if e.temp is not none %} · ≈ {{ e.temp }} °C{% endif %}</div></div>
 {% else %}<p class=muted>{{ t('fc_nothing') }}</p>{% endfor %}</div></div>
<p class=muted style="font-size:12px;margin:8px 0 0">⚠️ {{ t('fc_disclaimer') }} {{ t('fc_run', t=fc.run_at) }} · <a href="https://open-meteo.com/" target=_blank rel=noopener>Weather data by Open-Meteo.com</a> (CC BY 4.0)</p>
{% else %}<p class=muted style="margin:0">{{ t('fc_resting') }}</p>{% endif %}
{% if fc.quality %}<details style="margin-top:8px"><summary class=muted style="cursor:pointer">📏 {{ t('fc_quality_h') }}</summary>
<p class=muted style="font-size:12px">{{ t('fc_quality_exp') }}</p>
<table class=swt><tr><th>{{ t('sw_col_taxon') }}</th><th>{{ t('fc_q_days') }}</th><th>{{ t('fc_q_lift') }}</th><th>{{ t('fc_q_prev') }}</th><th>{{ t('fc_q_auc') }}</th></tr>
{% for q in fc.quality %}<tr><td><i>{{ q.taxon }}</i></td><td>{{ q.n_pos }}</td><td>{{ q.lift }}</td><td class=muted>{{ q.prev or '?' }}</td><td>{{ q.auc }}</td></tr>{% endfor %}</table>
<p class=muted style="font-size:12px;margin:6px 0 0">{{ t('fc_q_rules', n=fc.rules) }}{% if fc.pending %} {{ t('fc_q_pending', n=fc.pending) }}{% endif %}</p></details>{% endif %}
</div>{% endif %}
{% if clim.ok %}<div class=chartbox id=kalender style="margin-top:10px"><h4>🗓️ {{ t('sw_cl_h') }} <span class=info title="{{ t('sw_cl_exp') }}">ⓘ</span></h4>
<form method=get action="/schwarmflug#kalender" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 8px"><input type=hidden name=z value="{{ z }}"><input type=hidden name=g value="{{ g }}"><input type=hidden name=lang value="{{ lang }}">
 <select name=ct class=gsel onchange="this.form.submit()">{% for x in clim.taxa %}<option {{ 'selected' if x == clim.taxon }}>{{ x }}</option>{% endfor %}</select>
 <button type=button class="btn small" id=clPlay>▶ {{ t('sw_tl_play') }}</button><input type=range id=clWeek min=0 max=30 value=12 style="flex:1;min-width:140px"><span class=muted id=clLbl style="font-size:12px"></span></form>
<div id=climmap style="height:420px;border-radius:10px;border:1px solid #21262d;background:#0f141a"></div>
<div class=swlegend style="margin-top:6px"><span>{{ t('sw_cl_legend') }}</span><span><i style="background:#F0E442"></i>10–25 %</span><span><i style="background:#E69F00"></i>25–50 %</span><span><i style="background:#D55E00"></i>&gt; 50 %</span></div>
<p class=muted style="font-size:12px;margin:6px 0 0">{{ t('sw_cl_note') }}{% if not member %} {{ t('sw_lrn_guest') }}{% endif %}</p></div>
{% elif fc.show %}<div class=chartbox id=kalender style="margin-top:10px"><h4>🗓️ {{ t('sw_cl_h') }}</h4><p class=muted style="margin:0">{{ t('sw_cl_wait') }}</p></div>{% endif %}
<div class=chartbox id=sammeln style="margin-top:10px"><h4>📡 {{ t('sw_prog_h') }} <span class=muted style="font-weight:400;font-size:12px" id=pgState>{{ prog.state }}</span></h4>
<div class=pgrid>{% for b in prog.bars %}<div><div class=muted style="font-size:12px">{{ t('sw_prog_' ~ b.key) }} · <span id="pg_{{ b.key }}_t">{{ b.done }} / {{ b.total }}</span></div><div class=pbar><i id="pg_{{ b.key }}" style="width:{{ b.pct }}%"></i></div></div>{% endfor %}</div>
<p style="font-size:13px;margin:6px 0 0" id=pgEta>{% if prog.eta %}⏳ {{ prog.eta }}{% endif %}</p>
<p class=muted style="font-size:12px;margin:4px 0 0" id=pgBudget>{{ prog.budget }}</p>
{% if data.learn %}<details style="margin-top:8px" id=lrnBox><summary class=muted style="cursor:pointer;font-size:13px">🗺️ {{ t('sw_lrn_h') }}</summary>
<p class=muted style="font-size:12px;margin:6px 0">{{ t('sw_lrn_exp') }}{% if not member %} {{ t('sw_lrn_guest') }}{% endif %}</p>
<div id=learnmap style="height:340px;border-radius:10px;border:1px solid #21262d;background:#0f141a"></div>
<div class=swlegend style="margin-top:6px"><span>{{ t('sw_lrn_legend') }}</span><span><i style="background:#30363d"></i>0</span><span><i style="background:#1f6f3a"></i>{{ t('sw_lrn_part') }}</span><span><i style="background:#3fb950"></i>{{ t('sw_lrn_full') }}</span><span>◆ {{ t('sw_lrn_hot') }}</span><span>● {{ t('sw_lrn_grid') }}</span></div></details>{% endif %}
</div>
<div class=kpigrid style="margin:12px 0">
 <div class=kpi><div class=v>{{ k.total }}</div><div class=l>{{ t('sw_kpi_reports') }}</div></div>
 <div class=kpi><div class=v>{{ k.taxa }}</div><div class=l>{{ t('sw_kpi_taxa') }}</div></div>
 <div class=kpi><div class=v>{{ k.genera }}</div><div class=l>{{ t('sw_kpi_genera') }}</div></div>
 <div class=kpi><div class=v>{{ k.reporters }}</div><div class=l>{{ t('sw_kpi_reporters') }}</div></div>
 <div class=kpi><div class=v>{{ k.regions }}</div><div class=l>{{ t('sw_kpi_regions') }}</div></div>
</div>
<div class=swgrid>
 <div><div id=swmap></div>
  <div class=swlegend><span>{{ t('sw_legend') }}</span>{% for c, lbl in legend %}<span><i style="background:{{ c }}"></i>{{ lbl }}</span>{% endfor %}{% if member %}<span>· {{ t('sw_legend_pin') }}:</span>{% for gg, c in data.gcol.items() %}<span><i style="background:{{ c }};border-radius:50%;width:10px"></i>{{ gg }}</span>{% endfor %}<span><i style="background:#f0f6fc;border-radius:50%;width:10px"></i>{{ t('sw_legend_other') }}</span>{% endif %}</div>
  <div class=tl><button type=button class="btn grey small" id=tlPlay title="{{ t('sw_tl_hint') }}">▶ {{ t('sw_tl_play') }}</button>
   <input type=range id=tlWeek min=0 max="{{ data.weeks|length - 1 }}" value=0 aria-label="{{ t('sw_tl_hint') }}"><span class=lbl id=tlLbl>{{ t('sw_tl_all') }}</span></div>
  {% if home_n %}<p class=muted style="font-size:12px;margin:4px 0 0">≈ {{ t('sw_home_note', n=home_n) }}</p>{% endif %}
  {% if not member %}<div class=mlock>🔒 {{ t('sw_members_only') }}{% if login_url %} <a href="{{ login_url }}">{{ t('dir_login_btn') }}</a>{% endif %}</div>{% endif %}
 </div>
 <div class=chartbox style="margin-top:0"><h4>📜 {{ t('sw_feed_h') }}</h4><div class=feed>
 {% for r in recent %}<div class=it><i>{{ r.label }}</i>{% if r.indoor %} 🏠{% endif %}
  {% if r.abroad %}<span title="{{ t('sw_abroad_hint') }}"> 🌍</span>{% endif %}
  <div class=w>{{ r.flight_date|dmy }}{% if r.flight_time %} · {{ r.flight_time }}{% endif %}{% if r.temp_c is not none %} · {{ '%g'|format(r.temp_c) }} °C{% elif r.w_temp is not none %} · <span title="{{ t('sw_temp_est') }}">≈ {{ '%.0f'|format(r.w_temp) }} °C</span>{% endif %}{% if r.region_name %} · {% if r.geo_src == 'home' %}<span title="{{ t('sw_home_hint') }}">≈ {{ r.region_name }}</span>{% else %}{{ r.region_name }}{% endif %}{% endif %}{% if member and r.raw_label %} · <span class=info title="{{ t('sw_raw_hint', raw=r.raw_label) }}">ⓘ</span>{% endif %}
  {% if member %}{% if r.place %}<br>📍 {{ r.place }}{% endif %}{% if r.link %} · <a href="{{ r.link }}" target=_blank rel=noopener>Discord ↗</a>{% endif %}{% endif %}</div></div>
 {% else %}<p class=muted>{{ t('sw_feed_none') }}</p>{% endfor %}</div></div>
</div>
<div class=chartbox><h4>📈 {{ t('sw_season_h') }} <span class=info title="{{ t('sw_season_exp') }}">ⓘ</span></h4><div class=chartwrap style="height:280px"><canvas id=chSeason></canvas></div></div>
<div class=twobox>
 <div class=chartbox><h4>🕑 {{ t('sw_hour_h') }}</h4><div class=chartwrap style="height:200px"><canvas id=chHour></canvas></div></div>
 <div class=chartbox><h4>🌡️ {{ t('sw_temp_h') }}</h4><div class=chartwrap style="height:200px"><canvas id=chTemp></canvas></div></div>
</div>
<div class=chartbox><h4>🐜 {{ t('sw_taxa_h') }} <span class=info title="{{ t('sw_taxa_exp') }}">ⓘ</span></h4>
<div style="overflow-x:auto"><table class=swt><tr><th>{{ t('sw_col_taxon') }}</th><th>{{ t('sw_col_n') }}</th><th>{{ t('sw_col_window') }}</th><th>{{ t('sw_col_peak') }}</th><th>{{ t('sw_col_months') }}</th><th>{{ t('sw_col_hour') }}</th><th>{{ t('sw_col_temp') }}</th><th>{{ t('sw_col_regions') }}</th></tr>
{% for x in taxa %}<tr><td>{% if x.art %}<a href="/art/{{ x.art }}{{ qs() }}"><i>{{ x.name }}</i></a>{% else %}<i>{{ x.name }}</i>{% endif %}</td><td>{{ x.n }}</td>
<td>{{ x.win }}</td><td>{{ x.peak }}</td><td><div class=bars>{% for m in x.months %}<div style="height:{{ (2 + m * 20)|int }}px;opacity:{{ 0.25 if m == 0 else 1 }}" title="{{ month_names[loop.index0] }}"></div>{% endfor %}</div></td>
<td style="white-space:nowrap">{{ x.hours or '?' }}</td><td style="white-space:nowrap">{{ x.temps or '?' }}</td><td class=muted>{{ x.regions|join(', ') }}</td></tr>{% endfor %}</table></div></div>
{% if abroad %}<div class=chartbox id=ausland><h4>🌍 {{ t('sw_abroad_h') }} <span class=info title="{{ t('sw_abroad_exp') }}">ⓘ</span></h4>
<div style="overflow-x:auto"><table class=swt><tr><th>{{ t('sw_col_taxon') }}</th><th>{{ t('sw_abroad_country') }}</th><th>{{ t('sw_abroad_when') }}</th>{% if member %}<th>{{ t('sw_abroad_place') }}</th>{% endif %}</tr>
{% for r in abroad %}<tr><td><i>{{ r.label }}</i></td><td>{{ r.country_name }}</td><td>{{ r.flight_date|dmy }}</td>{% if member %}<td class=muted>{{ r.place }}{% if r.link %} · <a href="{{ r.link }}" target=_blank rel=noopener>Discord ↗</a>{% endif %}</td>{% endif %}</tr>{% endfor %}</table></div>
{% if abroad_more %}<p class=muted style="font-size:12px;margin:6px 0 0">{{ t('sw_abroad_more', n=abroad_more) }}</p>{% endif %}</div>{% endif %}
{% if admin %}<p class=muted style="font-size:13px">🔒 <a href="/admin/schwarmflug{{ qs() }}">{{ t('sw_admin_link', n=unclear_n) }}</a></p>{% endif %}
<div class=chartbox id=bestimmen><h4>🔎 {{ t('sw_id_h') }} <span class=info title="{{ t('sw_id_exp') }}">ⓘ</span></h4>
<p class=muted style="margin:0 0 8px">{{ t('sw_id_intro') }}</p>
<form method=get action="/schwarmflug#bestimmen" class=idf><input type=hidden name=lang value="{{ lang }}"><input type=hidden name=z value="{{ z }}">
 <div><label>{{ t('sw_id_date') }}</label><input type=date name=wd value="{{ ident.date }}" required></div>
 <div><label>{{ t('sw_id_hour') }}</label><select name=wt><option value="">?</option>{% for h in range(6, 24) %}<option value="{{ h }}" {{ 'selected' if ident.hour == h }}>{{ h }}:00</option>{% endfor %}</select></div>
 <div style="flex:2"><label>{{ t('sw_id_region') }}</label><select name=wr>{% for v, lbl in regions_sel %}<option value="{{ v }}" {{ 'selected' if v == ident.region }}>{{ lbl }}</option>{% endfor %}</select></div>
 <div><label>{{ t('sw_id_temp') }}</label><input type=number name=wtemp min=-5 max=45 step=1 value="{{ ident.temp if ident.temp is not none else '' }}" placeholder="°C"></div>
 <button class=btn>{{ t('sw_id_go') }}</button></form>
{% if ident.asked %}{% if ident.cands %}<div style="margin-top:10px">{% for c in ident.cands %}<div class=cand>
 <div>{% if c.art %}<a href="/art/{{ c.art }}{{ qs() }}"><i>{{ c.name }}</i></a>{% else %}<i>{{ c.name }}</i>{% endif %}</div>
 <div class=bar><i style="width:{{ c.pct }}%"></i></div><div style="text-align:right">{{ c.pct }} %</div>
 <div class=why>{{ c.why }}</div></div>{% endfor %}</div>
 <p class=muted style="font-size:12px;margin:8px 0 0">{{ t('sw_id_note') }}</p>
{% else %}<p>{{ t('sw_id_none') }}</p>{% endif %}{% endif %}</div>

{% if wx %}<div class=chartbox><h4>🌦️ {{ t('sw_wx_h') }} <span class=info title="{{ t('sw_wx_exp') }}">ⓘ</span></h4>
<p class=muted style="margin:0 0 8px">{{ t('sw_wx_intro', n=wx_n) }}</p>
<div style="overflow-x:auto"><table class=swt><tr><th>{{ t('sw_col_taxon') }}</th><th>{{ t('sw_wx_n') }}</th><th>{{ t('sw_wx_hum') }}</th><th>{{ t('sw_wx_press') }}</th><th>{{ t('sw_wx_wind') }}</th><th>{{ t('sw_wx_cloud') }}</th><th>{{ t('sw_wx_rain') }}</th></tr>
{% for x in wx %}<tr><td><i>{{ x.name }}</i></td><td>{{ x.n }}</td><td style="white-space:nowrap">{{ x.hum }}</td><td style="white-space:nowrap">{{ x.press }}</td><td style="white-space:nowrap">{{ x.wind }}</td><td style="white-space:nowrap">{{ x.cloud }}</td><td>{{ x.rain }}</td></tr>{% endfor %}</table></div>
<p class=muted style="font-size:12px;margin:8px 0 0">{{ t('sw_wx_src') }} <a href="https://open-meteo.com/" target=_blank rel=noopener>Weather data by Open-Meteo.com</a> (CC BY 4.0)</p></div>{% endif %}
{% if data.wreg %}<div class=chartbox id=wetterkarte><h4>🌡️ {{ t('sw_wr_h') }} <span class=info title="{{ t('sw_wr_exp') }}">ⓘ</span></h4>
<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 8px"><select id=wrVar class=gsel>{% for v in ['temp','rain','hum','press','wind'] %}<option value="{{ loop.index }}">{{ t('sw_wr_' ~ v) }}</option>{% endfor %}</select>
<span class=muted style="font-size:12px">{% if g %}{{ t('sw_wr_for', g=g) }}{% else %}{{ t('sw_wr_all') }}{% endif %}</span></div>
<div id=wrmap style="height:420px;border-radius:10px;border:1px solid #21262d;background:#0f141a"></div>
<div class=swlegend style="margin-top:6px" id=wrLegend></div></div>{% endif %}

{% if data.yrs.series %}<div class=chartbox><h4>📅 {{ t('sw_years_h') }} <span class=info title="{{ t('sw_years_exp') }}">ⓘ</span></h4><div class=chartwrap style="height:260px"><canvas id=chYears></canvas></div></div>{% endif %}

{% if cmp %}<div class=chartbox id=vergleich><h4>⚖️ {{ t('sw_cmp_h') }} <span class=info title="{{ t('sw_cmp_exp') }}">ⓘ</span></h4>
<form method=get action="/schwarmflug#vergleich" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 8px"><input type=hidden name=z value="{{ z }}"><input type=hidden name=g value="{{ g }}"><input type=hidden name=lang value="{{ lang }}">
 <select name=ya class=gsel onchange="this.form.submit()">{% for y in cmp.years %}<option {{ 'selected' if y == cmp.ya }}>{{ y }}</option>{% endfor %}</select>
 <span class=muted>{{ t('sw_cmp_vs') }}</span>
 <select name=yb class=gsel onchange="this.form.submit()">{% for y in cmp.years %}<option {{ 'selected' if y == cmp.yb }}>{{ y }}</option>{% endfor %}</select>
 <select name=yt class=gsel onchange="this.form.submit()"><option value="">{{ t('sw_cmp_all') }}</option>{% for o in cmp.opts %}<option value="{{ o.v }}" {{ 'selected' if o.v == cmp.tx }}>{{ o.l }}</option>{% endfor %}</select>
 <noscript><button class="btn small">OK</button></noscript></form>
<p style="margin:0 0 8px"><b>{{ cmp.say }}</b></p>
<div class=chartwrap style="height:240px"><canvas id=chCmp></canvas></div>
<div style="overflow-x:auto;margin-top:8px"><table class=swt><tr><th></th>{% for c in cmp.cols %}<th>{{ c.year }}</th>{% endfor %}</tr>
<tr><td>{{ t('sw_kpi_reports') }}</td>{% for c in cmp.cols %}<td>{{ c.n }}</td>{% endfor %}</tr>
<tr><td>{{ t('sw_cmp_first') }}</td>{% for c in cmp.cols %}<td>{{ c.first }}</td>{% endfor %}</tr>
<tr><td>{{ t('sw_cmp_peak') }}</td>{% for c in cmp.cols %}<td>{{ c.peak }}</td>{% endfor %}</tr>
<tr><td>{{ t('sw_cmp_last') }}</td>{% for c in cmp.cols %}<td>{{ c.last }}</td>{% endfor %}</tr>
<tr><td>{{ t('sw_cmp_temp') }}</td>{% for c in cmp.cols %}<td>{{ c.temp }}{% if c.temp_n %} <span class=muted style="font-size:11px">({{ c.temp_n }})</span>{% endif %}</td>{% endfor %}</tr></table></div>
<p class=muted style="font-size:12px;margin:6px 0 0">{{ t('sw_cmp_note') }}</p></div>{% endif %}

<p class=muted style="font-size:12px">ℹ️ {{ t('sw_note') }} · {{ t('sw_attrib') }} <a href="https://www.geoboundaries.org" target=_blank rel=noopener>geoBoundaries</a> (CC BY 4.0){% if osm %} · {{ t('sw_osm') }} <a href="https://www.openstreetmap.org/copyright" target=_blank rel=noopener>© OpenStreetMap-Mitwirkende</a> (ODbL){% endif %}</p>
<script src="/static/leaflet.js?v={{ v }}"></script>
<script src="/static/chart.umd.js?v={{ v }}"></script>
<script>
(function(){
 var D={{ data|tojson }};
 function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
 var CH=["#1b2a1f","#1f4d2c","#2a7a3e","#3fb950","#a6f0b5"];
 function col(n){return n>50?CH[4]:n>20?CH[3]:n>8?CH[2]:n>2?CH[1]:n>0?CH[0]:"#161b22";}
 if(window.L){
  var map=L.map("swmap",{minZoom:4,maxZoom:12,zoomSnap:0.25,preferCanvas:true,attributionControl:true}).fitBounds([[45.8,5.9],[55.05,17.1]]);
  map.attributionControl.setPrefix(false);
  map.createPane("pinPane").style.zIndex=620;
  var cur=D.regions, polys=[], CL=null;
  function sty(ft){var n=cur[key(ft)]||0;return {color:"#30363d",weight:1,fillColor:col(n),fillOpacity:n?0.85:0.5};}
  ["de_bundeslaender","at_bundeslaender","ch_kantone","li_land"].forEach(function(f){
   fetch("/static/"+f+".geojson",{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).then(function(gj){ if(!gj) return;
    polys.push(L.geoJSON(gj,{style:sty,
     onEachFeature:function(ft,ly){var k=key(ft),nm=(ft.properties||{}).shapeName||"";
      ly.bindPopup(function(){var n=cur[k]||0;return "<b>"+esc(D.names[k]||nm)+"</b><br>"+n+" "+esc(D.t.reports)+(cur===D.regions&&D.top[k]?"<br><span style='color:#8b949e'>"+esc(D.top[k])+"</span>":"");});
      ly.on("mouseover",function(){ly.setStyle({color:"#58a6ff",weight:2});});ly.on("mouseout",function(){ly.setStyle({color:"#30363d",weight:1});});}}).addTo(map));
   }).catch(function(){});
  });
  function restyle(){polys.forEach(function(g){g.setStyle(sty);});}
  function key(ft){var p=ft.properties||{},iso=String(p.shapeISO||"");if(iso==="LIE"||p.shapeName==="Liechtenstein")return "li-LI";var i=iso.indexOf("-");return i>0?iso.slice(0,i).toLowerCase()+"-"+iso.slice(i+1).toUpperCase():"";}
  var pinLayer=L.layerGroup().addTo(map), marks=[];
  (D.pins||[]).forEach(function(p){
   var m=L.circleMarker([p[0],p[1]],{pane:"pinPane",radius:5,color:"#0d1117",weight:1,fillColor:D.gcol[p[2]]||"#f0f6fc",fillOpacity:.95})
    .bindPopup("<b><i>"+esc(p[3])+"</i></b><br>"+esc(p[4])+(p[5]?"<br>📍 "+esc(p[5]):"")+(p[6]?"<br><a href='"+esc(p[6])+"' target=_blank rel=noopener>Discord ↗</a>":""));
   m.addTo(pinLayer); marks.push([m,p[7]]);
  });
  // Zeitraffer: Woche für Woche (alle Jahre des gewählten Zeitraums übereinander)
  var btn=document.getElementById("tlPlay"),rng=document.getElementById("tlWeek"),lbl=document.getElementById("tlLbl"),timer=null;
  function show(i){
   if(i<0){cur=D.regions;lbl.textContent=D.t.tlAll;marks.forEach(function(x){pinLayer.addLayer(x[0]);});restyle();return;}
   var w=D.wkNum[i];cur=D.tl[w]||{};lbl.textContent=D.weekTitles[i];
   marks.forEach(function(x){if(x[1]===w)pinLayer.addLayer(x[0]);else pinLayer.removeLayer(x[0]);});restyle();
  }
  function stop(reset){if(timer){clearInterval(timer);timer=null;}btn.textContent="▶ "+D.t.tlPlay;if(reset)show(-1);}
  if(btn&&rng){
   btn.addEventListener("click",function(){
    if(timer){stop(false);return;}
    var i=+rng.value; if(i>=D.wkNum.length-1)i=0; rng.value=i; show(i); btn.textContent="⏸ "+D.t.tlPause;
    timer=setInterval(function(){i++; if(i>=D.wkNum.length){stop(true);rng.value=0;return;} rng.value=i; show(i);},900);
   });
   rng.addEventListener("input",function(){stop(false);show(+rng.value);});
  }
 }
 if(window.L&&D.fc&&document.getElementById("fcmap")){
  var FCOL={1:"#F0E442",2:"#E69F00",3:"#D55E00"};
  var fm=L.map("fcmap",{minZoom:4,maxZoom:11,zoomSnap:0.25,preferCanvas:true}).fitBounds([[45.8,5.9],[55.05,17.1]]);
  fm.attributionControl.setPrefix(false);
  fm.createPane("cellPane").style.zIndex=620;
  ["de_bundeslaender","at_bundeslaender","ch_kantone","li_land"].forEach(function(f){
   fetch("/static/"+f+".geojson",{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).then(function(gj){ if(!gj) return;
    L.geoJSON(gj,{style:function(ft){var lv=D.fc.reg[key(ft)]||0;return {color:"#30363d",weight:1,fillColor:lv?FCOL[lv]:"#161b22",fillOpacity:lv?0.55:0.5};},
     onEachFeature:function(ft,ly){var k=key(ft),e=D.fc.regInfo[k];ly.bindPopup("<b>"+esc(D.names[k]||(ft.properties||{}).shapeName||"")+"</b>"+(e?"<br>"+esc(e):"<br>"+esc(D.t.fcNone)));}}).addTo(fm);
   }).catch(function(){});
  });
  (D.fc.cells||[]).forEach(function(c){
   L.circleMarker([c[0],c[1]],{pane:"cellPane",radius:c[3]==="hot"?5:7,color:"#0d1117",weight:1,fillColor:FCOL[c[2]],fillOpacity:.9})
    .bindPopup("<b><i>"+esc(c[4])+"</i></b><br>"+esc(c[5])).addTo(fm);
  });
 }
 // ── Hilfen für die zusätzlichen Karten ──
 function rkey(ft){var p=ft.properties||{},iso=String(p.shapeISO||"");if(iso==="LIE"||p.shapeName==="Liechtenstein")return "li-LI";var i=iso.indexOf("-");return i>0?iso.slice(0,i).toLowerCase()+"-"+iso.slice(i+1).toUpperCase():"";}
 function mkmap(id){var m=L.map(id,{minZoom:4,maxZoom:11,zoomSnap:0.25,preferCanvas:true}).fitBounds([[45.8,5.9],[55.05,17.1]]);m.attributionControl.setPrefix(false);m.createPane("dotPane").style.zIndex=620;return m;}
 function outlines(m,styleFn,popFn,done){var gs=[];["de_bundeslaender","at_bundeslaender","ch_kantone","li_land"].forEach(function(f){
   fetch("/static/"+f+".geojson",{credentials:"same-origin"}).then(function(r){return r.ok?r.json():null;}).then(function(gj){if(!gj)return;
    var g=L.geoJSON(gj,{style:styleFn,onEachFeature:function(ft,ly){if(popFn)ly.bindPopup(function(){return popFn(rkey(ft),ft);});}}).addTo(m);gs.push(g);if(done)done(gs);}).catch(function(){});});return gs;}
 function dot(lat,lon,hot,color,tip){
  if(hot)return L.marker([lat,lon],{pane:"dotPane",icon:L.divIcon({className:"",iconSize:[10,10],html:"<div style='width:9px;height:9px;transform:rotate(45deg);background:"+color+";border:1px solid #0d1117'></div>"})}).bindPopup(tip);
  return L.circleMarker([lat,lon],{pane:"dotPane",radius:4,color:"#0d1117",weight:1,fillColor:color,fillOpacity:.95}).bindPopup(tip);}
 function mix(a,b,f){var x=parseInt(a.slice(1),16),y=parseInt(b.slice(1),16),r=function(s){return Math.round(((x>>s)&255)*(1-f)+((y>>s)&255)*f);};return "#"+((1<<24)+(r(16)<<16)+(r(8)<<8)+r(0)).toString(16).slice(1);}
 // 1. Lerndaten-Karte (erst beim Aufklappen zeichnen, sonst falsche Größe)
 var lb=document.getElementById("lrnBox");
 if(window.L&&lb&&D.learn&&D.learn.length){var lrnDone=false;lb.addEventListener("toggle",function(){if(!lb.open||lrnDone)return;lrnDone=true;
  var lm=mkmap("learnmap");outlines(lm,function(){return {color:"#30363d",weight:1,fillColor:"#161b22",fillOpacity:.5};});
  D.learn.forEach(function(p){var f=p[5]?p[4]/p[5]:0,c=f<=0?"#30363d":f>=1?"#3fb950":mix("#1b3a24","#3fb950",f);
   var tip="<b>"+esc(p[2]?D.t.lrnHot:D.t.lrnGrid)+"</b><br>"+p[4]+" "+esc(D.t.lrnOf)+" "+p[5]+(p[3]<p[4]?"<br><span style='color:#8b949e'>"+p[3]+" "+esc(D.t.lrnFine)+"</span>":"");
   dot(p[0],p[1],p[2],c,tip).addTo(lm);});});}
 // 3. Wetter beim Flug je Region
 if(window.L&&D.wreg&&document.getElementById("wrmap")){
  var wm=mkmap("wrmap"),wsel=document.getElementById("wrVar"),vi=1,WRC=["#2c7bb6","#abd9e9","#ffffbf","#fdae61","#d7191c"],wrng=[0,1];
  var units=["°C","%","%","hPa","km/h"];
  function calc(){var vs=Object.keys(D.wreg).map(function(k){return D.wreg[k][vi];}).filter(function(v){return v!==null&&v!==undefined;});
   wrng=vs.length?[Math.min.apply(null,vs),Math.max.apply(null,vs)]:[0,1];}
  function wcol(v){if(v===null||v===undefined)return null;var f=wrng[1]>wrng[0]?(v-wrng[0])/(wrng[1]-wrng[0]):.5;return WRC[Math.min(4,Math.floor(f*5))];}
  function wsty(ft){var e=D.wreg[rkey(ft)],c=e?wcol(e[vi]):null;return {color:"#30363d",weight:1,fillColor:c||"#161b22",fillOpacity:c?.8:.4};}
  function legend(){var el=document.getElementById("wrLegend");if(!el)return;var u=units[vi-1],h="<span>"+esc(wsel.options[wsel.selectedIndex].text)+"</span>";
   for(var i=0;i<5;i++){var a=wrng[0]+(wrng[1]-wrng[0])*i/5;h+="<span><i style='background:"+WRC[i]+"'></i>"+(Math.round(a*10)/10).toLocaleString(D.lang)+(i===4?"+":"")+" "+u+"</span>";}
   h+="<span><i style='background:#161b22'></i>"+esc(D.t.wrFew)+"</span>";el.innerHTML=h;}
  calc();legend();
  var wgs=outlines(wm,wsty,function(k,ft){var e=D.wreg[k],nm=esc(D.names[k]||(ft.properties||{}).shapeName||"");
   if(!e)return "<b>"+nm+"</b><br>"+esc(D.t.wrFew);
   var h="<b>"+nm+"</b><br>"+(e[0]<3?"&lt; 3":e[0])+" "+esc(D.t.wrN);
   ["temp","rain","hum","press","wind"].forEach(function(v,i){if(e[i+1]!==null&&e[i+1]!==undefined)h+="<br>"+esc(D.t.wrVars[v])+": "+e[i+1].toLocaleString(D.lang)+" "+units[i];});return h;});
  wsel.addEventListener("change",function(){vi=+wsel.value;calc();legend();wgs.forEach(function(g){g.setStyle(wsty);});});
 }
 // 4. Flugwetter-Kalender
 if(window.L&&D.clim&&document.getElementById("climmap")){
  var cm=mkmap("climmap");outlines(cm,function(){return {color:"#30363d",weight:1,fillColor:"#161b22",fillOpacity:.5};});
  function ccol(v){return v===null||v===undefined||v<10?null:v>=50?"#D55E00":v>=25?"#E69F00":"#F0E442";}
  var cdots=D.clim.pts.map(function(p){
   var mk=p[2]?L.marker([p[0],p[1]],{pane:"dotPane",icon:L.divIcon({className:"",iconSize:[10,10],html:"<div class=cld style='width:9px;height:9px;transform:rotate(45deg);border:1px solid #0d1117'></div>"})})
             :L.circleMarker([p[0],p[1]],{pane:"dotPane",radius:6,color:"#0d1117",weight:1,fillOpacity:.9});
   mk.addTo(cm);return [mk,p];});
  var cw=document.getElementById("clWeek"),cl=document.getElementById("clLbl"),cp=document.getElementById("clPlay"),ct=null;
  function cshow(i){cl.textContent=D.weekTitles[i]+" · "+D.clim.taxon;
   cdots.forEach(function(x){var v=x[1][3][i],c=ccol(v),tip="<b><i>"+esc(D.clim.taxon)+"</i></b><br>"+esc(D.weekTitles[i])+"<br>"+(v===null||v===undefined?"–":v+" % "+esc(D.t.clDays));
    if(x[1][2]){var el=x[0].getElement();if(el&&el.firstChild){el.firstChild.style.background=c||"#30363d";el.firstChild.style.opacity=c?1:.4;}}
    else x[0].setStyle({fillColor:c||"#30363d",fillOpacity:c?.9:.35});
    x[0].bindPopup(tip);});}
  cshow(+cw.value);
  cw.addEventListener("input",function(){if(ct){clearInterval(ct);ct=null;cp.textContent="▶ "+D.t.tlPlay;}cshow(+cw.value);});
  cp.addEventListener("click",function(){if(ct){clearInterval(ct);ct=null;cp.textContent="▶ "+D.t.tlPlay;return;}
   var i=+cw.value;if(i>=30)i=0;cp.textContent="⏸ "+D.t.tlPause;ct=setInterval(function(){i++;if(i>30){clearInterval(ct);ct=null;cp.textContent="▶ "+D.t.tlPlay;return;}cw.value=i;cshow(i);},800);});
 }
 (function poll(){
  setTimeout(function(){
   fetch("/schwarmflug/status.json?lang="+encodeURIComponent(D.lang),{cache:"no-store"}).then(function(r){return r.ok?r.json():null;}).then(function(j){
    if(!j) return;
    j.bars.forEach(function(b){var e=document.getElementById("pg_"+b.key),x=document.getElementById("pg_"+b.key+"_t");if(e)e.style.width=b.pct+"%";if(x)x.textContent=b.done+" / "+b.total;});
    var s=document.getElementById("pgState");if(s)s.textContent=j.state;var bu=document.getElementById("pgBudget");if(bu)bu.textContent=j.budget;
    var et=document.getElementById("pgEta");if(et)et.textContent=j.eta?"⏳ "+j.eta:"";
   }).catch(function(){}).then(poll);
  },10000);
 })();
 if(!window.Chart) return;
 Chart.defaults.color="#8b949e"; Chart.defaults.borderColor="#21262d";
 new Chart(document.getElementById("chSeason"),{type:"line",data:{labels:D.weeks,datasets:D.series.map(function(s){return {label:s.label,data:s.data,borderColor:s.color,backgroundColor:s.color+"33",tension:.35,pointRadius:0,borderWidth:2,fill:false};})},
  options:{maintainAspectRatio:false,interaction:{mode:"index",intersect:false},plugins:{legend:{labels:{boxWidth:12}},tooltip:{callbacks:{title:function(it){return D.weekTitles[it[0].dataIndex];}}}},
   scales:{x:{ticks:{autoSkip:false,callback:function(v,i){return D.weekTicks[i];}},grid:{display:false}},y:{beginAtZero:true,title:{display:true,text:D.t.perWeek}}}}});
 function bars(id,lbl,vals,c){new Chart(document.getElementById(id),{type:"bar",data:{labels:lbl,datasets:[{data:vals,backgroundColor:c,borderRadius:3}]},options:{maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false}},y:{beginAtZero:true}}}});}
 bars("chHour",D.hours.labels,D.hours.data,"#56B4E9");
 bars("chTemp",D.temps.labels,D.temps.data,"#E69F00");
 var cc=document.getElementById("chCmp");
 if(cc&&D.cmp){
  new Chart(cc,{type:"line",data:{labels:D.cmp.weeks,datasets:D.cmp.sets.map(function(s){return {label:s.label,data:s.data,borderColor:s.color,backgroundColor:s.color,borderDash:s.dash?[6,4]:[],tension:.25,pointRadius:2,borderWidth:2};})},
   options:{maintainAspectRatio:false,interaction:{mode:"index",intersect:false},plugins:{legend:{labels:{boxWidth:12}},tooltip:{callbacks:{title:function(it){return D.weekTitles[it[0].dataIndex];},label:function(c){return c.dataset.label+": "+c.parsed.y.toLocaleString(D.lang)+" %";}}}},
    scales:{x:{ticks:{autoSkip:false,callback:function(v,i){return D.weekTicks[i];}},grid:{display:false}},y:{beginAtZero:true,title:{display:true,text:D.cmp.t.axis}}}}});
 }
 var cy=document.getElementById("chYears");
 if(cy&&D.yrs.series.length){
  function dd(v){var d=new Date(Date.UTC(2025,0,1)+(v-1)*864e5);return ("0"+d.getUTCDate()).slice(-2)+"."+("0"+(d.getUTCMonth()+1)).slice(-2)+".";}
  new Chart(cy,{type:"line",data:{labels:D.yrs.years,datasets:D.yrs.series.map(function(s){return {label:s.label,data:s.data,borderColor:s.color,backgroundColor:s.color,spanGaps:true,tension:.2,pointRadius:4,borderWidth:2};})},
   options:{maintainAspectRatio:false,plugins:{legend:{labels:{boxWidth:12}},tooltip:{callbacks:{label:function(c){return c.dataset.label+": "+dd(c.parsed.y);}}}},
    scales:{y:{reverse:false,ticks:{callback:function(v){return dd(v);}},title:{display:true,text:D.t.median}}}}});
 }
})();
</script>
{% endblock %}"""


def _week(d: str) -> int:
    try:
        return date.fromisoformat(d).isocalendar()[1]
    except ValueError:
        return 0


def _since(z: str, today: date) -> str | None:
    if z == "h48":
        return (today - timedelta(days=2)).isoformat()
    if z == "d7":
        return (today - timedelta(days=7)).isoformat()
    if z == "saison":
        return f"{today.year if today.month >= 3 else today.year - 1}-01-01"
    return None


def _taxon_key(r) -> str:
    return r["genus"] + (" " + r["species"] if r["species"] else "")


def _doy(d: str) -> int:
    try:
        return date.fromisoformat(d).timetuple().tm_yday
    except ValueError:
        return 0


def _med(xs):
    from statistics import median
    xs = [x for x in xs if x is not None]
    return median(xs) if xs else None


def identify(rows: list[dict], d: date, hour: int | None, region: str, temp: float | None, t) -> list[dict]:
    """„Was fliegt hier gerade?“ Rangliste der Arten (nur bis zur Art bestimmte Meldungen, ab 5).
    Gewichtet: Meldungen ±7 Tage um den Kalendertag (alle Jahre), stärker in der gewählten
    Region, dann wie gut Uhrzeit und Temperatur zu den bisherigen Meldungen passen."""
    c, _, reg = (region or "").partition("-")
    doy = d.timetuple().tm_yday
    by: dict = {}
    for r in rows:
        if r["species"] and not r["qualifier"]:
            by.setdefault(_taxon_key(r), []).append(r)
    out = []
    for name, lst in by.items():
        if len(lst) < 5:
            continue
        win = [r for r in lst if abs(_doy(r["flight_date"]) - doy) <= 7]
        if not win:
            continue
        loc = [r for r in win if c and r["country"] == c and (not reg or r["region"] == reg)]
        base = len(loc) * 3 + len(win) * (0.3 if c else 1)
        fh = ft = 1.0
        hrs = [int(r["flight_time"][:2]) for r in lst if r["flight_time"]]
        if hour is not None and len(hrs) >= 3:
            fh = 0.15 + sum(1 for h in hrs if abs(h - hour) <= 2) / len(hrs)
        tmp = [x for x in (S.temp_of(r) for r in lst) if x is not None]
        if temp is not None and len(tmp) >= 3:
            ft = 0.15 + sum(1 for x in tmp if abs(x - temp) <= 3) / len(tmp)
        why = t("sw_id_why_season", n=len(win))
        if c:
            why += " " + t("sw_id_why_region", n=len(loc))
        if len(hrs) >= 4:
            hs = sorted(hrs)
            why += " " + t("sw_id_why_hour", a=hs[len(hs) // 4], b=hs[(len(hs) * 3) // 4])
        out.append({"name": name, "score": base * fh * ft, "why": why})
    out.sort(key=lambda x: -x["score"])
    out = out[:8]
    tot = sum(x["score"] for x in out) or 1
    for x in out:
        x["pct"] = max(1, round(100 * x["score"] / tot))
    return out


def weather_table(rows: list[dict], t) -> tuple[list[dict], int]:
    ok = [r for r in rows if r.get("w_state") == 1]
    by: dict = {}
    for r in ok:
        by.setdefault(r["genus"], []).append(r)
        if r["species"]:
            by.setdefault(_taxon_key(r), []).append(r)
    out = []
    for name, lst in sorted(by.items(), key=lambda x: -len(x[1])):
        if len(lst) < 8:
            continue

        def rng(k, unit, nd=0):
            xs = sorted(x[k] for x in lst if x[k] is not None)
            if len(xs) < 4:
                return "?"
            a, b = xs[len(xs) // 4], xs[(len(xs) * 3) // 4]
            f = (lambda v: f"{v:.{nd}f}")
            return f"{f(a)}–{f(b)} {unit}" if f(a) != f(b) else f"{f(a)} {unit}"
        rain = [x["w_rain24"] for x in lst if x["w_rain24"] is not None]
        out.append({"name": name, "n": len(lst), "hum": rng("w_hum", "%"), "press": rng("w_press", "hPa"),
                    "wind": rng("w_wind", "km/h"), "cloud": rng("w_cloud", "%"),
                    "rain": f"{round(100 * sum(1 for x in rain if x >= 1.0) / len(rain))} %" if rain else "?"})
        if len(out) >= 14:
            break
    return out, len(ok)


def years_chart(rows: list[dict]) -> dict:
    """Mittlerer Flugtag je Jahr für die häufigsten Arten (je Jahr mind. 3 Meldungen)."""
    years = sorted({r["flight_date"][:4] for r in rows})
    by: dict = {}
    for r in rows:
        if r["species"] and not r["qualifier"]:
            by.setdefault(_taxon_key(r), {}).setdefault(r["flight_date"][:4], []).append(_doy(r["flight_date"]))
    cands = []
    for name, ys in by.items():
        good = {y: v for y, v in ys.items() if len(v) >= 3}
        if len(good) >= 3:
            cands.append((sum(len(v) for v in good.values()), name, good))
    cands.sort(reverse=True)
    series = []
    for i, (_n, name, good) in enumerate(cands[:6]):
        series.append({"label": name, "color": PALETTE[i % len(PALETTE)],
                       "data": [round(_med(good[y])) if y in good else None for y in years]})
    return {"years": years, "series": series}


CMP_MIN = 5                                # so viele Meldungen braucht jedes Jahr für eine Aussage


def compare_years(rows: list[dict], years: list[str], q: dict, lang: str, t) -> dict:
    """Zwei Jahre nebeneinander: Saisonkurve als Anteil der Jahresmeldungen je Woche (die Community
    ist gewachsen, absolute Zahlen wären nicht vergleichbar) und Kennzahlen."""
    if len(years) < 2:
        return {}
    cnt: dict = {}
    for r in rows:
        cnt[_taxon_key(r)] = cnt.get(_taxon_key(r), 0) + 1
        cnt[r["genus"] + " *"] = cnt.get(r["genus"] + " *", 0) + 1
    opts = [k for k, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= 2 * CMP_MIN and " " in k][:60]
    tx = (q.get("yt") or "").strip()[:60]
    if tx not in opts:
        tx = ""
    ya = q.get("ya") if q.get("ya") in years else years[0]
    yb = q.get("yb") if q.get("yb") in years else next((y for y in years if y != ya), years[-1])

    def match(r):
        if not tx:
            return True
        if tx.endswith(" *"):
            return r["genus"] == tx[:-2]
        return _taxon_key(r) == tx
    weeks = list(range(14, 45))
    out = {"ya": ya, "yb": yb, "tx": tx, "opts": [{"v": k, "l": (k[:-2] + " (" + t("sw_cmp_genus") + ")")
                                                   if k.endswith(" *") else k} for k in opts],
           "years": years, "cols": [], "data": {"weeks": [f"KW {w}" for w in weeks]}}
    name = (tx[:-2] if tx.endswith(" *") else tx) if tx else ""
    meds = {}
    for y, dash in ((ya, False), (yb, True)):
        lst = [r for r in rows if r["flight_date"][:4] == y and match(r)]
        n = len(lst)
        per = [0] * len(weeks)
        for r in lst:
            w = _week(r["flight_date"])
            if w in weeks:
                per[weeks.index(w)] += 1
        share = [round(100 * c / n, 1) for c in per] if n else [0] * len(weeks)
        ds = sorted(r["flight_date"] for r in lst)
        temps = [v for v in (S.temp_of(r) for r in lst) if v is not None]
        peak = weeks[per.index(max(per))] if n and max(per) else None
        col = {"year": y, "n": n,
               "first": date.fromisoformat(ds[0]).strftime("%d.%m.") if ds else "?",
               "last": date.fromisoformat(ds[-1]).strftime("%d.%m.") if ds else "?",
               "peak": S.week_label(peak, lang) if peak else "?",
               "temp": f"≈ {sum(temps) / len(temps):.0f} °C" if temps else "?",
               "temp_n": len(temps)}
        out["cols"].append(col)
        out["data"].setdefault("sets", []).append({"label": y, "data": share, "dash": dash,
                                                   "color": PALETTE[0] if not dash else PALETTE[1]})
        meds[y] = (_med([_doy(d) for d in ds]), n)
    (ma, na), (mb, nb) = meds[ya], meds[yb]
    subj = name or ""
    if na < CMP_MIN or nb < CMP_MIN:
        out["say"] = t("sw_cmp_few", a=ya, b=yb, na=na, nb=nb, m=CMP_MIN)
    else:
        d = round(mb - ma)                     # > 0: Jahr A früher
        key = "same" if abs(d) <= 2 else ("earlier" if d > 0 else "later")
        out["say"] = t(f"sw_cmp_{'tx' if subj else 'all'}_{key}", x=subj, a=ya, b=yb, d=abs(d))
    out["data"]["t"] = {"axis": t("sw_cmp_axis")}
    return out


def _abroad_ctx(rows: list, g: str, inr, member: bool, link, lang: str) -> dict:
    """Kasten „Aus dem Urlaub“: Meldungen aus Ländern außerhalb DACH (nicht in der Auswertung)."""
    sel = [r for r in rows if (not g or r["genus"] == g) and inr(r["flight_date"])]
    if not sel:
        sel = [r for r in rows if not g or r["genus"] == g]
    seen, out = set(), []
    for r in sel:
        k = (r["message_id"], r["label"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"label": r["label"], "country_name": S.country_label(r["country"], lang),
                    "flight_date": r["flight_date"], "place": r["place"] if member else "",
                    "link": link(r["message_id"]) if member else ""})
    return {"abroad": out[:25], "abroad_more": max(0, len(out) - 25)}


WR_VARS = ("temp", "rain", "hum", "press", "wind")


def weather_regions(rows: list[dict]) -> tuple[dict, dict]:
    """Wetter beim Flug je Region: {key: [n, temp, rain %, hum, press, wind]} (Median, Regen = Anteil ab 1 mm).
    Regionen mit weniger als 3 Meldungen nur mit n (Werte None)."""
    by: dict = {}
    names: dict = {}
    for r in rows:
        if not (r["country"] and r["region"]):
            continue
        k = f"{r['country']}-{r['region']}"
        names[k] = S.region_name(r["country"], r["region"])
        by.setdefault(k, []).append(r)
    out = {}
    for k, lst in by.items():
        n = len(lst)
        if n < 3:
            out[k] = [n, None, None, None, None, None]
            continue
        def med(xs):
            xs = [x for x in xs if x is not None]
            return round(_med(xs), 1) if len(xs) >= 3 else None
        rain = [r["w_rain24"] for r in lst if r.get("w_rain24") is not None]
        out[k] = [n, med([S.temp_of(r) for r in lst]),
                  round(100 * sum(1 for x in rain if x >= 1) / len(rain)) if len(rain) >= 3 else None,
                  med([r.get("w_hum") for r in lst]), med([r.get("w_press") for r in lst]),
                  med([r.get("w_wind") for r in lst])]
    return out, names


async def climate_ctx(bot, ct: str, g: str, member: bool) -> dict:
    """Flugwetter-Kalender: Anteil der Tage mit gutem Flugwetter je Lernpunkt und Woche (alle Jahre)."""
    try:
        taxa = [r["taxon"] for r in await execute_db(bot,
            "SELECT c.taxon FROM fc_climate c LEFT JOIN fc_models m ON m.taxon=c.taxon GROUP BY c.taxon "
            "ORDER BY MAX(m.n_pos) DESC", fetch=True) or []]
    except Exception:
        taxa = []
    if not taxa:
        return {"ok": False}
    tx = ct if ct in taxa else (g if g in taxa else taxa[0])
    rows = await execute_db(bot,
        "SELECT c.key, c.week, c.days, c.good, p.lat, p.lon, p.kind FROM fc_climate c JOIN fc_points p "
        "ON p.key=c.key WHERE c.taxon=? AND c.week BETWEEN 14 AND 44", (tx,), fetch=True) or []
    pts: dict = {}
    for r in rows:
        if r["kind"] == "hot" and not member:
            continue
        e = pts.setdefault(r["key"], [r["lat"], r["lon"], 1 if r["kind"] == "hot" else 0, [None] * 31])
        e[3][r["week"] - 14] = round(100 * r["good"] / r["days"]) if r["days"] else None
    return {"ok": True, "taxa": taxa, "taxon": tx, "data": {"taxon": tx, "pts": list(pts.values())}}


async def progress(bot, t) -> dict:
    """Stand der Datensammlung (für den Kasten und /schwarmflug/status.json)."""
    from utils import forecast as FC
    from config import SWARM_OM_DAILY

    async def one(sql, params=()):
        r = await execute_db(bot, sql, params, fetch=True)
        return (r[0][0] or 0) if r else 0
    q_out = "indoor=0 AND country IN ('de','at','ch','li')"
    places_t = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0 AND place<>''")
    places_o = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0 AND place<>'' "
                         "AND lat IS NULL AND geo_src IS NULL")
    clar_t = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0")
    clar_o = await one("SELECT COUNT(DISTINCT message_id) FROM swarm_reports WHERE indoor=0 AND ("
                       "(country='' AND ((geo_src IS NULL AND place='') OR geo_src='none')) OR taxon_check='unknown')")
    wx_t = await one(f"SELECT COUNT(*) FROM swarm_reports WHERE {q_out} AND lat IS NOT NULL")
    wx_o = await one(f"SELECT COUNT(*) FROM swarm_reports WHERE {q_out} AND lat IS NOT NULL AND w_state IS NULL")
    try:
        pend = await FC.train_pending(bot)
        done = await one("SELECT COUNT(*) FROM fc_train_done")
    except Exception:
        pend, done = 0, 0
    bud = await FC.budget_today(bot)
    used = sum(v for k, v in bud.items() if k != "blocked")

    def bar(key, total, open_):
        d = max(0, total - open_)
        return {"key": key, "done": d, "total": total, "pct": round(100 * d / total) if total else 100}
    bars = [bar("places", places_t, places_o), bar("clarify", clar_t, clar_o), bar("weather", wx_t, wx_o),
            bar("learn", done + pend, pend)]
    cool = FC.cooling() if hasattr(FC, "cooling") else 0
    if getattr(FC, "_BLOCK_DAY", "") == FC._today():
        state = t("sw_prog_blocked")
    elif cool > 0:
        state = t("sw_prog_pause", m=int(cool // 60) + 1)
    elif any(b["done"] < b["total"] for b in bars):
        state = t("sw_prog_running")
    else:
        state = t("sw_prog_done")
    eta_txt = ""
    try:
        eta = await FC.learn_eta(bot) if hasattr(FC, "learn_eta") else None
    except Exception:
        eta = None
    if eta:
        dd = date.fromisoformat(eta["date"]).strftime("%d.%m.%Y")
        eta_txt = t("sw_prog_eta_today") if eta["days"] == 0 else t("sw_prog_eta", d=eta["days"], date=dd,
                                                                      n=eta["per_day"])
    elif wx_o:
        eta_txt = t("sw_prog_eta_wx", d=max(1, -(-wx_o // 1500)))
    return {"bars": bars, "state": state, "eta": eta_txt,
            "budget": t("sw_prog_budget", u=f"{used:,.0f}".replace(",", "."), m=f"{SWARM_OM_DAILY:,}".replace(",", "."))}


def season_info(lang: str) -> dict | None:
    r = S.season_now()
    if not r:
        return None
    return {**r, "where": S.region_name(r["country"], r["region"]) if r["region"] else "?"}


def regions_select(t, lang: str) -> list[tuple[str, str]]:
    from utils import geo
    names = {"de": "Deutschland", "at": "Österreich", "ch": "Schweiz", "li": "Liechtenstein"}
    out = [("", t("sw_id_anywhere"))]
    for c, regs in geo.all_regions().items():
        out.append((c, names.get(c, c.upper())))
        if c != "li":
            out += [(f"{c}-{k}", f"  · {n}") for k, n in sorted(regs.items(), key=lambda x: x[1])]
    return out


async def forecast_ctx(bot, member: bool, q: dict, t) -> dict:
    """Prognose-Abschnitt: Tage, Taxa, Regionsstufen, Liste, Zellen (nur Mitglieder), Trefferquote."""
    from config import SWARM_FORECAST
    from utils import forecast as FC
    if not SWARM_FORECAST:
        return {"show": False}
    try:
        days = await FC.pred_days(bot)
        mods = await FC.models(bot)
    except Exception:
        return {"show": False}
    quality = []
    try:
        prev = await FC.previous_quality(bot)
    except Exception:
        prev = {}

    def _lift(m):
        return (m["hit_hi"] / m["hit_base"]) if m and m.get("hit_hi") is not None and m.get("hit_base") else None
    for name, m in sorted(mods.items(), key=lambda x: -(x[1].get("n_pos") or 0)):
        if m["kind"] != "model":
            continue
        lift, pl = _lift(m), _lift(prev.get(name))
        quality.append({"taxon": name, "n_pos": m.get("n_pos") or 0,
                        "prev": t("fc_q_times", x=f"{pl:.1f}".replace(".", ",")) if pl else "",
                        "lift": t("fc_q_times", x=f"{lift:.1f}".replace(".", ",")) if lift else "?",
                        "auc": f"{m['auc']:.2f}".replace(".", ",") if m.get("auc") is not None else "?"})
    rules = sum(1 for m in mods.values() if m["kind"] == "rule")
    try:
        pending = await FC.train_pending(bot)
    except Exception:
        pending = 0
    out = {"show": bool(mods), "active": bool(days), "days": days, "quality": quality[:20], "rules": rules,
           "pending": pending, "di": 0, "taxon": "", "taxa": [], "list": [], "run_at": ""}
    if not days:
        return out
    di = 1 if str(q.get("fd")) == "1" and len(days) > 1 else 0
    day = days[di]
    rows = await FC.by_region(bot, day, 1)
    taxa = sorted({r["taxon"] for r in rows}, key=lambda x: (" " not in x, x))
    tx = (q.get("ft") or "")[:60]
    if tx not in taxa:
        tx = ""
    sel = [r for r in rows if not tx or r["taxon"] == tx]
    reg: dict = {}
    info: dict = {}
    for r in sel:
        k = f"{r['country']}-{r['region']}"
        if r["level"] > reg.get(k, 0):
            reg[k] = r["level"]
        info.setdefault(k, []).append((r["level"], r["p"], r["taxon"]))
    reg_info = {k: ", ".join(f"{n} ({t('fc_lv_' + str(lv))})" for lv, _p, n in sorted(v, reverse=True)[:4])
                for k, v in info.items()}
    cells = []
    if member:
        best: dict = {}
        for c in await FC.cells(bot, day, tx or None):
            k = (c["lat"], c["lon"])
            if k not in best or c["level"] > best[k]["level"]:
                best[k] = c
        for c in best.values():
            win = t("fc_hours", a=c["h_from"], b=c["h_to"]) if c["h_from"] is not None else ""
            cells.append([c["lat"], c["lon"], c["level"], c["kind"], c["taxon"],
                          " · ".join(x for x in (t("fc_lv_" + str(c["level"])), win,
                                                 f"≈ {c['temp']:.0f} °C" if c["temp"] is not None else "") if x)])
    ra = await execute_db(bot, "SELECT MAX(run_at) AS r FROM fc_pred", fetch=True)
    run_at = ""
    if ra and ra[0]["r"]:
        from utils.timez import berlin_from_utc_naive
        run_at = berlin_from_utc_naive(ra[0]["r"], "%Y-%m-%d %H:%M:%S", "%d.%m. %H:%M")
    out.update(di=di, taxon=tx, taxa=taxa[:60], list=FC.condense(sel, 10, 5), run_at=run_at,
               data={"reg": reg, "regInfo": reg_info, "cells": cells})
    return out


async def page(bot, z: str, g: str, member: bool, lang: str, art_slug, month_names, t,
               q: dict | None = None) -> dict:
    """Kontext für das Template. art_slug(name) -> Slug oder None (Art im Verzeichnis?)."""
    from config import SWARM_CHANNEL_ID, SWARM_OSM_GEOCODING
    today = datetime.now(BERLIN).date()
    all_rows = await S.rows(bot, include_indoor=True)
    outdoor = [r for r in all_rows if not r["indoor"]]
    genera_cnt: dict = {}
    for r in outdoor:
        genera_cnt[r["genus"]] = genera_cnt.get(r["genus"], 0) + 1
    genera = sorted(genera_cnt, key=lambda x: -genera_cnt[x])
    if g and g not in genera_cnt:
        g = ""
    year_list = sorted({r["flight_date"][:4] for r in outdoor}, reverse=True)
    if z not in RANGES and z not in year_list:
        season_n = sum(1 for r in outdoor if r["flight_date"] >= (_since("saison", today) or ""))
        z = "saison" if season_n >= 20 else "alle"
    since = _since(z, today) if z in RANGES else f"{z}-01-01"
    until = f"{z}-12-31" if z in year_list else None

    def inr(d: str) -> bool:
        return (not since or d >= since) and (not until or d <= until)
    base = [r for r in outdoor if not g or r["genus"] == g]
    sel = [r for r in base if inr(r["flight_date"])]

    # Regionen (Karte)
    regions, names, top = {}, {}, {}
    per_reg: dict = {}
    for r in sel:
        if r["country"] and r["region"]:
            k = f"{r['country']}-{r['region']}"
            regions[k] = regions.get(k, 0) + 1
            names[k] = S.region_name(r["country"], r["region"])
            per_reg.setdefault(k, {}).setdefault(r["label"], 0)
            per_reg[k][r["label"]] += 1
    for k, d in per_reg.items():
        top[k] = ", ".join(f"{n} ({c})" for n, c in sorted(d.items(), key=lambda x: -x[1])[:3])

    # Farben je Gattung (Top 6 der Auswahl, Rest grau)
    gtop = genera[:6] if not g else [g]
    gcol = {gg: PALETTE[i % len(PALETTE)] for i, gg in enumerate(gtop)}

    guild_id = None
    ch = bot.get_channel(SWARM_CHANNEL_ID) if SWARM_CHANNEL_ID and hasattr(bot, "get_channel") else None
    guild_id = getattr(getattr(ch, "guild", None), "id", None)

    def link(mid):
        return f"https://discord.com/channels/{guild_id}/{SWARM_CHANNEL_ID}/{mid}" if guild_id else ""

    pins = []
    if member:
        for r in sel:
            if r["lat"] is not None and r["lon"] is not None:
                when = date.fromisoformat(r["flight_date"]).strftime("%d.%m.%Y") + \
                    (f" · {r['flight_time']}" if r["flight_time"] else "") + \
                    (f" · {r['temp_c']:g} °C" if r["temp_c"] is not None else
                     (f" · ≈ {r['w_temp']:.0f} °C" if r.get("w_temp") is not None else ""))
                pins.append([r["lat"], r["lon"], r["genus"], r["label"], when, r["place"], link(r["message_id"]),
                             _week(r["flight_date"])])

    # Jetzt gerade (48 h)
    fly: dict = {}
    since48 = (today - timedelta(days=2)).isoformat()
    for r in base:
        if r["flight_date"] >= since48:
            e = fly.setdefault(r["label"], {"label": r["label"], "n": 0, "regions": set()})
            e["n"] += 1
            if r["region"]:
                e["regions"].add(S.region_name(r["country"], r["region"]))
    flying = sorted(fly.values(), key=lambda x: -x["n"])[:12]
    for f in flying:
        f["regions"] = sorted(f["regions"])[:4]

    # Saisonverlauf (alle Jahre, je Gattung bzw. je Art der gewählten Gattung)
    weeks = list(range(14, 45))
    groups: dict = {}
    for r in base:
        key = r["genus"] if not g else _taxon_key(r)
        groups.setdefault(key, []).append(r)
    series = []
    for i, (key, lst) in enumerate(sorted(groups.items(), key=lambda x: -len(x[1]))[:6]):
        cnt = [0] * len(weeks)
        years = {r["flight_date"][:4] for r in lst}
        for r in lst:
            w = _week(r["flight_date"])
            if w in weeks:
                cnt[weeks.index(w)] += 1
        n_years = max(1, len(years))
        series.append({"label": key, "color": PALETTE[i % len(PALETTE)],
                       "data": [round(c / n_years, 2) for c in cnt]})
    week_titles = [f"KW {w} · {S.week_label(w, lang)}" for w in weeks]
    week_ticks = []
    last_m = None
    for w in weeks:
        m = date.fromisocalendar(2025, w, 3).month
        week_ticks.append(month_names[m - 1][:3] if m != last_m else "")
        last_m = m

    hours = [0] * 24
    for r in base:
        if r["flight_time"]:
            hours[int(r["flight_time"][:2])] += 1
    lo = next((i for i, v in enumerate(hours) if v), 6)
    hi = 23 - next((i for i, v in enumerate(reversed(hours)) if v), 1)
    hrs = {"labels": [f"{h}" for h in range(lo, hi + 1)], "data": hours[lo:hi + 1]}
    tb = list(range(5, 41, 3))
    tcnt = [0] * len(tb)
    for r in base:
        tv = S.temp_of(r)
        if tv is not None:
            for i, b in enumerate(tb):
                if b <= tv < b + 3:
                    tcnt[i] += 1
    temps = {"labels": [f"{b}–{b + 2}" for b in tb], "data": tcnt}

    # Steckbriefe (alle Jahre)
    summ = S.summarize(base)
    taxa = []
    for x in summ["taxa"][:40]:
        win = f"{S.week_label(x['w_from'], lang)} {t('sw_to')} {S.week_label(x['w_to'], lang)}" if x["w_from"] else "?"
        peak = S.week_label(x["w_peak"], lang) if x["w_peak"] else "?"
        hours_s = (f"{x['h_from']}–{x['h_to']} {t('sw_oclock')}" if x["h_from"] is not None and x["h_from"] != x["h_to"]
                   else (f"≈ {x['hour']} {t('sw_oclock')}" if x["hour"] is not None else ""))
        temps_s = (f"{x['t_from']}–{x['t_to']} °C" if x["t_from"] is not None and x["t_from"] != x["t_to"]
                   else (f"≈ {x['temp']} °C" if x["temp"] is not None else ""))
        mx = max(x["months"]) or 1
        taxa.append({"name": x["name"], "n": x["n"], "win": win, "peak": peak,
                     "months": [m / mx for m in x["months"]], "hours": hours_s, "temps": temps_s,
                     "regions": [n for n, _ in x["regions"]],
                     "art": art_slug(x["name"]) if " " in x["name"] else None})

    abroad_all = await S.rows(bot, include_indoor=True, scope="abroad")
    unclear_n = len({r["message_id"] for r in await S.rows(bot, include_indoor=True, scope="unclear")
                     if not r["indoor"]}) if member else 0   # wie die Admin-Seite: ohne Zimmerflüge
    pool = sorted([r for r in all_rows + abroad_all if not g or r["genus"] == g],
                  key=lambda r: (r["flight_date"], r["flight_time"] or ""), reverse=True)
    inside = [r for r in pool if inr(r["flight_date"])]
    recent = []
    for r in (inside[:40] if inside else pool[:15]):
        recent.append({**r, "abroad": r["country"] not in S.DACH,
                       "region_name": S.region_name(r["country"], r["region"]) if r["country"] else "",
                       "link": link(r["message_id"]) if member else "", "place": r["place"] if member else ""})

    k = {"total": len(sel), "taxa": len({_taxon_key(r) for r in sel if r["species"]}),
         "genera": len({r["genus"] for r in sel}), "regions": len(regions),
         "reporters": (lambda n: "< 3" if 0 < n < 3 else n)(len({r["author_id"] for r in sel if r["author_id"]}))}
    legend = [(c, lbl) for c, lbl in zip(["#1b2a1f", "#1f4d2c", "#2a7a3e", "#3fb950", "#a6f0b5"],
                                         ["1–2", "3–8", "9–20", "21–50", "> 50"])]
    data = {"regions": regions, "names": names, "top": top, "pins": pins, "gcol": gcol,
            "series": series, "weeks": [f"KW {w}" for w in weeks], "weekTitles": week_titles,
            "weekTicks": week_ticks, "hours": hrs, "temps": temps,
            "t": {"reports": t("sw_kpi_reports"), "perWeek": t("sw_per_week")}, "lang": lang}
    # Zeitraffer: Meldungen je Region und Kalenderwoche (Auswahl)
    tl: dict = {}
    for r in sel:
        if r["country"] and r["region"]:
            w = _week(r["flight_date"])
            if w in weeks:
                kk = f"{r['country']}-{r['region']}"
                tl.setdefault(w, {}).setdefault(kk, 0)
                tl[w][kk] += 1
    data.update(tl=tl, wkNum=weeks, yrs=years_chart(base))
    data["t"].update(tlAll=t("sw_tl_all"), tlPlay=t("sw_tl_play"), tlPause=t("sw_tl_pause"),
                     median=t("sw_years_axis"))
    # Bestimmungshilfe
    q = q or {}
    ident = {"asked": False, "date": today.isoformat(), "hour": None, "region": "", "temp": None, "cands": []}
    try:
        if q.get("wd"):
            ident["date"] = date.fromisoformat(q["wd"][:10]).isoformat()
            ident["asked"] = True
        if (q.get("wt") or "").isdigit():
            ident["hour"] = max(0, min(23, int(q["wt"])))
        if q.get("wtemp") not in (None, ""):
            ident["temp"] = max(-5.0, min(45.0, float(str(q["wtemp"]).replace(",", "."))))
    except ValueError:
        pass
    ident["region"] = (q.get("wr") or "")[:8]
    if ident["asked"]:
        ident["cands"] = identify(outdoor, date.fromisoformat(ident["date"]), ident["hour"], ident["region"],
                                  ident["temp"], t)
        for c in ident["cands"]:
            c["art"] = art_slug(c["name"])
    cmp = compare_years(base, year_list, q, lang, t)
    if cmp:
        data["cmp"] = cmp.pop("data")
    wx, wx_n = weather_table(base, t)
    from utils import forecast as FC
    try:
        data["learn"] = [p for p in await FC.learn_map(bot) if member or not p[2]]   # Hotspots nur für Mitglieder
    except Exception:
        data["learn"] = []
    data["wreg"], wreg_names = weather_regions(base)
    data["names"] = {**wreg_names, **data["names"]}
    clim = await climate_ctx(bot, (q or {}).get("ct", ""), g, member)
    if clim.get("data"):
        data["clim"] = clim.pop("data")
    data["t"].update(lrnHot=t("sw_lrn_hot"), lrnGrid=t("sw_lrn_grid"), lrnOf=t("sw_lrn_of"),
                     lrnFine=t("sw_lrn_fine"), wrVars={k: t("sw_wr_" + k) for k in WR_VARS},
                     wrFew=t("sw_wr_few"), wrN=t("sw_wr_n"), clDays=t("sw_cl_days"))
    prog = await progress(bot, t)
    fc = await forecast_ctx(bot, member, q, t)
    if fc.get("data"):
        data["fc"] = fc.pop("data")
    data["t"]["fcNone"] = t("fc_region_none")
    return {"z": z, "g": g, "ranges": RANGES, "genera": genera, "flying": flying, "k": k, "data": data,
            "ident": ident, "regions_sel": regions_select(t, lang), "wx": wx, "wx_n": wx_n, "cmp": cmp,
            "season": season_info(lang), "fc": fc, "prog": prog, "clim": clim,
            **_abroad_ctx(abroad_all, g, inr, member, link, lang), "unclear_n": unclear_n,
            "year_list": year_list, "home_n": len({r["message_id"] for r in sel if r.get("geo_src") == "home"}),
            "recent": recent, "taxa": taxa, "legend": legend, "member": member,
            "first_year": (min(r["flight_date"][:4] for r in all_rows) if all_rows else str(today.year)),
            "last_date": max((r["flight_date"] for r in outdoor), default=""),
            "season_year": today.year if today.month >= 3 else today.year - 1,
            "month_names": month_names,
            "osm": bool(SWARM_OSM_GEOCODING and any(r["geo_src"] == "osm" for r in all_rows))}


async def art_box(bot, name: str, lang: str = "de") -> dict | None:
    """Kasten „Schwarmflüge in der Community“ auf der Artenseite."""
    parts = name.split()
    if len(parts) < 2:
        return None
    gen, sp = parts[0], parts[1].lower()
    try:
        rs = [dict(r) for r in await execute_db(bot,
            "SELECT * FROM swarm_reports WHERE genus=? AND indoor=0 AND country IN ('de','at','ch','li')",
            (gen,), fetch=True) or []]
    except Exception:
        return None
    mine = [r for r in rs if r["species"] == sp]
    if not mine:
        return {"n": 0, "genus_n": len(rs), "genus": gen} if rs else None
    s = S.summarize([{**r, "species": sp} for r in mine])
    x = s["taxa"][0] if s["taxa"] else None
    months = [0] * 12
    for r in mine:
        months[int(r["flight_date"][5:7]) - 1] += 1
    mx = max(months) or 1
    return {"n": len(mine), "genus_n": len(rs), "genus": gen, "months": [m / mx for m in months],
            "years": len({r["flight_date"][:4] for r in mine}),
            "last": max(r["flight_date"] for r in mine),
            "peak": S.week_label(x["w_peak"], lang) if x else None,
            "win": (f"{S.week_label(x['w_from'], lang)} {dict(de='bis', en='to', eo='ĝis').get(lang, 'bis')} {S.week_label(x['w_to'], lang)}" if x and x["w_from"] else None),
            "hours": (f"{x['h_from']}–{x['h_to']}" if x and x["h_from"] is not None else None),
            "temps": (f"{x['t_from']}–{x['t_to']}" if x and x["t_from"] is not None else None),
            "regions": [n for n, _ in x["regions"]] if x else []}


ART_BOX = """{% if swarm %}<div class=chartbox><h4>🪽 {{ t('sw_art_h') }}</h4>
{% if swarm.n %}<p style="margin:0 0 8px">{{ t('sw_art_line', n=swarm.n, y=swarm.years) }}{% if swarm.win %} {{ t('sw_art_win', w=swarm.win) }}{% endif %}{% if swarm.peak %} {{ t('sw_art_peak', p=swarm.peak) }}{% endif %}</p>
<div style="display:grid;grid-template-columns:repeat(12,1fr);gap:4px;align-items:end;height:50px">{% for m in swarm.months %}<div style="background:{{ '#3fb950' if m >= 0.5 else ('#3fb95055' if m > 0 else '#21262d') }};height:{{ (4 + m * 46)|int }}px;border-radius:3px"></div>{% endfor %}</div>
<div style="display:grid;grid-template-columns:repeat(12,1fr);gap:4px;font-size:11px;text-align:center" class=muted>{% for n in (season.short if season else sw_short) %}<div>{{ n }}</div>{% endfor %}</div>
<p class=muted style="margin:8px 0 0;font-size:13px">{% if swarm.hours %}🕑 {{ swarm.hours }} {{ t('sw_oclock') }} · {% endif %}{% if swarm.temps %}🌡️ {{ swarm.temps }} °C · {% endif %}{% if swarm.regions %}🗺️ {{ swarm.regions|join(', ') }} · {% endif %}{{ t('sw_art_last', d=swarm.last|dmy) }}</p>
{% else %}<p style="margin:0">{{ t('sw_art_genus_only', g=swarm.genus, n=swarm.genus_n) }}</p>{% endif %}
<p style="margin:8px 0 0"><a href="/schwarmflug?g={{ swarm.genus }}&lang={{ lang }}">{{ t('sw_art_more') }} →</a></p></div>{% endif %}"""
