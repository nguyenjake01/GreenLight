/* Live processing status for the Mock ATS (polls /_mock/status). */
(function () {
  "use strict";
  var POLL_MS = 2500;
  var chips = {};   // id -> element
  var panel = document.querySelector("[data-rv-panel]");
  var bar = document.querySelector("[data-rv-indet]");

  function el(name, cls, text) {
    var n = document.createElement(name);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function chipText(st) {
    var t = st.step;
    if (st.state === "working" && st.detail) t += " · " + st.detail.replace(" projects", "");
    return t;
  }
  function paintChip(node, st) {
    var cls = "rv-chip rv-chip-info rv-chip-spin", text = chipText(st), spin = true;
    if (st.state === "done") { cls = "rv-chip rv-chip-ok"; text = "report ready"; spin = false; }
    else if (st.state === "failed") { cls = "rv-chip rv-chip-bad"; text = "couldn't read resume"; spin = false; }
    else if (st.state === "queued") { cls = "rv-chip"; text = "queued"; spin = false; }
    node.className = cls;
    node.dataset.state = st.state;
    node.textContent = "";
    if (spin) { var s = el("span", "rv-spin"); s.setAttribute("aria-hidden", "true"); node.appendChild(s); }
    node.appendChild(document.createTextNode(text));
  }

  function paintSteps(list, steps) {
    list.textContent = "";
    steps.forEach(function (s) {
      var li = el("li"); li.dataset.s = s.status;
      var dot = el("span", "rv-step-dot");
      if (s.status === "done") dot.textContent = "✓";
      else if (s.status === "current") dot.appendChild(el("span", "rv-spin"));
      li.appendChild(dot); li.appendChild(document.createTextNode(s.label));
      list.appendChild(li);
    });
  }
  function paintPanel(st) {
    var card = panel;
    card.dataset.state = st.state;
    var head = card.querySelector("[data-rv-title]");
    var sub = card.querySelector("[data-rv-sub]");
    var list = card.querySelector("[data-rv-steps]");
    var fill = card.querySelector("[data-rv-fill]");
    var act = card.querySelector("[data-rv-actions]");
    var spin = card.querySelector("[data-rv-spin]");
    if (st.state === "done") {
      head.textContent = "Report ready";
      sub.textContent = "The annotated resume and note are in.";
      list.hidden = true; card.querySelector("[data-rv-bar]").hidden = true;
      if (spin && !card.querySelector(".rv-done-ico")) {
        var ok = el("span", "rv-done-ico", "\u2713"); ok.setAttribute("aria-hidden", "true");
        spin.parentNode.insertBefore(ok, spin);
      }
      if (spin) spin.hidden = true;
      if (!act.firstChild) {
        var b = el("button", "rv-btn rv-btn-primary", "Show annotated resume ");
        b.type = "button"; b.appendChild(el("span", "rv-arrow", "→"));
        b.addEventListener("click", function () { window.location.reload(); });
        act.appendChild(b);
      }
      return;
    }
    if (st.state === "failed") {
      head.textContent = "Couldn't read the resume";
      sub.textContent = "The verifier found no claims to check.";
      list.hidden = true; card.querySelector("[data-rv-bar]").hidden = true;
      if (spin) spin.hidden = true;
      return;
    }
    head.textContent = "Resume Verifier is checking this resume";
    sub.textContent = chipText(st);
    paintSteps(list, st.steps);
    fill.style.width = Math.round(st.progress * 100) + "%";
  }

  var ids = [];
  document.querySelectorAll("[data-rv-chip]").forEach(function (n) {
    chips[n.dataset.rvChip] = n;
    if (n.dataset.state !== "done" && n.dataset.state !== "failed") ids.push(n.dataset.rvChip);
  });
  if (panel && panel.dataset.rvPanel && panel.dataset.state !== "done" && panel.dataset.state !== "failed") {
    ids.push(panel.dataset.rvPanel);
  }
  var pending = ids.slice();
  if (!pending.length) return;

  function tick() {
    if (!pending.length) { if (bar) bar.hidden = true; return; }
    fetch("/_mock/status?ids=" + pending.join(","), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        pending = pending.filter(function (id) {
          var st = data[id];
          if (!st) return true;
          if (chips[id]) paintChip(chips[id], st);
          if (panel && panel.dataset.rvPanel === id) paintPanel(st);
          return st.state !== "done" && st.state !== "failed";
        });
      })
      .catch(function () { /* keep trying */ })
      .then(function () {
        if (pending.length) setTimeout(tick, POLL_MS);
        else if (bar) bar.hidden = true;
      });
  }
  setTimeout(tick, POLL_MS);
})();
