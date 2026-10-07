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
  var cityPane = map.createPane("cityPane");          // Städte: über den Umrissen, unter allem anderen
  cityPane.style.zIndex = 440; cityPane.style.pointerEvents = "none";
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
  // ── Farbschemata ─────────────────────────────────────────────────────────
  // Alle Farben der Karte kommen aus genau einem Schema. Umschalten zeichnet die Ebenen
  // aus den schon geladenen Daten neu (keine neuen Anfragen).
  //  • standard: bisherige Farben
  //  • cvd:      für Farbsehschwächen – Okabe-Ito-Palette (Kategorien) und viridis (Mengen)
  //  • contrast: Okabe-Ito + cividis (auch in Graustufen eindeutig), kräftige weiße Ränder,
  //              volle Deckkraft, größere Marker
  // Zusätzlich unterscheiden sich Pins/Events immer auch über die FORM (WCAG 1.4.1):
  // Halter = gefüllter Kreis, grobes PLZ-Gebiet = Ring, Termin = Quadrat mit Symbol.
  var OKABE = { orange: "#E69F00", sky: "#56B4E9", green: "#009E73", yellow: "#F0E442",
                blue: "#0072B2", vermilion: "#D55E00", purple: "#CC79A7" };
  var CIVIDIS = ["#4f576c", "#777776", "#a19975", "#d0be62", "#fee838"];   // matplotlib cividis 0.3–1.0
  var VIRIDIS = ["#414487", "#2a788e", "#22a884", "#7ad151", "#fde725"];   // matplotlib viridis 0.2–1.0
  var THEMES = {
    standard: {
      choro: ["#7ee787", "#56d364", "#3fb950", "#2ea043", "#238636"], empty: "#3b434d",
      fillOpacity: 0.75, emptyOpacity: 0.9,
      border: { color: "#0b0f14", weight: 1.6, opacity: 1 },
      hover:  { color: "#e6edf3", weight: 2.5, opacity: 1 },
      pinExact:  { fill: "#58a6ff", stroke: "#1f6feb" },
      pinCoarse: { fill: "#f778ba", stroke: "#bf4b8a" },
      events: { fair: "#3fb950", meetup: "#58a6ff", shop: "#e3833b", talk: "#a371f7", field: "#d29922", other: "#8b949e" },
      outline: "#0d1117", outlineW: 1.5, scale: 1, cluster: "#1f6feb", clusterText: "#ffffff"
    },
    cvd: {
      choro: VIRIDIS, empty: "#262c34",
      fillOpacity: 0.85, emptyOpacity: 0.95,
      border: { color: "#0b0f14", weight: 1.6, opacity: 1 },
      hover:  { color: "#ffffff", weight: 2.5, opacity: 1 },
      pinExact:  { fill: OKABE.sky,    stroke: OKABE.blue },
      pinCoarse: { fill: OKABE.orange, stroke: OKABE.vermilion },
      events: { fair: OKABE.green, meetup: OKABE.sky, shop: OKABE.orange, talk: OKABE.purple, field: OKABE.yellow, other: "#bbbbbb" },
      outline: "#0d1117", outlineW: 1.5, scale: 1, cluster: OKABE.blue, clusterText: "#ffffff"
    },
    contrast: {
      choro: CIVIDIS, empty: "#161b22",
      fillOpacity: 1, emptyOpacity: 1,
      border: { color: "#ffffff", weight: 2, opacity: 1 },
      hover:  { color: OKABE.sky, weight: 4, opacity: 1 },
      pinExact:  { fill: OKABE.sky,    stroke: "#ffffff" },
      pinCoarse: { fill: OKABE.orange, stroke: "#ffffff" },
      events: { fair: OKABE.green, meetup: OKABE.sky, shop: OKABE.orange, talk: OKABE.purple, field: OKABE.yellow, other: "#dddddd" },
      outline: "#ffffff", outlineW: 2.5, scale: 1.25, cluster: "#000000", clusterText: "#ffffff"
    }
  };
  // Symbole je Event-Typ (zweites Merkmal neben der Farbe).
  var EVENT_ICONS = { fair: "🏷️", meetup: "👥", shop: "🛍️", talk: "🎤", field: "🔎", other: "📌" };

  var themeName = "standard";
  function T() { return THEMES[themeName] || THEMES.standard; }
  function pickTheme() {
    var q = (location.search.match(/[?&]colors=(standard|cvd|contrast)\b/) || [])[1];
    if (q) return q;
    try { var s = localStorage.getItem("mapColors"); if (THEMES[s]) return s; } catch (e) {}
    if (window.matchMedia && window.matchMedia("(prefers-contrast: more)").matches) return "contrast";
    return "standard";
  }
  themeName = pickTheme();

  // Marker-Grafiken (SVG/HTML) – Form + Farbe aus dem aktiven Schema.
  function pinSvg(coarse, px) {
    var t = T(), s = px || Math.round(16 * t.scale), c = coarse ? t.pinCoarse : t.pinExact, r = s / 2;
    var body = coarse
      // Ring: dicker farbiger Rand, dunkler Kern -> „Gebiet, nicht Punkt“
      ? "<circle cx='" + r + "' cy='" + r + "' r='" + (r - 3) + "' fill='" + t.outline + "' fill-opacity='0.35' stroke='" + c.fill + "' stroke-width='4'/>" +
        "<circle cx='" + r + "' cy='" + r + "' r='" + (r - 0.8) + "' fill='none' stroke='" + c.stroke + "' stroke-width='1.2'/>"
      : "<circle cx='" + r + "' cy='" + r + "' r='" + (r - 1.5) + "' fill='" + c.fill + "' stroke='" + c.stroke + "' stroke-width='" + t.outlineW + "'/>";
    return "<svg xmlns='http://www.w3.org/2000/svg' width='" + s + "' height='" + s + "' viewBox='0 0 " + s + " " + s + "' aria-hidden='true'>" + body + "</svg>";
  }
  function pinIcon(coarse) {
    var s = Math.round(16 * T().scale);
    return L.divIcon({ className: "mpin", html: pinSvg(coarse, s), iconSize: [s, s], iconAnchor: [s / 2, s / 2], popupAnchor: [0, -s / 2] });
  }
  function eventHtml(type, px) {
    var t = T(), s = px || Math.round(22 * t.scale), col = t.events[type] || t.events.other;
    return "<span class=mev style='width:" + s + "px;height:" + s + "px;background:" + col + ";border:" +
           t.outlineW + "px solid " + (themeName === "standard" ? "#ffffff" : t.outline) + ";font-size:" + Math.round(s * 0.55) + "px'>" +
           (EVENT_ICONS[type] || EVENT_ICONS.other) + "</span>";
  }
  function eventIcon(type) {
    var s = Math.round(22 * T().scale);
    return L.divIcon({ className: "mevw", html: eventHtml(type, s), iconSize: [s, s], iconAnchor: [s / 2, s / 2], popupAnchor: [0, -s / 2] });
  }
  function clusterIcon(cluster) {
    var t = T(), n = cluster.getChildCount(), s = n < 10 ? 30 : n < 50 ? 36 : 42;
    s = Math.round(s * t.scale);
    return L.divIcon({ className: "mcl", iconSize: [s, s],
      html: "<div style='width:" + s + "px;height:" + s + "px;line-height:" + s + "px;background:" + t.cluster +
            ";color:" + t.clusterText + ";border:" + Math.max(2, t.outlineW) + "px solid " +
            (themeName === "standard" ? t.cluster + "66" : t.outline) + "'>" + n + "</div>" });
  }
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

  // Stufen 1–2, 3–5, 6–10, 11–20, >20 (Index in T().choro).
  function color(n) {
    var c = T().choro;
    return n > 20 ? c[4] : n > 10 ? c[3] : n > 5 ? c[2] : n > 2 ? c[1] : n > 0 ? c[0] : T().empty;
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
        var m = L.circleMarker([e.lat, e.lon], { pane: "bubblePane", radius: r * T().scale, color: T().outline,
                                                 weight: T().outlineW * 0.6, fillColor: color(e.count),
                                                 fillOpacity: Math.max(0.75, T().fillOpacity) });
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
            if (plzMode) return { color: "#484f58", weight: 1, opacity: 0.8, fillColor: "#30363d", fillOpacity: 0.35 };
            var n = countFor(feat), t = T();
            return { color: t.border.color, weight: t.border.weight, opacity: t.border.opacity,
                     fillColor: color(n), fillOpacity: n > 0 ? t.fillOpacity : t.emptyOpacity };
          },
          onEachFeature: function (feat, layer) {
            if (plzMode) return;               // PLZ-Ebene: Umrisse nur als Hintergrund
            var n = countFor(feat);
            var nm = (feat.properties && (feat.properties.region_name || feat.properties.shapeName ||
                      feat.properties.GEN || feat.properties.name || feat.properties.NAME_1)) || "";
            layer.bindPopup("<b>" + esc(nm) + "</b><br>" + n + " " + (CFG.lang === "en" ? "keepers" : "Halter"));
            // Hover: Region hell umranden (ohne die Pins zu überdecken – Umrisse liegen darunter).
            layer.on("mouseover", function () { layer.setStyle(T().hover); if (layer.bringToFront) layer.bringToFront(); });
            layer.on("mouseout",  function () { layer.setStyle(T().border); });
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

  // ── Städte als Orientierungspunkte (GeoNames, nicht anklickbar) ──────────
  // Je weiter hineingezoomt, desto mehr Orte: t0 = Hauptstädte/ab 500.000 Einw. … t3 = ab 50.000.
  var cityData = null, cityLayer = null, citiesOn = true;
  try { citiesOn = localStorage.getItem("mapCities") !== "0"; } catch (e) {}
  function maxTierForZoom(z) { return z <= 5 ? 0 : z === 6 ? 1 : z === 7 ? 2 : 3; }
  function drawCities() {
    if (cityLayer) { map.removeLayer(cityLayer); cityLayer = null; }
    if (!citiesOn || !cityData) return;
    var maxT = maxTierForZoom(map.getZoom());
    cityLayer = L.layerGroup();
    // Größte Orte zuerst; überlappt ein Name einen schon gesetzten, bleibt nur der Punkt.
    var placed = [];
    cityData.filter(function (c) { return c.t <= maxT; })
      .sort(function (a, b) { return (a.t - b.t) || (b.p - a.p); })
      .forEach(function (c) {
        var big = c.t === 0, pt = map.latLngToContainerPoint([c.lat, c.lon]);
        var box = { x1: pt.x + 4, y1: pt.y - 8, x2: pt.x + 8 + String(c.n).length * (big ? 7 : 6.2), y2: pt.y + 8 };
        var free = placed.every(function (o) { return box.x2 < o.x1 || box.x1 > o.x2 || box.y2 < o.y1 || box.y1 > o.y2; });
        if (free) placed.push(box);
        L.marker([c.lat, c.lon], {
          pane: "cityPane", interactive: false, keyboard: false,
          icon: L.divIcon({ className: "mcity" + (big ? " big" : ""), iconSize: [0, 0], iconAnchor: [0, 0],
                            html: "<span class=cdot></span>" + (free ? "<span class=clbl>" + esc(c.n) + "</span>" : "") })
        }).addTo(cityLayer);
      });
    cityLayer.addTo(map);
  }
  getJSON("/static/map_cities.json").then(function (d) {
    cityData = (d && d.cities) || [];
    drawCities();
  }).catch(function () { cityData = null; });   // Datei fehlt (noch kein /map_refresh) -> ohne Städte
  // Nach jedem Zoomen/Verschieben neu setzen (Zoomstufe + Überlappung der Namen).
  map.on("moveend", function () { if (cityData && citiesOn) drawCities(); });
  var cityToggle = document.getElementById("citytoggle");
  if (cityToggle) {
    cityToggle.classList.toggle("on", citiesOn);
    cityToggle.setAttribute("aria-pressed", citiesOn ? "true" : "false");
    cityToggle.addEventListener("click", function (ev) {
      ev.preventDefault();
      citiesOn = !citiesOn;
      try { localStorage.setItem("mapCities", citiesOn ? "1" : "0"); } catch (e) {}
      cityToggle.classList.toggle("on", citiesOn);
      cityToggle.setAttribute("aria-pressed", citiesOn ? "true" : "false");
      drawCities();
    });
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
                                 maxClusterRadius: 40, spiderfyOnMaxZoom: true, iconCreateFunction: clusterIcon })
        : L.layerGroup();
      pinByRef = {};
      allPins.forEach(function (p) {
        if (!tagsMatch(p.tag_codes)) return;
        var m = L.marker([p.lat, p.lon], { pane: "pinPane", icon: pinIcon(!!p.coarse), keyboard: true,
                                           title: String(p.name || ""), alt: String(p.coarse ? PL.coarse : PL.exact) });
        var tags = (p.tags && p.tags.length) ? "<br><span style='color:#8b949e'>" + esc(p.tags.join(", ")) + "</span>" : "";
        var contact = p.contact_url ? ("<br>" + contactBtn(p.contact_url)) : "";
        var area = p.coarse ? ("<br><span style='color:" + T().pinCoarse.fill + "'>◎ " + esc(PL.coarseNote) + "</span>") : "";
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
      return "<span class=lgi>" + eventHtml(t, 18) + " " + esc(labels[t] || t) + "</span>";
    }).join("");
  }

  function buildPinLegend() {
    var el = document.getElementById("pinlegend");
    if (!el) return;
    el.innerHTML = [[false, PL.exact], [true, PL.coarse]].map(function (x) {
      return "<span class=lgi>" + pinSvg(x[0], 16) + " " + esc(x[1]) + "</span>";
    }).join("");
  }

  // Legende der Regionsfarben (Anzahl Halter je Bundesland/Kanton).
  function buildChoroLegend() {
    var el = document.getElementById("cholegend");
    if (!el) return;
    var t = T(), c = t.choro, lb = ["1–2", "3–5", "6–10", "11–20", ">20"];
    el.innerHTML = "<span class=lgi>" + esc(CFG.choroLabel || (CFG.lang === "en" ? "Keepers per region" : "Halter je Region")) + ":</span>" +
      "<span class=lgi><span class=lgsw style='background:" + t.empty + "'></span>0</span>" +
      lb.map(function (l, i) {
        return "<span class=lgi><span class=lgsw style='background:" + c[i] + "'></span>" + l + "</span>";
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
        var m = L.marker([e.lat, e.lon], { pane: "eventPane", icon: eventIcon(e.type), keyboard: true,
                                           title: String(e.title || ""), alt: String((CFG.evlabels || {})[e.type] || e.type) });
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

  // Farb-Umschalter: Schema wählen, merken (nur im Browser) und alle Ebenen neu zeichnen.
  function markColorChip() {
    document.querySelectorAll("#colorswitch a[data-colors]").forEach(function (a) {
      a.classList.toggle("on", a.getAttribute("data-colors") === themeName);
      a.setAttribute("aria-pressed", a.getAttribute("data-colors") === themeName ? "true" : "false");
    });
    document.documentElement.setAttribute("data-mapcolors", themeName);
  }
  function setTheme(name) {
    if (!THEMES[name]) return;
    themeName = name;
    try { localStorage.setItem("mapColors", name); } catch (e) {}
    markColorChip();
    drawChoropleth();
    if (pinLayer && allPins.length) buildPinLayer();
    if (eventLayer) applyEventFilter();
    var pl = document.getElementById("pinlegend"); if (pl && pl.style.display !== "none") buildPinLegend();
    var el = document.getElementById("evlegend"); if (el && el.style.display !== "none") buildLegend();
    buildChoroLegend();
  }
  wireSwitch("#colorswitch", "data-colors", setTheme);
  markColorChip();
  buildChoroLegend();

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
