/* Resume Verifier: liquid glass over a live commit graph.
   Commit graph background, theme toggle, cursor rim light. No dependencies. */
(function () {
  "use strict";
  var root = document.documentElement;

  function tok(n) { return getComputedStyle(root).getPropertyValue(n).trim(); }

  // ---------------------------------------------------------------- graph
  var cv = document.getElementById("rv-graph");
  var ctx = null;
  try { ctx = cv && cv.getContext ? cv.getContext("2d") : null; } catch (e) { ctx = null; }
  var refreshGraph = function () {};

  if (cv && ctx) {
    var T = {}, W = 0, H = 0, DPR = 1, L = 0, MAIN = 0, DX = 64, scroll = 0;
    var mode = cv.getAttribute("data-mode") === "drift" ? "drift" : "still";
    var dim = mode === "still" ? 0.55 : 1;

    var hash = function (a, b) {
      var h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul((b | 0) + 0x632be5ab, 0xc2b2ae35);
      h ^= h >>> 13; h = Math.imul(h, 0x27d4eb2f); h ^= h >>> 16;
      return (h >>> 0) / 4294967296;
    };
    var readTokens = function () {
      T = {
        page: tok("--page"), a: tok("--glow-a"), b: tok("--glow-b"),
        lanes: [tok("--lane-1"), tok("--lane-2"), tok("--lane-3"), tok("--lane-4")],
        alpha: (parseFloat(tok("--lane-alpha")) || 0.5) * dim,
        hashc: tok("--faint"), font: tok("--font-mono") || "monospace"
      };
    };
    var size = function () {
      DPR = Math.min(2, window.devicePixelRatio || 1);
      W = window.innerWidth; H = window.innerHeight;
      cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
      L = Math.max(5, Math.round(H / 86)); MAIN = Math.floor(L * 0.42);
    };
    var laneY = function (s) { var m = H * 0.08; return m + s * (H - 2 * m) / (L - 1); };
    var alive = function (s, c) {
      if (s === MAIN) return true;
      var span = 4 + Math.floor(hash(s, 7) * 9), off = Math.floor(hash(s, 13) * span);
      return hash(s * 31 + 5, Math.floor((c + off) / span)) > 0.3 + Math.abs(s - MAIN) * 0.07;
    };
    var parent = function (s, c) {
      var step = s > MAIN ? -1 : 1, p = s + step;
      while (p !== MAIN && !alive(p, c)) p += step;
      return p;
    };
    var color = function (s) { return s === MAIN ? T.lanes[0] : T.lanes[(Math.abs(s - MAIN) + (s > MAIN ? 1 : 2)) % 4]; };
    var curve = function (x1, y1, x2, y2) { var mx = (x1 + x2) / 2; ctx.moveTo(x1, y1); ctx.bezierCurveTo(mx, y1, mx, y2, x2, y2); };
    var glow = function (x, y, r, c) {
      var g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, c); g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    };
    var frame = function () {
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.globalAlpha = 1; ctx.fillStyle = T.page; ctx.fillRect(0, 0, W, H);
      glow(W * 0.18, H * 0.2, Math.max(W, H) * 0.6, T.a);
      glow(W * 0.85, H * 0.85, Math.max(W, H) * 0.5, T.b);
      var c0 = Math.floor(scroll / DX) - 1, cols = Math.ceil(W / DX) + 3, s, c;
      ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.globalAlpha = T.alpha;
      for (s = 0; s < L; s++) {
        ctx.strokeStyle = color(s); ctx.beginPath();
        for (c = c0; c < c0 + cols; c++) {
          var x1 = c * DX - scroll, x2 = x1 + DX, a1 = alive(s, c), a2 = alive(s, c + 1);
          if (a1 && a2) { ctx.moveTo(x1, laneY(s)); ctx.lineTo(x2, laneY(s)); }
          else if (!a1 && a2) curve(x1, laneY(parent(s, c)), x2, laneY(s));
          else if (a1 && !a2) curve(x1, laneY(s), x2, laneY(parent(s, c + 1)));
        }
        ctx.stroke();
      }
      ctx.font = "11px " + T.font;
      for (s = 0; s < L; s++) for (c = c0; c < c0 + cols; c++) {
        if (!alive(s, c)) continue;
        var hv = hash(s * 977 + 3, c);
        if (hv < 0.38) continue;
        var x = c * DX - scroll, y = laneY(s), col = color(s);
        ctx.globalAlpha = Math.min(1, T.alpha + 0.15 * dim); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, hv > 0.9 ? 4.5 : 3.5, 0, 6.2832);
        if (hv > 0.9) { ctx.fillStyle = col; ctx.fill(); }
        else { ctx.fillStyle = T.page; ctx.fill(); ctx.strokeStyle = col; ctx.stroke(); }
        if (hv > 0.975) {
          ctx.globalAlpha = T.alpha * 0.5; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.arc(x, y, 9, 0, 6.2832); ctx.strokeStyle = col; ctx.stroke();
        }
        if (hv > 0.955) {
          ctx.globalAlpha = T.alpha * 0.9; ctx.fillStyle = T.hashc;
          ctx.fillText(("000000" + Math.floor(hash(c, s + 99) * 0xfffffff).toString(16)).slice(-7), x + 10, y - 9);
        }
      }
      ctx.globalAlpha = 1;
    };

    // Drift at ~20fps; frozen under reduced motion or when the tab is hidden.
    var reduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };
    var last = 0, running = false;
    var tick = function (t) {
      if (!running) return;
      if (!last) last = t;
      if (t - last >= 50) { scroll += (t - last) * 0.012; last = t; frame(); }
      requestAnimationFrame(tick);
    };
    var start = function () {
      if (mode !== "drift" || running || reduce.matches || document.hidden) return;
      running = true; last = 0; requestAnimationFrame(tick);
    };
    var stop = function () { running = false; };

    document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); else start(); });
    try {
      reduce.addEventListener("change", function () { if (reduce.matches) { stop(); frame(); } else start(); });
    } catch (e) {}
    window.addEventListener("resize", function () { size(); frame(); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { readTokens(); frame(); });

    refreshGraph = function () { readTokens(); frame(); };
    size(); readTokens(); frame(); start();
  }

  // ---------------------------------------------------------------- theme toggle
  function syncToggle() {
    var night = tok("--is-night") === "1";
    var btns = document.querySelectorAll("[data-set-theme]");
    for (var i = 0; i < btns.length; i++) {
      var isDark = btns[i].getAttribute("data-set-theme") === "dark";
      btns[i].setAttribute("aria-pressed", (isDark === night) ? "true" : "false");
    }
  }
  function onThemeChange() { refreshGraph(); syncToggle(); }

  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target.closest("[data-set-theme]") : null;
    if (!t) return;
    var v = t.getAttribute("data-set-theme") === "light" ? "light" : "dark";
    root.setAttribute("data-theme", v);
    try { localStorage.setItem("rv-theme", v); } catch (err) {}
    onThemeChange(); // observer also fires; this keeps it immediate
  });
  try {
    new MutationObserver(onThemeChange).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  } catch (e) {}
  try {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", onThemeChange);
  } catch (e) {}

  // ---------------------------------------------------------------- cursor rim light
  document.addEventListener("pointermove", function (e) {
    var el = e.target && e.target.closest ? e.target.closest(".rv-glass") : null;
    if (!el) return;
    var b = el.getBoundingClientRect();
    el.style.setProperty("--mx", (e.clientX - b.left) + "px");
    el.style.setProperty("--my", (e.clientY - b.top) + "px");
  }, { passive: true });

  syncToggle();
})();
