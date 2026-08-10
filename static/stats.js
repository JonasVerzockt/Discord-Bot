/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (C) 2026 Jonas Beier
 *
 * static/stats.js – Diagramme der /stats-Seite des Feedback-Boards.
 * Liest die globalen Objekte STATS (sprachneutrale Rohzahlen) und STATS_L
 * (lokalisierte Beschriftungen), die der Server als JSON-Inseln einbettet, und
 * rendert sie mit dem self-hosted Chart.js. Dark-Theme. Fehlt ein Canvas oder
 * fehlen Daten, wird das jeweilige Diagramm einfach übersprungen (kein Fehler).
 */
(function () {
  "use strict";
  var L = (typeof STATS_L !== "undefined" && STATS_L) ? STATS_L : {};

  // Hover-Erklärungen: ⓘ an jede Diagramm-Überschrift hängen (unabhängig von Chart.js).
  if (L.exp) {
    Object.keys(L.exp).forEach(function (id) {
      var c = document.getElementById(id); if (!c) return;
      var box = c.closest ? c.closest(".chartbox") : null; if (!box) return;
      var h = box.querySelector("h4"); if (!h || h.querySelector(".info")) return;
      var s = document.createElement("span");
      s.className = "info"; s.title = L.exp[id]; s.textContent = "ⓘ";
      h.appendChild(document.createTextNode(" ")); h.appendChild(s);
    });
  }

  if (typeof Chart === "undefined" || typeof STATS === "undefined" || !STATS) return;

  // Dark-Theme-Defaults
  Chart.defaults.color = "#8b949e";
  Chart.defaults.borderColor = "#21262d";
  Chart.defaults.font.family = "system-ui, Segoe UI, Arial";
  Chart.defaults.font.size = 12;
  Chart.defaults.plugins.legend.labels.color = "#c9d1d9";
  Chart.defaults.maintainAspectRatio = false;

  var ACCENT = "#58a6ff", OK = "#3fb950", OFF = "#6e7681";
  var PALETTE = ["#58a6ff", "#3fb950", "#d29922", "#bc8cff", "#f78166", "#39c5cf",
                 "#db61a2", "#e3b341", "#7ee787", "#ff7b72", "#a5d6ff", "#ffa657"];

  function el(id) { return document.getElementById(id); }

  // Treemap-Plugin registrieren (self-hosted). Falls der UMD-Build sich bereits
  // selbst registriert hat, wird der Fehler ignoriert.
  try {
    var TM = window["chartjs-chart-treemap"];
    if (TM && TM.TreemapController) Chart.register(TM.TreemapController, TM.TreemapElement);
  } catch (e) { /* bereits registriert */ }

  // ── Shops pro Land (horizontaler Balken) ──────────────────────────────────
  (function () {
    var c = el("chCountries"); if (!c || !L.countries) return;
    new Chart(c, {
      type: "bar",
      data: {
        labels: L.countries.labels,
        datasets: [{
          label: L.countries.axis,
          data: L.countries.values,
          backgroundColor: ACCENT,
          borderRadius: 4,
        }],
      },
      options: {
        indexAxis: "y",
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#21262d" } },
          y: { grid: { display: false } },
        },
      },
    });
  })();

  // ── Verfügbarkeit (Donut: lagernd vs. nicht lagernd) ──────────────────────
  (function () {
    var c = el("chStock"); if (!c || !L.stock) return;
    new Chart(c, {
      type: "doughnut",
      data: {
        labels: L.stock.labels,
        datasets: [{
          data: L.stock.values,
          backgroundColor: [OK, OFF],
          borderColor: "#0f141a",
          borderWidth: 2,
        }],
      },
      options: {
        cutout: "62%",
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var d = ctx.dataset.data, tot = d.reduce(function (a, b) { return a + b; }, 0);
                var v = ctx.parsed, pct = tot ? Math.round(v / tot * 1000) / 10 : 0;
                return ctx.label + ": " + v + " (" + pct + "%)";
              },
            },
          },
        },
      },
    });
  })();

  // ── Top-Gattungen (Treemap) ───────────────────────────────────────────────
  (function () {
    var c = el("chGenera"); if (!c || !L.genera) return;
    try {
      new Chart(c, {
        type: "treemap",
        data: {
          datasets: [{
            tree: L.genera.data,
            key: "v",
            spacing: 1,
            borderWidth: 1,
            borderColor: "#0f141a",
            backgroundColor: function (ctx) {
              return ctx.type === "data" ? PALETTE[ctx.dataIndex % PALETTE.length] : "transparent";
            },
            labels: {
              display: true, color: "#fff", font: { size: 11 },
              formatter: function (ctx) { var d = ctx.raw._data || {}; return [d.g, "" + ctx.raw.v]; },
            },
          }],
        },
        options: {
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: function (items) { return items[0].raw._data.g; },
                label: function (ctx) { return "" + ctx.raw.v; },
              },
            },
          },
        },
      });
    } catch (e) { /* Treemap-Plugin nicht verfügbar -> Diagramm überspringen */ }
  })();

  // ── Beliebteste Arten nach Shop-Reichweite (horizontaler Balken) ──────────
  (function () {
    var c = el("chReach"); if (!c || !L.reach) return;
    new Chart(c, {
      type: "bar",
      data: {
        labels: L.reach.labels,
        datasets: [{ label: L.reach.axis, data: L.reach.values, backgroundColor: OK, borderRadius: 4 }],
      },
      options: {
        indexAxis: "y",
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#21262d" } },
          y: { grid: { display: false } },
        },
      },
    });
  })();

  // ── Long-Tail: Arten nach Shop-Reichweite (vertikaler Balken) ─────────────
  (function () {
    var c = el("chLongtail"); if (!c || !L.longtail) return;
    new Chart(c, {
      type: "bar",
      data: {
        labels: L.longtail.labels,
        datasets: [{ label: L.longtail.y, data: L.longtail.values, backgroundColor: ACCENT }],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { title: { display: true, text: L.longtail.x }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: L.longtail.y }, grid: { color: "#21262d" } },
        },
      },
    });
  })();

  // ── Block 3: Shop-Vergleich (horizontale Balken) ──────────────────────────
  function hbar(id, cfg, color, xmax) {
    var c = el(id); if (!c || !cfg) return;
    var x = { beginAtZero: true, grid: { color: "#21262d" } };
    if (xmax) { x.max = xmax; }
    new Chart(c, {
      type: "bar",
      data: { labels: cfg.labels, datasets: [{ label: cfg.axis, data: cfg.values, backgroundColor: color, borderRadius: 4 }] },
      options: {
        indexAxis: "y",
        plugins: { legend: { display: false } },
        scales: { x: x, y: { grid: { display: false } } },
      },
    });
  }
  hbar("chShopOffers", L.shop_offers, ACCENT);
  hbar("chShopBreadth", L.shop_breadth, OK);
  hbar("chShopExclusive", L.shop_exclusive, "#d29922");

  // ── Breite vs. Tiefe (Streudiagramm, alle Shops) ──────────────────────────
  (function () {
    var c = el("chShopScatter"); if (!c || !L.shop_scatter) return;
    new Chart(c, {
      type: "scatter",
      data: { datasets: [{ label: L.shop_scatter.title, data: L.shop_scatter.points,
                           backgroundColor: ACCENT, pointRadius: 4, pointHoverRadius: 6 }] },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function (ctx) {
            var p = ctx.raw; return p.label + ": " + p.x + " " + L.shop_scatter.x + ", " + p.y + " " + L.shop_scatter.y;
          } } },
        },
        scales: {
          x: { title: { display: true, text: L.shop_scatter.x }, beginAtZero: true, grid: { color: "#21262d" } },
          y: { title: { display: true, text: L.shop_scatter.y }, beginAtZero: true, grid: { color: "#21262d" } },
        },
      },
    });
  })();

  // ── Block 4: Preise ───────────────────────────────────────────────────────
  (function () {
    var c = el("chPriceHist"); if (!c || !L.price_hist) return;
    new Chart(c, {
      type: "bar",
      data: { labels: L.price_hist.labels, datasets: [{ label: L.price_hist.y, data: L.price_hist.values, backgroundColor: ACCENT }] },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          x: { title: { display: true, text: L.price_hist.x }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: L.price_hist.y }, grid: { color: "#21262d" } },
        },
      },
    });
  })();

  hbar("chPriceGenus", L.price_genus, "#d29922");

  // ── Preisspanne je Art (Floating-Bar: min–max) – größte und kleinste ──────
  function rangebar(id, cfg, color) {
    var c = el(id); if (!c || !cfg || !cfg.labels || !cfg.labels.length) return;
    new Chart(c, {
      type: "bar",
      data: { labels: cfg.labels, datasets: [{ label: cfg.axis, data: cfg.ranges, backgroundColor: color, borderRadius: 3 }] },
      options: {
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: function (ctx) {
            var r = ctx.raw; return r[0] + " € – " + r[1] + " €";
          } } },
        },
        scales: {
          x: { beginAtZero: true, title: { display: true, text: cfg.axis }, grid: { color: "#21262d" } },
          y: { grid: { display: false } },
        },
      },
    });
  }
  rangebar("chPriceSpread", L.price_spread, OK);
  rangebar("chPriceSpreadSmall", L.price_spread_small, ACCENT);

  // ── Block 5: Verfügbarkeit (Lagerquoten in %, x bis 100) ──────────────────
  hbar("chAvGenus", L.av_genus, OK, 100);
  hbar("chAvCountry", L.av_country, ACCENT, 100);
  hbar("chAvShopBest", L.av_shop_best, OK, 100);
  hbar("chAvShopWorst", L.av_shop_worst, "#f85149", 100);
  hbar("chAvHardest", L.av_hardest, "#d29922");

  // ── Block 6: Datenqualität ────────────────────────────────────────────────
  hbar("chDqShopUncanon", L.dq_shop_uncanon, "#f85149");
  hbar("chDqShopAdjusted", L.dq_shop_adjusted, "#d29922");
  hbar("chDqVariants", L.dq_variants, ACCENT);

  // ── Block 7: Zeitverläufe ─────────────────────────────────────────────────
  function line(id, cfg, color, ymax) {
    var c = el(id); if (!c || !cfg || !cfg.labels || !cfg.labels.length) return;
    var y = { beginAtZero: true, grid: { color: "#21262d" } };
    if (ymax) { y.max = ymax; }
    if (cfg.axis) { y.title = { display: true, text: cfg.axis }; }
    new Chart(c, {
      type: "line",
      data: { labels: cfg.labels, datasets: [{ label: cfg.axis || "", data: cfg.values,
              borderColor: color, backgroundColor: color, tension: 0.25, pointRadius: 2, fill: false }] },
      options: {
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, title: cfg.x ? { display: true, text: cfg.x } : undefined }, y: y },
      },
    });
  }
  line("chTrPrice", L.tr_price, ACCENT);
  line("chTrAvail", L.tr_avail, OK, 100);

  // Preisänderungen je Monat (gruppierte Balken: Senkungen/Erhöhungen)
  (function () {
    var c = el("chTrChanges"); if (!c || !L.tr_changes) return;
    new Chart(c, {
      type: "bar",
      data: { labels: L.tr_changes.labels, datasets: [
        { label: L.tr_changes.down_label, data: L.tr_changes.down, backgroundColor: OK },
        { label: L.tr_changes.up_label, data: L.tr_changes.up, backgroundColor: "#f85149" },
      ] },
      options: {
        plugins: { legend: { position: "bottom" } },
        scales: { x: { grid: { display: false } },
                  y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: L.tr_changes.y }, grid: { color: "#21262d" } } },
      },
    });
  })();

  // Aktuelle größte Preis-Senkungen / -Erhöhungen (horizontale %-Balken, Tooltip alt→neu)
  function drbar(id, cfg, color) {
    var c = el(id); if (!c || !cfg || !cfg.labels || !cfg.labels.length) return;
    new Chart(c, {
      type: "bar",
      data: { labels: cfg.labels, datasets: [{ label: cfg.axis, data: cfg.values, backgroundColor: color, borderRadius: 3 }] },
      options: {
        indexAxis: "y",
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: function (ctx) {
            var info = cfg.info && cfg.info[ctx.dataIndex] ? "  (" + cfg.info[ctx.dataIndex] + ")" : "";
            return ctx.parsed.x + " %" + info;
          } } } },
        scales: { x: { title: { display: true, text: cfg.axis }, grid: { color: "#21262d" } },
                  y: { grid: { display: false } } },
      },
    });
  }
  drbar("chTrDrops", L.tr_drops, OK);
  drbar("chTrIncreases", L.tr_increases, "#f85149");
})();
