/* SPDX-License-Identifier: AGPL-3.0-or-later
   Halter-Karte – ausklappbare Statistiken (/map/stats.json).
   Unabhängig von Leaflet: funktioniert auch, wenn die Kartenbibliothek fehlt.
   Öffentlich nur Summen (kleine Zahlen serverseitig als "< 3" maskiert),
   Mitglieder zusätzlich Tags, Umkreis, Kontakt, Teilnahme, Regionen ohne Halter. */
(function () {
  "use strict";
  var CFG = window.MAP_CFG || { lang: "de" };
  var S = CFG.st || {};
  var box = document.getElementById("statsbox");
  var body = document.getElementById("statsbody");
  if (!box || !body) return;

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function L(key, vars) {
    var t = S[key] || key;
    if (vars) Object.keys(vars).forEach(function (k) { t = t.split("{" + k + "}").join(String(vars[k])); });
    return t;
  }
  var loc = CFG.lang === "en" ? "en-GB" : "de-DE";
  function fmtDate(iso, allDay) {
    try {
      var o = { timeZone: "Europe/Berlin", weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" };
      if (!allDay) { o.hour = "2-digit"; o.minute = "2-digit"; }
      return new Date(iso).toLocaleString(loc, o);
    } catch (e) { return String(iso || "").slice(0, 10); }
  }
  function monthLabel(ym) {
    try { return new Date(ym + "-01T12:00:00Z").toLocaleString(loc, { month: "short" }); }
    catch (e) { return ym.slice(5); }
  }
  // Balkenzeile: Label | Balken | Wert (Wert immer als Text -> nicht nur Farbe/Länge)
  function bar(label, value, frac, extra) {
    var w = Math.max(0, Math.min(1, frac || 0)) * 100;
    return "<div class=sbar><span class=lb title='" + esc(label) + "'>" + esc(label) + "</span>" +
           "<span class=tr aria-hidden=true><span class=fi style='display:block;width:" + w.toFixed(1) + "%'></span></span>" +
           "<span class=vl>" + esc(value) + (extra ? " · " + esc(extra) : "") + "</span></div>";
  }
  function section(title, inner, note) {
    return "<section><h5>" + esc(title) + "</h5>" + (note ? "<p class=stnote>" + esc(note) + "</p>" : "") + inner + "</section>";
  }
  function none() { return "<p class=muted style='font-size:12px'>" + esc(L("map_st_none")) + "</p>"; }

  function render(d) {
    var t = d.totals || {}, h = "";
    // Kopfzeile
    h += "<div class=stk>" +
      "<div><b>" + esc(t.keepers) + "</b><span>" + esc(L("map_st_keepers")) + "</span></div>" +
      "<div><b>" + esc(t.countries) + "</b><span>" + esc(L("map_st_countries")) + "</span></div>" +
      "<div><b>" + esc(t.regions) + "</b><span>" + esc(L("map_st_regions")) + "</span></div>" +
      "<div><b>" + esc(t.events) + "</b><span>" + esc(L("map_st_events")) + "</span></div></div>";

    var sec = [];
    // Top-Regionen
    var top = d.top_regions || [];
    var tmax = Math.max.apply(null, [1].concat(top.map(function (e) { return e.n || 0; })));
    sec.push(section(L("map_st_top"), top.length ? top.map(function (e) {
      return bar(e.name, e.count, (e.n || 0.5) / tmax);
    }).join("") : none()));

    // Wachstum (Säulen)
    var g = d.growth || [];
    var gmax = Math.max.apply(null, [1].concat(g.map(function (e) { return e.count; })));
    var gsum = g.reduce(function (a, e) { return a + e.count; }, 0);
    var cols = "<div class=scol role=img aria-label='" + esc(g.map(function (e) { return monthLabel(e.month) + ": " + e.count; }).join(", ")) + "'>" +
      g.map(function (e) {
        return "<div title='" + esc(monthLabel(e.month) + " " + e.month.slice(0, 4) + ": " + e.count) + "' style='height:" +
               (100 * e.count / gmax).toFixed(1) + "%'></div>";
      }).join("") + "</div><div class=scolx aria-hidden=true>" +
      g.map(function (e) { return "<span>" + esc(monthLabel(e.month).slice(0, 3)) + "</span>"; }).join("") + "</div>";
    sec.push(section(L("map_st_growth"), gsum ? cols : none(), L("map_st_growth_note")));

    // Ländervergleich
    var cs = (d.countries || []).filter(function (c) { return c.count !== 0; });
    sec.push(section(L("map_st_ctry"), cs.length ? cs.map(function (c) {
      return bar(c.country === "intl" ? L("map_st_intl") : c.name, c.count, c.share == null ? 0.02 : c.share / 100, c.share == null ? "" : c.share + " %");
    }).join("") : none()));

    // Termine nach Art + nächster Termin
    var labels = CFG.evlabels || {};
    var et = d.events_by_type || [];
    var emax = Math.max.apply(null, [1].concat(et.map(function (e) { return e.count; })));
    var evh = et.map(function (e) { return bar(labels[e.type] || e.type, e.count, e.count / emax); }).join("");
    if (d.next_event) {
      var ne = d.next_event;
      evh += "<p style='font-size:12px;margin:8px 0 0'><b>" + esc(L("map_st_next")) + ":</b> " + esc(ne.title) +
             "<br><span class=muted>" + esc(fmtDate(ne.next, ne.all_day)) + (ne.venue ? " · " + esc(ne.venue) : "") + "</span></p>";
    }
    sec.push(section(L("map_st_evtypes"), evh || none()));

    h += "<div class=stgrid>" + sec.join("") + "</div>";

    if (!d.member) {
      h += "<p class=muted style='font-size:12px;margin-top:10px'>🔑 " + esc(L("map_st_login")) + "</p>";
    } else {
      var ms = [];
      // Tags
      var tg = d.tags || [];
      ms.push(section(L("map_st_tags"), tg.length ? tg.map(function (e) {
        return bar(e.label, e.pct + " %", e.pct / 100, e.count);
      }).join("") : none(), L("map_st_tags_note")));
      // Umkreis
      var nb = d.nearby;
      var nmax = nb ? Math.max.apply(null, [1].concat(nb.map(function (e) { return e.count; }))) : 1;
      ms.push(section(L("map_st_near"), nb ? nb.map(function (e) {
        var lbl = e.from ? L("map_st_band", { a: e.from, b: e.to }) : L("map_st_within", { km: e.to });
        return bar(lbl, e.count, e.count / nmax);
      }).join("") : "<p class=muted style='font-size:12px'>" + esc(L("map_st_near_none")) + "</p>",
        nb ? L("map_st_near_note") : ""));
      // Kontakt
      var ct = d.contact || {};
      var ch = "<p style='font-size:12px;margin:0'>" + esc(L("map_st_contact_all", { n: ct.count || 0, total: d.visible || 0, pct: ct.pct || 0 })) + "</p>";
      if (ct.region) ch += "<p style='font-size:12px;margin:4px 0 0'>" +
        esc(L("map_st_contact_reg", { region: ct.region.name, n: ct.region.count, total: ct.region.keepers })) + "</p>";
      ms.push(section(L("map_st_contact"), ch));
      // Teilnahme
      var te = d.top_events || [], my = d.my_events || [];
      var tmx = Math.max.apply(null, [1].concat(te.map(function (e) { return e.count; })));
      var th = te.length ? te.map(function (e) {
        return bar(e.title + " (" + fmtDate(e.next, true) + ")", e.count, e.count / tmx);
      }).join("") : none();
      th += "<h5 style='margin-top:10px'>" + esc(L("map_st_myev")) + "</h5>" + (my.length
        ? "<ul class=stlist>" + my.map(function (e) { return "<li>" + esc(e.title) + " – <span class=muted>" + esc(fmtDate(e.next, e.all_day)) + "</span></li>"; }).join("") + "</ul>"
        : "<p class=muted style='font-size:12px'>" + esc(L("map_st_myev_none")) + "</p>");
      ms.push(section(L("map_st_topev"), th));
      // Weiße Flecken
      var em = d.empty_regions || [];
      var byC = {};
      em.forEach(function (e) { (byC[e.country] = byC[e.country] || []).push(e.name); });
      var eh = em.length ? Object.keys(byC).map(function (c) {
        return "<p style='font-size:12px;margin:3px 0'><b>" + esc(c.toUpperCase()) + ":</b> " + esc(byC[c].join(", ")) + "</p>";
      }).join("") : "<p style='font-size:12px'>" + esc(L("map_st_empty_none")) + "</p>";
      ms.push(section(L("map_st_empty"), eh, em.length ? L("map_st_empty_note") : ""));

      h += "<h4 style='margin:16px 0 8px'>👥 " + esc(L("map_st_members")) + "</h4><div class=stgrid>" + ms.join("") + "</div>";
    }
    h += "<p class=stpriv>" + esc(L("map_st_privacy", { min: d.min || 3 })) + "</p>";
    body.innerHTML = h;
  }

  var loaded = false;
  function load() {
    if (loaded) return;
    loaded = true;
    fetch("/map/stats.json", { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(render)
      .catch(function () {
        loaded = false;
        body.innerHTML = "<p class=muted>" + esc(L("map_st_error")) + "</p>";
      });
  }
  // Auf-/Zu-Zustand pro Browser merken (nur Komfort; ohne Storage einfach zu).
  try { if (window.localStorage.getItem("mapStatsOpen") === "1") box.open = true; } catch (e) {}
  box.addEventListener("toggle", function () {
    try { window.localStorage.setItem("mapStatsOpen", box.open ? "1" : "0"); } catch (e) {}
    if (box.open) load();
  });
  if (box.open) load();
})();
