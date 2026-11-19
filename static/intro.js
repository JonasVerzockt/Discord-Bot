/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (C) 2026 Jonas Beier
 *
 * static/intro.js – Anlass-Intros beim Öffnen des Boards.
 * Fragt /intro.json (welcher Anlass, Texte) und spielt eine kurze Szene als Ebene über
 * der Seite ab: eigene SVG-Ameisen, reines CSS, keine Bibliothek, kein Ton.
 * Klick, Taste oder Esc überspringt. Bei „Bewegung reduzieren“ nur ein kleines Banner.
 * Ob ein Anlass schon gezeigt wurde, entscheidet der Server (Cookie board_intro).
 */
(function () {
  "use strict";
  var lang = document.documentElement.lang || "de";
  var forced = (new URLSearchParams(location.search)).get("intro") || "";
  var url = "/intro.json?lang=" + encodeURIComponent(lang) + (forced ? "&intro=" + encodeURIComponent(forced) : "");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var CSS = [
    "#aint{position:fixed;inset:0;z-index:100;display:flex;flex-direction:column;align-items:center;justify-content:center;",
    "background:rgba(13,17,23,.86);backdrop-filter:blur(2px);color:#e6edf3;font:15px/1.5 system-ui,Segoe UI,Arial;",
    "cursor:pointer;opacity:0;transition:opacity .35s}",
    "#aint.on{opacity:1} #aint.off{opacity:0}",
    "#aint svg{width:min(560px,92vw);height:auto;overflow:visible}",
    "#aint .t{font-size:clamp(20px,4.5vw,30px);font-weight:700;margin-top:8px;text-align:center;padding:0 16px;",
    "opacity:0;animation:aiUp .6s ease-out .5s forwards}",
    "#aint .s{color:#8b949e;margin-top:4px;text-align:center;padding:0 16px;opacity:0;animation:aiUp .6s ease-out .8s forwards}",
    "#aint .l{margin-top:12px;opacity:0;animation:aiUp .6s ease-out 1.1s forwards}",
    "#aint .l a{background:#238636;color:#fff;border-radius:6px;padding:7px 14px;text-decoration:none;display:inline-block}",
    "#aint .k{position:absolute;bottom:14px;color:#6e7681;font-size:12px}",
    "@keyframes aiUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}",
    /* Ameise: Beine abwechselnd */
    ".ai-l1,.ai-l2{transform-box:fill-box;transform-origin:50% 0}",
    ".ai-walk .ai-l1{animation:aiStep .32s ease-in-out infinite}",
    ".ai-walk .ai-l2{animation:aiStep .32s ease-in-out infinite reverse}",
    "@keyframes aiStep{0%,100%{transform:skewX(18deg)}50%{transform:skewX(-18deg)}}",
    ".ai-ant{fill:#d29922;stroke:#d29922} .ai-ant .b{stroke:none} .ai-ant .eye{fill:#0d1117}",
    ".ai-ant line,.ai-ant path{fill:none;stroke-width:1.6;stroke-linecap:round}",
    /* Bewegungen */
    ".ai-in{animation:aiIn 1.6s cubic-bezier(.3,.7,.4,1) both}",
    "@keyframes aiIn{from{transform:translateX(-420px)}to{transform:none}}",
    ".ai-cross{animation:aiCross 2.8s linear both}",
    "@keyframes aiCross{from{transform:translateX(-160px)}to{transform:translateX(560px)}}",
    ".ai-fade{opacity:0;animation:aiFade .6s ease-out both}",
    "@keyframes aiFade{to{opacity:1}}",
    ".ai-pop{transform-box:fill-box;transform-origin:50% 50%;animation:aiPop .5s cubic-bezier(.2,1.6,.4,1) both}",
    "@keyframes aiPop{from{transform:scale(0)}to{transform:scale(1)}}",
    ".ai-snow{animation:aiSnow linear infinite}",
    "@keyframes aiSnow{from{transform:translateY(-30px)}to{transform:translateY(240px)}}",
    ".ai-burst{transform-box:fill-box;transform-origin:50% 50%;animation:aiBurst 1.1s ease-out both}",
    "@keyframes aiBurst{0%{transform:scale(.1);opacity:1}70%{opacity:1}100%{transform:scale(1);opacity:0}}",
    ".ai-roll{transform-box:fill-box;transform-origin:50% 50%;animation:aiRoll 1.6s linear both}",
    "@keyframes aiRoll{from{transform:rotate(-540deg)}to{transform:none}}",
    ".ai-wing{transform-box:fill-box;transform-origin:20% 100%;animation:aiWing .12s ease-in-out infinite alternate}",
    "@keyframes aiWing{from{transform:rotate(-25deg)}to{transform:rotate(15deg)}}",
    ".ai-fly{animation:aiFly 2.6s ease-in-out both}",
    "@keyframes aiFly{0%{transform:translate(-120px,180px)}50%{transform:translate(220px,60px)}100%{transform:translate(620px,-60px)}}",
    ".ai-sun{animation:aiSun 1.8s ease-out both}",
    "@keyframes aiSun{from{transform:translateY(90px)}to{transform:none}}",
    ".ai-grow{transform-box:fill-box;transform-origin:50% 100%;animation:aiGrow 1.2s ease-out .4s both}",
    "@keyframes aiGrow{from{transform:scaleY(0)}to{transform:none}}",
    ".ai-zz{animation:aiZz 1.2s ease-out both}",
    "@keyframes aiZz{0%{opacity:0;transform:translate(0,0)}30%{opacity:1}100%{opacity:0;transform:translate(14px,-26px)}}",
    ".ai-wake{transform-box:fill-box;transform-origin:50% 100%;animation:aiWake .6s ease-out 1s both}",
    "@keyframes aiWake{from{transform:rotate(-12deg) translateY(4px)}to{transform:none}}",
    ".ai-drop{animation:aiDrop .9s cubic-bezier(.3,1.4,.5,1) both}",
    "@keyframes aiDrop{from{transform:translateY(-200px)}to{transform:none}}",
    ".ai-conf{animation:aiConf 1.8s ease-out both}",
    "@keyframes aiConf{from{transform:translate(0,0) rotate(0);opacity:1}to{opacity:0}}",
    ".ai-flame{transform-box:fill-box;transform-origin:50% 100%;animation:aiFlame .3s ease-in-out infinite alternate}",
    "@keyframes aiFlame{from{transform:scaleY(.8)}to{transform:scaleY(1.15)}}",
    ".ai-wave{transform-box:fill-box;transform-origin:0 100%;animation:aiWave .4s ease-in-out infinite alternate}",
    "@keyframes aiWave{from{transform:rotate(-20deg)}to{transform:rotate(25deg)}}",
    /* Banner bei reduzierter Bewegung */
    "#aintb{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:100;max-width:min(560px,92vw);",
    "background:#161b22;border:1px solid #30363d;border-radius:10px;padding:10px 14px;color:#e6edf3;",
    "font:14px/1.45 system-ui,Segoe UI,Arial;box-shadow:0 4px 14px #0008;cursor:pointer}",
    "#aintb b{display:block} #aintb span{color:#8b949e}"
  ].join("");

  /* ── Bausteine ─────────────────────────────────────────────────────────── */
  // Ameise (Seitenansicht, schaut nach rechts), ca. 44×26 Einheiten, Ursprung links oben.
  function ant(x, y, s, extra, walk) {
    s = s || 1;
    return '<g transform="translate(' + x + ' ' + y + ') scale(' + s + ')"><g class="ai-ant' + (walk === false ? "" : " ai-walk") + '">' +
      '<g class="ai-l1"><line x1="16" y1="13" x2="12" y2="25"/><line x1="24" y1="13" x2="26" y2="25"/></g>' +
      '<g class="ai-l2"><line x1="20" y1="13" x2="19" y2="25"/><line x1="22" y1="13" x2="31" y2="24"/></g>' +
      '<ellipse class="b" cx="9" cy="12" rx="9" ry="7"/>' +
      '<ellipse class="b" cx="21" cy="11" rx="6" ry="3.8"/>' +
      '<circle class="b" cx="32" cy="9" r="5"/><circle class="eye" cx="34" cy="7.8" r="1.1"/>' +
      '<path d="M34 5 Q37 -1 43 -2"/><path d="M33 4.5 Q34 -3 39 -5"/>' +
      (extra || "") + '</g></g>';
  }
  function txt(x, y, t, size, fill, cls, delay) {
    return '<text x="' + x + '" y="' + y + '" text-anchor="middle" font-size="' + (size || 20) +
      '" font-weight="700" fill="' + (fill || "#e6edf3") + '" font-family="system-ui,Segoe UI,Arial"' +
      (cls ? ' class="' + cls + '"' : "") + (delay ? ' style="animation-delay:' + delay + 's"' : "") + '>' + esc(t) + '</text>';
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function ground() { return '<line x1="0" y1="186" x2="560" y2="186" stroke="#30363d" stroke-width="2"/>'; }
  function confetti(cx, cy, n, delay) {
    var c = ["#58a6ff", "#3fb950", "#d29922", "#f85149", "#d2a8ff"], o = "";
    for (var i = 0; i < n; i++) {
      var a = (i / n) * Math.PI * 2, r = 70 + (i % 4) * 22;
      o += '<g class="ai-conf" style="animation-delay:' + (delay || 0) + 's;--dx:0"><rect x="' + cx + '" y="' + cy + '" width="6" height="3" fill="' + c[i % 5] +
        '"><animateTransform attributeName="transform" type="translate" from="0 0" to="' + Math.round(Math.cos(a) * r) + ' ' + Math.round(Math.sin(a) * r + 40) +
        '" dur="1.8s" begin="' + (delay || 0) + 's" fill="freeze"/></rect></g>';
    }
    return o;
  }
  function cake(x, y, candles) {
    var o = '<g transform="translate(' + x + ' ' + y + ')">' +
      '<rect x="0" y="14" width="46" height="22" rx="3" fill="#d2a8ff"/>' +
      '<rect x="0" y="14" width="46" height="6" rx="3" fill="#f0f6fc"/>' +
      '<rect x="-4" y="35" width="54" height="3" rx="1.5" fill="#8b949e"/>';
    for (var i = 0; i < candles; i++) {
      var cx = 23 - (candles - 1) * 5 + i * 10;
      o += '<rect x="' + (cx - 1.5) + '" y="2" width="3" height="12" fill="#58a6ff"/>' +
        '<ellipse class="ai-flame" cx="' + cx + '" cy="-1" rx="2.4" ry="4" fill="#f0883e"/>';
    }
    return o + "</g>";
  }

  /* ── Szenen (viewBox 0 0 560 220) ──────────────────────────────────────── */
  var S = {};
  S.yearreview = function (d) {
    var y = String(d.p.y || ""), o = ground();
    for (var i = 0; i < y.length; i++) {
      var x = 160 + i * 64;
      o += '<g class="ai-in" style="animation-delay:' + (i * 0.18) + 's">' +
        ant(x, 160, 1, '<text x="21" y="-6" text-anchor="middle" font-size="30" font-weight="800" fill="#58a6ff" stroke="none">' + y[i] + '</text>') + '</g>';
    }
    return o;
  };
  S.xmas = function () {
    var o = "";
    for (var i = 0; i < 40; i++) {
      o += '<circle class="ai-snow" cx="' + ((i * 53) % 560) + '" cy="0" r="' + (1.2 + (i % 3)) + '" fill="#f0f6fc" opacity=".8" style="animation-duration:' +
        (2.2 + (i % 5) * .5) + 's;animation-delay:-' + ((i * 0.37) % 3).toFixed(2) + 's"/>';
    }
    o += ground() + '<g class="ai-cross">' +
      '<g transform="translate(0 150)"><path d="M0 30 H44 Q52 30 52 22" stroke="#8b949e" stroke-width="3" fill="none"/>' +
      '<rect x="4" y="14" width="40" height="13" rx="2" fill="#8b2b2b"/>' +
      '<rect x="12" y="-4" width="22" height="18" fill="#3fb950"/><rect x="21" y="-4" width="4" height="18" fill="#f85149"/>' +
      '<rect x="12" y="3" width="22" height="4" fill="#f85149"/><path d="M23 -4 q-6 -8 -9 -2 M23 -4 q6 -8 9 -2" stroke="#f85149" stroke-width="2" fill="none"/>' +
      '<line x1="44" y1="20" x2="62" y2="20" stroke="#8b949e" stroke-width="1.5"/></g>' +
      ant(62, 160, 1) + '</g>';
    return o;
  };
  S.newyear = function (d) {
    var o = "", col = ["#58a6ff", "#d29922", "#f85149", "#3fb950", "#d2a8ff"];
    [[120, 70, 0], [300, 50, .35], [440, 80, .7], [210, 95, 1.0]].forEach(function (b, j) {
      var rays = "";
      for (var i = 0; i < 14; i++) {
        var a = i / 14 * Math.PI * 2;
        rays += '<line x1="' + b[0] + '" y1="' + b[1] + '" x2="' + (b[0] + Math.cos(a) * 48).toFixed(1) + '" y2="' + (b[1] + Math.sin(a) * 48).toFixed(1) +
          '" stroke="' + col[(i + j) % 5] + '" stroke-width="2.5" stroke-linecap="round"/>';
      }
      o += '<g class="ai-burst" style="animation-delay:' + b[2] + 's">' + rays + '</g>';
    });
    o += ground() + txt(280, 150, d.p.y, 44, "#d29922", "ai-fade", 1.1);
    for (var k = 0; k < 4; k++) o += '<g class="ai-in" style="animation-delay:' + (0.9 + k * .15) + 's">' + ant(170 + k * 60, 160, .8) + '</g>';
    return o;
  };
  S.spring = function () {
    var o = '<g class="ai-sun"><circle cx="430" cy="70" r="30" fill="#d29922"/>';
    for (var i = 0; i < 10; i++) {
      var a = i / 10 * Math.PI * 2;
      o += '<line x1="' + (430 + Math.cos(a) * 38).toFixed(1) + '" y1="' + (70 + Math.sin(a) * 38).toFixed(1) + '" x2="' + (430 + Math.cos(a) * 50).toFixed(1) +
        '" y2="' + (70 + Math.sin(a) * 50).toFixed(1) + '" stroke="#d29922" stroke-width="3" stroke-linecap="round"/>';
    }
    o += '</g>' + ground();
    [[90, 0], [140, .2], [380, .4]].forEach(function (f) {
      o += '<g class="ai-grow" style="animation-delay:' + (0.4 + f[1]) + 's"><line x1="' + f[0] + '" y1="186" x2="' + f[0] + '" y2="150" stroke="#3fb950" stroke-width="3"/>' +
        '<circle cx="' + f[0] + '" cy="146" r="7" fill="#d2a8ff"/><circle cx="' + f[0] + '" cy="146" r="3" fill="#d29922"/></g>';
    });
    o += '<text class="ai-zz" x="290" y="120" font-size="20" fill="#8b949e" font-weight="700">z</text>' +
      '<text class="ai-zz" x="300" y="105" font-size="16" fill="#8b949e" font-weight="700" style="animation-delay:.3s">z</text>' +
      '<g class="ai-wake">' + ant(240, 145, 1.5, "", false) + '</g>';
    return o;
  };
  S.easter = function () {
    return ground() + '<g class="ai-in"><g class="ai-roll"><g transform="translate(250 136)">' +
      '<ellipse cx="25" cy="25" rx="22" ry="28" fill="#d2a8ff"/>' +
      '<path d="M3 20 l7 -6 7 6 7 -6 7 6 7 -6 7 6" stroke="#3fb950" stroke-width="3" fill="none"/>' +
      '<path d="M4 34 l7 6 7 -6 7 6 7 -6 7 6 7 -6" stroke="#d29922" stroke-width="3" fill="none"/>' +
      '<circle cx="25" cy="27" r="3" fill="#58a6ff"/></g></g>' +
      ant(196, 160, 1) + '</g>';
  };
  S.swarm = function () {
    var o = "", wings = '<ellipse class="ai-wing" cx="24" cy="2" rx="14" ry="5" fill="#c9d1d9" opacity=".55" stroke="none"/>' +
      '<ellipse class="ai-wing" cx="20" cy="3" rx="12" ry="4" fill="#c9d1d9" opacity=".4" stroke="none" style="animation-delay:.05s"/>';
    for (var i = 0; i < 7; i++) {
      o += '<g class="ai-fly" style="animation-delay:' + (i * 0.22) + 's"><g transform="translate(' + (i % 3) * 30 + ' ' + ((i * 37) % 90) + ')">' +
        ant(0, 0, .7 + (i % 3) * .15, wings, false) + '</g></g>';
    }
    return o;
  };
  S.aam = function (d) {
    var pump = '<g transform="translate(330 140)"><ellipse cx="26" cy="24" rx="28" ry="22" fill="#f0883e"/>' +
      '<path d="M12 6 Q8 24 12 44 M40 6 Q44 24 40 44 M26 2 V46" stroke="#bd561d" stroke-width="2" fill="none"/>' +
      '<rect x="23" y="-6" width="6" height="9" rx="2" fill="#3fb950"/>' +
      '<path d="M14 18 l6 -6 6 6 z M32 18 l6 -6 6 6 z" fill="#0d1117"/><path d="M14 30 q12 10 26 0 l-4 4 -4 -3 -5 4 -4 -4 -5 3z" fill="#0d1117"/></g>';
    return ground() + '<g class="ai-pop" style="animation-delay:.2s">' + pump + '</g>' +
      '<g class="ai-in">' + ant(150, 160, 1.1, '<g transform="translate(2 -36)">' + cake(0, 0, 0) + '</g>') + '</g>' +
      txt(280, 70, d.p.n, 58, "#d29922", "ai-fade", 1.2) + confetti(280, 60, 22, 1.3);
  };
  S.botbday = function (d) {
    return ground() + '<g class="ai-in">' + ant(220, 160, 1.2, '<g transform="translate(-2 -38)">' + cake(0, 0, Math.min(5, d.p.n || 1)) + '</g>') + '</g>' +
      confetti(280, 80, 22, 1.4);
  };
  S.join = function (d) {
    var arm = '<path class="ai-wave" d="M34 4 L44 -10" stroke-width="2.2"/>';
    return ground() + '<g class="ai-in">' + ant(240, 160, 1.3, arm) + '</g>' +
      txt(280, 80, d.p.n, 56, "#58a6ff", "ai-fade", 1.1) + confetti(280, 70, 24, 1.3);
  };
  S.ach = function (d) {
    var medal = '<g class="ai-drop"><path d="M268 20 L262 60 M292 20 L298 60" stroke="#58a6ff" stroke-width="6"/>' +
      '<circle cx="280" cy="84" r="26" fill="#d29922"/><circle cx="280" cy="84" r="19" fill="none" stroke="#f0f6fc" stroke-width="2" opacity=".6"/>' +
      txt(280, 93, d.p.e || "🏅", 22) + '</g>';
    return ground() + medal + '<g class="ai-in">' + ant(160, 160, 1) + '</g>' + confetti(280, 84, 22, .9);
  };
  S.inat = function (d) {
    var lens = '<g class="ai-pop" style="animation-delay:.3s"><circle cx="380" cy="120" r="30" fill="#58a6ff22" stroke="#c9d1d9" stroke-width="5"/>' +
      '<line x1="402" y1="142" x2="428" y2="170" stroke="#c9d1d9" stroke-width="8" stroke-linecap="round"/></g>';
    return ground() + lens + '<g class="ai-in">' + ant(160, 160, 1.2) + '</g>' + txt(280, 80, d.p.n, 56, "#3fb950", "ai-fade", 1) + confetti(280, 70, 20, 1.2);
  };
  S.reviews = function (d) {
    var star = '<g class="ai-pop"><path d="M280 20 l14 30 33 4 -24 22 6 33 -29 -16 -29 16 6 -33 -24 -22 33 -4z" fill="#d29922"/></g>';
    return ground() + star + '<g class="ai-in">' + ant(150, 160, 1) + '</g>' + txt(280, 150, d.p.n, 34, "#e6edf3", "ai-fade", 1) + confetti(280, 70, 22, 1.1);
  };

  /* ── Anzeige ───────────────────────────────────────────────────────────── */
  function close(el, ms) {
    if (!el || el._closing) return;
    el._closing = true;
    el.classList.add("off");
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, ms || 380);
    document.removeEventListener("keydown", el._key);
  }
  function show(d) {
    var st = document.createElement("style");
    st.textContent = CSS;
    document.head.appendChild(st);
    if (reduce || !S[d.scene]) {
      var b = document.createElement("div");
      b.id = "aintb";
      b.setAttribute("role", "status");
      b.innerHTML = "<b>" + esc(d.title) + "</b><span>" + esc(d.sub) + "</span>" +
        (d.link ? ' <a href="' + esc(d.link) + '">' + esc(d.link_label) + "</a>" : "");
      b.addEventListener("click", function (e) { if (e.target.tagName !== "A") close(b, 0); });
      document.body.appendChild(b);
      setTimeout(function () { close(b, 0); }, 8000);
      return;
    }
    var el = document.createElement("div");
    el.id = "aint";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", d.title);
    el.innerHTML = '<svg viewBox="70 10 420 195" aria-hidden="true">' + S[d.scene](d) + "</svg>" +
      '<div class="t">' + esc(d.title) + '</div><div class="s">' + esc(d.sub) + "</div>" +
      (d.link ? '<div class="l"><a href="' + esc(d.link) + '">' + esc(d.link_label) + "</a></div>" : "") +
      '<div class="k">' + esc(d.skip) + "</div>";
    el.addEventListener("click", function (e) { if (!(e.target.closest && e.target.closest("a"))) close(el); });
    el._key = function (e) { if (e.key === "Escape" || e.key === "Enter" || e.key === " ") close(el); };
    document.addEventListener("keydown", el._key);
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("on"); });
    setTimeout(function () { close(el); }, d.link ? 8200 : 6400);
  }
  function go() {
    fetch(url, {credentials: "same-origin", headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d && d.show) show(d); })
      .catch(function () {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go); else go();
})();
