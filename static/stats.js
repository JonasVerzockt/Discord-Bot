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
})();
