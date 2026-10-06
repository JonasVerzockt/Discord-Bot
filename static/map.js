/* SPDX-License-Identifier: AGPL-3.0-or-later
   Halter-Karte – Front-End. Leaflet rein als Vektor/GeoJSON, keine Tiles.
   Degradiert sauber, wenn Leaflet oder GeoJSON-Dateien fehlen. */
(function () {
  "use strict";
  var CFG = window.MAP_CFG || { lang: "de", member: false, maxZoom: 10 };
  var notice = document.getElementById("mapnotice");
  function note(msg) { if (notice) notice.textContent = msg; }
  // Nutzerdaten (Namen, Tags, Event-Texte) immer maskieren, bevor sie als HTML landen.
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || "")) ? esc(u) : ""; }

  if (typeof L === "undefined") {
    note(CFG.lang === "en" ? "Map library not loaded yet (the bot downloads it automatically)."
                           : "Kartenbibliothek noch nicht geladen (der Bot lädt sie automatisch herunter).");
    return;
  }

  var map = L.map("map", { worldCopyJump: false, maxZoom: CFG.maxZoom, minZoom: 4 })
    .setView([49.5, 9.5], 5);

  var choroLayer = null, pinLayer = null, eventLayer = null;
  var pinByRef = {};        // ref -> Leaflet-Marker (Pin ↔ Listenzeile)
  // Farbe je Event-Typ (muss zu map_evtype_* / board_i18n passen).
  var EVENT_COLORS = { fair: "#3fb950", meetup: "#58a6ff", shop: "#e3833b",
                       talk: "#a371f7", field: "#d29922", other: "#8b949e" };
  // Halter-Pins: genau (PLZ, gefuzzt) = blau, grobes PLZ-Gebiet = pink.
  var PIN_STYLE = { exact:  { color: "#1f6feb", fillColor: "#58a6ff" },
                    coarse: { color: "#bf4b8a", fillColor: "#f778ba" } };
  var PL = CFG.pinlabels || { exact: "PLZ", coarse: "PLZ-Gebiet", coarseNote: "PLZ-Gebiet", contact: "Kontakt über Discord" };
  var regionData = { bundesland: [], plz: [], countries: [] };
  var curLevel = "bundesland";
  var geojsonCache = {};

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error(url + " " + r.status);
      return r.json();
    });
  }

  function countFor(level, feat) {
    // Versucht gängige Property-Namen für Regions-/PLZ-Codes zu matchen.
    var p = feat.properties || {};
    var keys = level === "plz"
      ? ["plz", "plz_prefix", "PLZ", "postcode", "code"]
      : ["region_code", "shapeName", "shapeISO", "AGS", "GEN", "name", "NAME_1", "id", "code"];
    var val = null;
    for (var i = 0; i < keys.length; i++) { if (p[keys[i]] != null) { val = String(p[keys[i]]); break; } }
    if (val == null) return 0;
    var arr = regionData[level] || [];
    for (var j = 0; j < arr.length; j++) {
      var rc = level === "plz" ? arr[j].plz_prefix : arr[j].region_code;
      if (rc && (val === rc || val.indexOf(rc) === 0 || rc.indexOf(val) === 0)) return arr[j].count;
    }
    return 0;
  }

  function color(n) {
    return n > 20 ? "#238636" : n > 10 ? "#2ea043" : n > 5 ? "#3fb950"
         : n > 2 ? "#56d364" : n > 0 ? "#7ee787" : "#30363d";
  }

  function drawChoropleth() {
    if (choroLayer) { map.removeLayer(choroLayer); choroLayer = null; }
    // PLZ-Ebene: Blasen je PLZ-Gebiet (Zentroide aus GeoNames) – keine Polygon-Datei nötig.
    if (curLevel === "plz") {
      var group = L.layerGroup();
      var arr = regionData.plz || [];
      if (!arr.length) {
        note(CFG.lang === "en" ? "No postcode data yet (geodata still loading)."
                               : "Noch keine PLZ-Daten (Geodaten werden geladen).");
      }
      arr.forEach(function (e) {
        if (e.lat == null || e.lon == null) return;
        var r = 4 + Math.min(e.count, 24) * 0.5;
        var m = L.circleMarker([e.lat, e.lon], { radius: r, color: "#0d1117", weight: 0.8,
                                                 fillColor: color(e.count), fillOpacity: 0.75 });
        var xs = (e.country === "de") ? "xxx" : "xx";
        m.bindPopup("<b>PLZ " + esc(e.plz_prefix) + xs + "</b><br>" + e.count + " " +
                    (CFG.lang === "en" ? "keepers" : "Halter"));
        m.addTo(group);
      });
      group.addTo(map); choroLayer = group; return;
    }
    // Bundesland/Kanton-Ebene: echte Polygone (geoBoundaries).
    var files = ["/static/de_bundeslaender.geojson", "/static/at_bundeslaender.geojson",
                 "/static/ch_kantone.geojson", "/static/li_land.geojson"];
    var group = L.layerGroup();
    var loaded = 0, attempted = files.length;
    files.forEach(function (f) {
      var p = geojsonCache[f] ? Promise.resolve(geojsonCache[f]) : getJSON(f).then(function (g) { geojsonCache[f] = g; return g; });
      p.then(function (gj) {
        loaded++;
        L.geoJSON(gj, {
          style: function (feat) {
            var n = countFor(curLevel, feat);
            return { color: "#21262d", weight: 1, fillColor: color(n), fillOpacity: 0.55 };
          },
          onEachFeature: function (feat, layer) {
            var n = countFor(curLevel, feat);
            var nm = (feat.properties && (feat.properties.region_name || feat.properties.shapeName ||
                      feat.properties.GEN || feat.properties.name || feat.properties.NAME_1)) || "";
            layer.bindPopup("<b>" + esc(nm) + "</b><br>" + n + " " + (CFG.lang === "en" ? "keepers" : "Halter"));
          }
        }).addTo(group);
      }).catch(function () { /* Datei fehlt -> ignorieren */ })
        .then(function () {
          if (loaded === 0 && --attempted === 0)
            note(CFG.lang === "en" ? "Region outlines (GeoJSON) not installed yet – showing counts in the list."
                                   : "Regions-Umrisse (GeoJSON) noch nicht hinterlegt – Zahlen stehen in der Liste.");
        });
    });
    group.addTo(map);
    choroLayer = group;
  }

  function drawPins() {
    if (!CFG.member) return;
    getJSON("/map/pins.json").then(function (d) {
      if (pinLayer) { map.removeLayer(pinLayer); }
      pinLayer = L.layerGroup();
      pinByRef = {};
      (d.pins || []).forEach(function (p) {
        var st = p.coarse ? PIN_STYLE.coarse : PIN_STYLE.exact;
        var m = L.circleMarker([p.lat, p.lon], { radius: 7, color: st.color, fillColor: st.fillColor, fillOpacity: 0.9, weight: 2 });
        var tags = (p.tags && p.tags.length) ? "<br><span style='color:#8b949e'>" + esc(p.tags.join(", ")) + "</span>" : "";
        var contact = p.contact ? ("<br><i>" + esc(PL.contact) + "</i>") : "";
        var area = p.coarse ? ("<br><span style='color:#f778ba'>" + esc(PL.coarseNote) + "</span>") : "";
        m.bindPopup("<b>" + esc(p.name) + "</b><br>" + esc(p.region || p.country) + area + tags + contact);
        if (p.ref) { pinByRef[p.ref] = m; m.on("click", function () { highlightRow(p.ref); }); }
        m.addTo(pinLayer);
      });
      pinLayer.addTo(map);
    }).catch(function () {});
  }

  function renderList() {
    var box = document.getElementById("maplist");
    if (!box) return;
    getJSON("/map/list.json").then(function (d) {
      if (!d.member) {
        var h = "<p class=muted>" + (CFG.lang === "en"
          ? "Log in to see individual keepers. Public overview by country:"
          : "Nach Login siehst du einzelne Halter. Öffentliche Übersicht je Land:") + "</p>";
        (d.counts || []).forEach(function (c) {
          h += "<div class=mrow><span class=nm>" + esc(c.country_name) + "</span><span class=grow></span><span class=fl2>" + esc(c.count) + "</span></div>";
        });
        box.innerHTML = h || "<p class=muted>–</p>";
        return;
      }
      window._MAPITEMS = d.items || [];
      filterList();
    }).catch(function () {});
  }

  function filterList() {
    var box = document.getElementById("maplist");
    var q = (document.getElementById("listsearch") || {}).value || "";
    q = q.toLowerCase();
    var items = window._MAPITEMS || [];
    var h = "", lastC = null;
    items.forEach(function (it) {
      var hay = (it.name + " " + it.country_name + " " + it.region + " " + (it.tags || []).join(" ")).toLowerCase();
      if (q && hay.indexOf(q) < 0) return;
      if (it.country_name !== lastC) { h += "<div class=status-sub style='margin-top:8px'>" + esc(it.country_name) + (it.dach ? "" : " 🌍") + "</div>"; lastC = it.country_name; }
      var tags = (it.tags && it.tags.length) ? "<div class=tg>" + esc(it.tags.join(" · ")) + "</div>" : "";
      var refattr = it.ref ? (" data-ref='" + esc(it.ref) + "'") : "";
      h += "<div class=mrow" + refattr + "><div><div class=nm>" + esc(it.name) + "</div><div class=fl2>" + esc(it.region || it.country_name) + "</div>" + tags + "</div></div>";
    });
    box.innerHTML = h || "<p class=muted>–</p>";
    // Klick auf eine Listenzeile -> zugehörigen Pin öffnen/zentrieren (falls DACH-Pin vorhanden)
    box.querySelectorAll(".mrow[data-ref]").forEach(function (row) {
      row.addEventListener("click", function () { focusPin(row.getAttribute("data-ref")); });
    });
  }

  function highlightRow(ref) {
    var box = document.getElementById("maplist");
    if (!box) return;
    box.querySelectorAll(".mrow.hl").forEach(function (r) { r.classList.remove("hl"); });
    var row = box.querySelector(".mrow[data-ref='" + ref + "']");
    if (row) {
      row.classList.add("hl");
      row.scrollIntoView({ block: "nearest" });
      setTimeout(function () { row.classList.remove("hl"); }, 2500);
    }
  }

  function focusPin(ref) {
    var m = pinByRef[ref];
    if (!m) return;                       // Nicht-DACH-Eintrag ohne Pin
    map.setView(m.getLatLng(), Math.min(CFG.maxZoom, 9));
    m.openPopup();
  }

  var eventRange = "all";   // "30" | "90" | "all"

  function buildLegend() {
    var el = document.getElementById("evlegend");
    if (!el) return;
    var labels = CFG.evlabels || {};
    var order = ["fair", "meetup", "shop", "talk", "field", "other"];
    el.innerHTML = order.map(function (t) {
      return "<span style='display:inline-block;width:10px;height:10px;border-radius:50%;background:" +
             EVENT_COLORS[t] + ";margin:0 4px 0 12px;vertical-align:middle;border:1px solid #0d1117'></span>" +
             esc(labels[t] || t);
    }).join("");
  }

  function buildPinLegend() {
    var el = document.getElementById("pinlegend");
    if (!el) return;
    el.innerHTML = [["exact", PL.exact], ["coarse", PL.coarse]].map(function (x) {
      return "<span style='display:inline-block;width:10px;height:10px;border-radius:50%;background:" +
             PIN_STYLE[x[0]].fillColor + ";margin:0 4px 0 12px;vertical-align:middle;border:2px solid " +
             PIN_STYLE[x[0]].color + "'></span>" + esc(x[1]);
    }).join("");
  }

  function renderEvents() {
    getJSON("/map/events.json").then(function (d) {
      window._MAPEVENTS = d.events || [];
      applyEventFilter();
    }).catch(function () {});
  }

  function applyEventFilter() {
    var box = document.getElementById("agenda");
    var events = window._MAPEVENTS || [];
    if (eventLayer) { map.removeLayer(eventLayer); }
    eventLayer = L.layerGroup();
    var now = Date.now();
    var horizon = eventRange === "all" ? Infinity : (parseInt(eventRange, 10) * 86400000);
    var h = "";
    events.forEach(function (e) {
      var dt = new Date(e.next);
      if ((dt.getTime() - now) > horizon) return;        // außerhalb des Zeitfensters
      var ds = dt.toLocaleString(CFG.lang === "en" ? "en-GB" : "de-DE", { dateStyle: "medium", timeStyle: "short" });
      var link = safeUrl(e.url);
      h += "<div class=mrow><div><div class=nm>" + esc(e.title) + (e.recurring ? " 🔁" : "") +
           "</div><div class=fl2>" + esc(ds) + (e.venue ? " · " + esc(e.venue) : "") + "</div>" +
           (link ? "<a class=fl2 href='" + link + "' target=_blank rel='noopener noreferrer'>Link</a>" : "") + "</div></div>";
      if (e.lat && e.lon) {
        var col = EVENT_COLORS[e.type] || EVENT_COLORS.other;
        var m = L.circleMarker([e.lat, e.lon], { radius: 8, color: "#fff", weight: 2, fillColor: col, fillOpacity: 0.95 });
        m.bindPopup("<b>" + esc(e.title) + "</b><br>" + esc(ds) + (e.venue ? "<br>" + esc(e.venue) : ""));
        m.addTo(eventLayer);
      }
    });
    eventLayer.addTo(map);
    if (box) box.innerHTML = h || "<p class=muted>–</p>";
  }

  // ── Layer-/Level-Umschalter ───────────────────────────────────────────────
  function wireSwitch(sel, attr, cb) {
    document.querySelectorAll(sel + " a[" + attr + "]").forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        document.querySelectorAll(sel + " a[" + attr + "]").forEach(function (x) { x.classList.remove("on"); });
        a.classList.add("on");
        cb(a.getAttribute(attr));
      });
    });
  }

  // Drei Ebenen: "map" (Halter), "events" (Termine), "all" (beides).
  function setLayer(layer) {
    var listbox = document.getElementById("listbox");
    var agendabox = document.getElementById("agendabox");
    var rangeswitch = document.getElementById("rangeswitch");
    var evlegend = document.getElementById("evlegend");
    var pinlegend = document.getElementById("pinlegend");
    var showEvents = (layer === "events" || layer === "all");
    var showPins   = (layer === "map" || layer === "all");
    // Seitenpanel: Termine-Ansicht zeigt die Agenda, sonst die Liste.
    if (listbox)   listbox.style.display   = (layer === "events") ? "none" : "";
    if (agendabox) agendabox.style.display = (layer === "events") ? "" : "none";
    if (rangeswitch) rangeswitch.style.display = showEvents ? "" : "none";
    if (evlegend) { if (showEvents) { buildLegend(); evlegend.style.display = ""; } else evlegend.style.display = "none"; }
    if (pinlegend) { if (showPins && CFG.member) { buildPinLegend(); pinlegend.style.display = ""; } else pinlegend.style.display = "none"; }
    // Pins
    if (showPins) drawPins(); else if (pinLayer) { map.removeLayer(pinLayer); pinLayer = null; }
    // Events
    if (showEvents) renderEvents(); else if (eventLayer) { map.removeLayer(eventLayer); eventLayer = null; }
  }
  wireSwitch(".rangesw", "data-layer", setLayer);

  wireSwitch("#choroswitch", "data-level", function (level) {
    curLevel = level; drawChoropleth();
  });

  wireSwitch("#rangeswitch", "data-range", function (range) {
    eventRange = range; applyEventFilter();
  });

  var search = document.getElementById("listsearch");
  if (search) search.addEventListener("input", filterList);

  // ── Initialer Load ────────────────────────────────────────────────────────
  getJSON("/map/regions.json").then(function (d) {
    regionData = d; drawChoropleth();
  }).catch(function () { drawChoropleth(); });
  drawPins();
  if (CFG.member) { var pl0 = document.getElementById("pinlegend"); if (pl0) { buildPinLegend(); pl0.style.display = ""; } }
  renderList();
})();
