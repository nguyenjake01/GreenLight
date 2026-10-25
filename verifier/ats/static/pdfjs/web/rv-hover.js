/* Resume Verifier hover cards for pdf.js annotations. DOM built with textContent only. */
(function () {
  "use strict";
  var AUTHOR = "Resume Verifier";
  var VERDICTS = [
    ["Partially supported", "warn"], ["No public evidence", "neutral"],
    ["Contradicted", "bad"], ["Supported", "ok"]
  ];
  var GLYPH = { pass: "✓", soft: "~", hard: "✕", skip: "–" };
  var cards = new Map(); // annotation element -> parsed data
  var card = null, hideTimer = null, current = null;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function parse(text) {
    var lines = String(text || "").split(/\r?\n/).filter(function (l) { return l.trim(); });
    var out = { warnings: [], verdict: null, tone: "neutral", reason: "", checks: [], notes: [], links: [] };
    if (!lines.length) return out;
    var head = lines[0];
    var m = head.match(/^WARNING - (.*?)\.\s+(.*)$/);
    if (m) { out.warnings = m[1].split(/,\s*/); head = m[2]; }
    var matched = false;
    for (var i = 0; i < VERDICTS.length; i++) {
      var v = VERDICTS[i];
      if (head.indexOf(v[0] + ":") === 0) {
        out.verdict = v[0]; out.tone = v[1];
        out.reason = head.slice(v[0].length + 1).trim(); matched = true; break;
      }
    }
    if (!matched) out.reason = head;
    for (var j = 1; j < lines.length; j++) {
      var l = lines[j].trim(), c;
      if ((c = l.match(/^\[(pass|soft|hard|skip)\]\s+([^:]*):\s*(.*)$/))) {
        out.checks.push({ result: c[1], title: c[2], reason: c[3] });
      } else if ((c = l.match(/^\[note\]\s*(.*)$/))) {
        out.notes.push(c[1]);
      } else if (/^https?:\/\//i.test(l)) {
        out.links.push(l);
      }
    }
    return out;
  }

  function build(d) {
    card.textContent = "";
    var chips = el("div", "rv-chips");
    if (d.verdict) chips.appendChild(el("span", "rv-chip " + d.tone, d.verdict));
    d.warnings.forEach(function (w) { chips.appendChild(el("span", "rv-chip bad", w)); });
    if (chips.childNodes.length) card.appendChild(chips);
    if (d.reason) card.appendChild(el("p", "rv-headline", d.reason));
    if (d.checks.length) {
      card.appendChild(el("p", "rv-label", "Why this colour"));
      var ul = el("ul", "rv-checks");
      d.checks.forEach(function (c) {
        var li = el("li");
        li.appendChild(el("span", "rv-glyph " + c.result, GLYPH[c.result]));
        var body = el("div");
        body.appendChild(el("span", "rv-check-title", c.title));
        if (c.reason) {
          body.appendChild(document.createTextNode(" "));
          body.appendChild(el("span", "rv-check-reason", c.reason));
        }
        li.appendChild(body);
        ul.appendChild(li);
      });
      card.appendChild(ul);
    }
    if (d.notes.length) {
      var nl = el("ul", "rv-notes");
      d.notes.forEach(function (n) { nl.appendChild(el("li", null, n)); });
      card.appendChild(nl);
    }
    if (d.links.length) {
      card.appendChild(el("p", "rv-label", "Evidence"));
      var box = el("div", "rv-links");
      d.links.forEach(function (u) {
        var a = el("a", null, u);
        a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer";
        box.appendChild(a);
      });
      card.appendChild(box);
    }
    card.appendChild(el("p", "rv-footer", "Resume Verifier · rubric check"));
  }

  function ensureCard() {
    if (card) return;
    card = el("div", "rv-card");
    card.hidden = true;
    card.setAttribute("role", "tooltip");
    card.addEventListener("pointerenter", cancelHide);
    card.addEventListener("pointerleave", scheduleHide);
    document.body.appendChild(card);
  }

  function cancelHide() { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; } }
  function scheduleHide() { cancelHide(); hideTimer = setTimeout(hide, 150); }
  function hide() { cancelHide(); if (card) card.hidden = true; current = null; }

  function place(x, y) {
    var pad = 12, gap = 14;
    var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    var w = card.offsetWidth, h = card.offsetHeight;
    var left = x + gap, top = y + gap;
    if (left + w > vw - pad) left = x - gap - w;
    if (left < pad) left = Math.max(pad, vw - pad - w);
    if (top + h > vh - pad) top = y - gap - h;
    if (top < pad) top = pad;
    card.style.left = left + "px";
    card.style.top = top + "px";
  }

  function show(elm, x, y) {
    var d = cards.get(elm);
    if (!d) return;
    ensureCard();
    cancelHide();
    if (current !== elm) { build(d); current = elm; }
    card.hidden = false;
    place(x, y);
  }

  function attach(elm, d) {
    if (cards.has(elm)) { cards.set(elm, d); return; }
    cards.set(elm, d);
    elm.setAttribute("data-rv-card", "1");
    elm.addEventListener("pointerenter", function (e) {
      if (e.pointerType === "touch") return;
      show(elm, e.clientX, e.clientY);
    });
    elm.addEventListener("pointermove", function (e) {
      if (e.pointerType !== "touch" && current === elm && !card.hidden) place(e.clientX, e.clientY);
    });
    elm.addEventListener("pointerleave", scheduleHide);
    elm.addEventListener("click", function (e) {
      if (e.pointerType === "touch" || e.detail === 0 || window.matchMedia("(hover: none)").matches) {
        if (current === elm && card && !card.hidden) hide(); else show(elm, e.clientX, e.clientY);
      }
    });
  }

  async function onLayer(evt, app) {
    var n = evt.pageNumber;
    try {
      var page = await app.pdfDocument.getPage(n);
      var annots = await page.getAnnotations();
      var view = app.pdfViewer.getPageView(n - 1);
      var layerDiv = view && view.div && view.div.querySelector(".annotationLayer");
      if (!layerDiv) return;
      annots.forEach(function (a) {
        var title = a.titleObj && a.titleObj.str;
        if (title !== AUTHOR || a.subtype === "Popup") return;
        var text = a.contentsObj && a.contentsObj.str;
        if (!text) return;
        var node = layerDiv.querySelector('[data-annotation-id="' + a.id + '"]');
        if (node) attach(node, parse(text));
      });
    } catch (err) { console.warn("rv-hover:", err); }
  }

  function init(app) {
    app.eventBus.on("annotationlayerrendered", function (e) { onLayer(e, app); });
    app.eventBus.on("pagechanging", hide);
    // Layers rendered before this script was ready, or while a page is re-rendered, are caught here.
    function scanAll() {
      if (!app.pdfDocument || !app.pdfViewer) return;
      for (var n = 1; n <= app.pdfDocument.numPages; n++) {
        var v = app.pdfViewer.getPageView(n - 1);
        if (v && v.div && v.div.querySelector(".annotationLayer")) onLayer({ pageNumber: n }, app);
      }
    }
    app.eventBus.on("pagesloaded", scanAll);
    app.eventBus.on("pagerendered", function () { setTimeout(scanAll, 0); });
    scanAll();
    setTimeout(scanAll, 1000);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
    document.addEventListener("scroll", function (e) { if (!card || !card.contains(e.target)) hide(); }, true);
  }

  var tries = 0;
  (function wait() {
    var app = window.PDFViewerApplication;
    if (app && app.initializedPromise) {
      app.initializedPromise.then(function () { init(app); });
    } else if (tries++ < 200) {
      setTimeout(wait, 50);
    }
  })();
})();
