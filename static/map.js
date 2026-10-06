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
  // Kontakt-Button (nur bei Opt-in): öffnet das Discord-Profil, von dort kann man eine PN schreiben.
  function contactBtn(u) {
    var s = /^https:\/\/discord\.com\/users\/\d+$/.test(String(u || "")) ? esc(u) : "";
    return s ? "<a class=cbtn href='" + s + "' target=_blank rel='noopener noreferrer'>" +
               esc(PL.contactBtn || PL.contact) + "</a>" : "";
  }

  if (typeof L === "undefined") {
    note(CFG.lang === "en" ? "Map library not loaded yet (the bot downloads it automatically)."
                           : "Kartenbibliothek noch nicht geladen (der Bot lädt sie automatisch herunter).");
    return;
  }

  var map = L.map("map", { worldCopyJump: false, maxZoom: CFG.maxZoom, minZoom: 4 })
    .setView([49.5, 9.5], 5);
  // Feste Stapelreihenfolge: Umrisse (overlayPane 400) < PLZ-Blasen < Halter-Pins < Termine.
  // So liegen Pins immer oben und bleiben anklickbar, egal was zuletzt geladen wurde.
  map.createPane("bubblePane").style.zIndex = 450;
  map.createPane("pinPane").style.zIndex = 620;
  map.createPane("eventPane").style.zIndex = 630;

  var choroLayer = null, pinLayer = null, eventLayer = null;
  var pinByRef = {};        // ref -> Leaflet-Marker (Pin ↔ Listenzeile)
  var allPins = [];         // zuletzt geladene Pins (für Tag-Filter ohne Neuladen)
  var activeTags = [];      // gewählte Tag-Codes (UND-Verknüpfung)
  function tagsMatch(codes) {
    if (!activeTags.length) return true;
    codes = codes || [];
    for (var i = 0; i < activeTags.length; i++) if (codes.indexOf(activeTags[i]) < 0) return false;
    return true;
  }
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

  // Zahl je Bundesland/Kanton: Abgleich über den ISO-Code der Umrisse
  // (shapeISO "DE-BB", "AT-9", "CH-ZH"; Liechtenstein als Ganzes), Name als Rückfallebene.
  function countFor(feat) {
    var p = feat.properties || {};
    var iso = String(p.shapeISO || "");
    var cc = null, code = null;
    var dash = iso.indexOf("-");
    if (dash > 0) { cc = iso.slice(0, dash).toLowerCase(); code = iso.slice(dash + 1).toUpperCase(); }
    else if (iso === "LIE" || String(p.shapeName || "") === "Liechtenstein") { cc = "li"; code = "LI"; }
    if (cc === "li") code = "LI";
    var nm = String(p.shapeName || p.region_name || p.name || "").toLowerCase();
    var arr = regionData.bundesland || [];
    for (var j = 0; j < arr.length; j++) {
      var e = arr[j];
      if (cc && code && e.country === cc && String(e.region_code).toUpperCase() === code) return e.count;
      if (!code && nm && String(e.region_name || "").toLowerCase() === nm) return e.count;
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
    var plzMode = (curLevel === "plz");
    var bubbles = null;
    if (plzMode) {
      bubbles = L.layerGroup();
      var arr = regionData.plz || [];
      if (!arr.length) {
        note(CFG.lang === "en" ? "No postcode data yet (geodata still loading)."
                               : "Noch keine PLZ-Daten (Geodaten werden geladen).");
      }
      arr.forEach(function (e) {
        if (e.lat == null || e.lon == null) return;
        var r = 4 + Math.min(e.count, 24) * 0.5;
        var m = L.circleMarker([e.lat, e.lon], { pane: "bubblePane", radius: r, color: "#0d1117", weight: 0.8,
                                                 fillColor: color(e.count), fillOpacity: 0.75 });
        var xs = (e.country === "de") ? "xxx" : "xx";
        m.bindPopup("<b>PLZ " + esc(e.plz_prefix) + xs + "</b><br>" + e.count + " " +
                    (CFG.lang === "en" ? "keepers" : "Halter"));
        m.addTo(bubbles);
      });
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
          interactive: !plzMode,
          style: function (feat) {
            if (plzMode) return { color: "#6e7681", weight: 1, opacity: 0.7, fillColor: "#30363d", fillOpacity: 0.35 };
            var n = countFor(feat);
            return { color: "#6e7681", weight: 1, opacity: 0.9, fillColor: color(n), fillOpacity: 0.55 };
          },
          onEachFeature: function (feat, layer) {
            if (plzMode) return;               // PLZ-Ebene: Umrisse nur als Hintergrund
            var n = countFor(feat);
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
    if (bubbles) bubbles.addTo(group);     // Blasen liegen über den Umrissen (eigene Ebene)
    group.addTo(map);
    choroLayer = group;
  }

  function drawPins() {
    if (!CFG.member) return;
    getJSON("/map/pins.json").then(function (d) {
      allPins = d.pins || [];
      buildPinLayer();
    }).catch(function () {});
  }

  // Pins (gefiltert nach Tags) in eine Cluster-Ebene legen. Ohne Leaflet.markercluster
  // (Datei fehlt) fällt die Karte auf eine normale Ebene ohne Clustering zurück.
  function buildPinLayer() {
      if (pinLayer) { map.removeLayer(pinLayer); }
      pinLayer = (typeof L.markerClusterGroup === "function")
        ? L.markerClusterGroup({ clusterPane: "pinPane", showCoverageOnHover: false,
                                 maxClusterRadius: 40, spiderfyOnMaxZoom: true })
        : L.layerGroup();
      pinByRef = {};
      allPins.forEach(function (p) {
        if (!tagsMatch(p.tag_codes)) return;
        var st = p.coarse ? PIN_STYLE.coarse : PIN_STYLE.exact;
        var m = L.circleMarker([p.lat, p.lon], { pane: "pinPane", radius: 7, color: st.color, fillColor: st.fillColor, fillOpacity: 0.9, weight: 2 });
        var tags = (p.tags && p.tags.length) ? "<br><span style='color:#8b949e'>" + esc(p.tags.join(", ")) + "</span>" : "";
        var contact = p.contact_url ? ("<br>" + contactBtn(p.contact_url)) : "";
        var area = p.coarse ? ("<br><span style='color:#f778ba'>" + esc(PL.coarseNote) + "</span>") : "";
        m.bindPopup("<b>" + esc(p.name) + "</b><br>" + esc(p.region || p.country) + area + tags + contact);
        if (p.ref) { pinByRef[p.ref] = m; m.on("click", function () { highlightRow(p.ref); }); }
        pinLayer.addLayer(m);
      });
      pinLayer.addTo(map);
  }

  function renderList() {
    var box = document.getElementById("maplist");
    if (!box) return;
    getJSON("/map/list.json").then(function (d) {
      if (!d.member) {
        var h = "<p class=muted>" + (CFG.lang === "en"
          ? "Log in to see individual keepers. Public overview by country:"
          : "Nach Login siehst du einzelne Halter. Öffentliche Übersicht je Land:") + "</p>";
        var counts = d.counts || [];
        counts.forEach(function (c) {
          h += "<div class=mrow><span class=nm>" + esc(c.country_name) + "</span><span class=grow></span><span class=fl2>" + esc(c.count) + "</span></div>";
        });
        if (!counts.length) h += "<p class=muted>" + esc(CFG.emptyText || "–") + "</p>";
        box.innerHTML = h;
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
      if (!tagsMatch(it.tag_codes)) return;
      if (it.country_name !== lastC) { h += "<div class=status-sub style='margin-top:8px'>" + esc(it.country_name) + (it.dach ? "" : " 🌍") + "</div>"; lastC = it.country_name; }
      var tags = (it.tags && it.tags.length) ? "<div class=tg>" + esc(it.tags.join(" · ")) + "</div>" : "";
      var refattr = it.ref ? (" data-ref='" + esc(it.ref) + "'") : "";
      h += "<div class=mrow" + refattr + "><div><div class=nm>" + esc(it.name) + "</div><div class=fl2>" + esc(it.region || it.country_name) + "</div>" + tags + contactBtn(it.contact_url) + "</div></div>";
    });
    box.innerHTML = h || ("<p class=muted>" + esc(activeTags.length ? (CFG.tagNoMatch || "–")
                                                  : (q ? "–" : (CFG.emptyText || "–"))) + "</p>");
    // Klick auf eine Listenzeile -> zugehörigen Pin öffnen/zentrieren (falls DACH-Pin vorhanden)
    box.querySelectorAll(".mrow[data-ref]").forEach(function (row) {
      row.addEventListener("click", function (ev) {
        if (ev.target.closest && ev.target.closest("a")) return;   // Kontakt-Link nicht abfangen
        focusPin(row.getAttribute("data-ref"));
      });
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
    if (pinLayer && pinLayer.zoomToShowLayer) {      // Pin steckt evtl. in einem Cluster
      pinLayer.zoomToShowLayer(m, function () { m.openPopup(); });
      return;
    }
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

  // Aktionen je Termin: Einzeltermin als .ics, Teilnahme (nur eingeloggt), Teilnehmerzahl.
  var EVT = CFG.evtext || {};
  function evActions(e) {
    var h = "<div class=evact>";
    h += "<a class=cbtn href='/map/events/" + encodeURIComponent(e.id) + ".ics' download>" + esc(EVT.ics || "ICS") + "</a>";
    if (CFG.member) {
      h += " <a href='#' class='cbtn rsvp" + (e.going ? " on" : "") + "' data-eid='" + esc(e.id) + "'>" +
           esc(e.going ? (EVT.going || "✓") : (EVT.go || "+")) + "</a>";
    }   // Nicht eingeloggt: Zusagen über den Login-Button oben in der Seitenleiste
    if (e.going_count) {
      h += "<div class=fl2>👥 " + esc(e.going_count) + " " + esc(EVT.count || "") +
           ((e.going_names && e.going_names.length) ? ": " + esc(e.going_names.join(", ")) : "") + "</div>";
    }
    return h + "</div>";
  }

  // Teilnahme an/aus – gilt für Agenda und Karten-Popups (Event-Delegation).
  document.addEventListener("click", function (ev) {
    var a = ev.target.closest ? ev.target.closest("a.rsvp[data-eid]") : null;
    if (!a) return;
    ev.preventDefault();
    if (a.getAttribute("data-busy")) return;
    a.setAttribute("data-busy", "1");
    var eid = a.getAttribute("data-eid");
    fetch("/map/events/" + encodeURIComponent(eid) + "/rsvp", {
      method: "POST", credentials: "same-origin", headers: { "X-Map-CSRF": CFG.csrf || "" }
    }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        (window._MAPEVENTS || []).forEach(function (e) {
          if (String(e.id) === String(eid)) {
            e.going = d.going; e.going_count = d.going_count; e.going_names = d.going_names;
          }
        });
        applyEventFilter();
      })
      .catch(function () { a.removeAttribute("data-busy"); });
  });

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
      var loc = CFG.lang === "en" ? "en-GB" : "de-DE", tz = "Europe/Berlin";
      var dOpt = { dateStyle: "medium", timeZone: tz }, tOpt = { timeStyle: "short", timeZone: tz };
      var ds = e.all_day ? dt.toLocaleDateString(loc, dOpt)
                         : dt.toLocaleString(loc, { dateStyle: "medium", timeStyle: "short", timeZone: tz });
      if (e.end) {                                         // Ende anzeigen (gleicher Tag: nur Uhrzeit)
        var en = new Date(e.end);
        var sameDay = en.toLocaleDateString(loc, dOpt) === dt.toLocaleDateString(loc, dOpt);
        if (sameDay && e.end_has_time) ds += "–" + en.toLocaleTimeString(loc, tOpt);
        else if (!sameDay) ds += " – " + (e.end_has_time ? en.toLocaleString(loc, { dateStyle: "medium", timeStyle: "short", timeZone: tz })
                                                         : en.toLocaleDateString(loc, dOpt));
      }
      var link = safeUrl(e.url);
      h += "<div class=mrow><div><div class=nm>" + esc(e.title) + (e.recurring ? " 🔁" : "") +
           "</div><div class=fl2>📅 " + esc(ds) + "</div>" +
           (e.venue ? "<div class=fl2>📍 " + esc(e.venue) + "</div>" : "") +
           (link ? "<a class=fl2 href='" + link + "' target=_blank rel='noopener noreferrer'>Link</a>" : "") +
           evActions(e) + "</div></div>";
      if (e.lat && e.lon) {
        var col = EVENT_COLORS[e.type] || EVENT_COLORS.other;
        var m = L.circleMarker([e.lat, e.lon], { pane: "eventPane", radius: 8, color: "#fff", weight: 2, fillColor: col, fillOpacity: 0.95 });
        m.bindPopup("<b>" + esc(e.title) + "</b><br>" + esc(ds) + (e.venue ? "<br>" + esc(e.venue) : "") + evActions(e));
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
    var tagfilter = document.getElementById("tagfilterbox");
    if (tagfilter) tagfilter.style.display = showPins ? "" : "none";
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

  // Tag-Filter: Chips an/aus (UND), wirkt auf Pins und Liste.
  var tagbar = document.getElementById("tagfilter");
  if (tagbar) {
    var reset = document.getElementById("tagreset");
    function applyTags() {
      activeTags = [];
      tagbar.querySelectorAll("a[data-tag].on").forEach(function (a) { activeTags.push(a.getAttribute("data-tag")); });
      if (reset) reset.style.display = activeTags.length ? "" : "none";
      var tc = document.getElementById("tagcount");
      if (tc) tc.textContent = activeTags.length ? "(" + activeTags.length + ")" : "";
      if (allPins.length) buildPinLayer();
      filterList();
    }
    tagbar.querySelectorAll("a[data-tag]").forEach(function (a) {
      a.addEventListener("click", function (ev) { ev.preventDefault(); a.classList.toggle("on"); applyTags(); });
    });
    if (reset) reset.addEventListener("click", function (ev) {
      ev.preventDefault();
      tagbar.querySelectorAll("a[data-tag].on").forEach(function (a) { a.classList.remove("on"); });
      applyTags();
    });
  }

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
