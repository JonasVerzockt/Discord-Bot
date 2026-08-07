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
  if (typeof Chart === "undefined" || typeof STATS === "undefined" || !STATS) return;
  var L = (typeof STATS_L !== "undefined" && STATS_L) ? STATS_L : {};

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
})();
