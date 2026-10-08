/* plangraph boot — UI-PLAN §3/§9/§13 (T2): wires the shell, sidebar and
 * DetailPanel, persists `settings` in localStorage (try/catch: the page works
 * without storage) and the rest of `ui` in location.hash so a URL reproduces
 * the view (R6), owns the keyboard map `/ e c F N Esc` (plus R6 arrows), and
 * records A12's shell-paint timing.
 *
 * Load order matters: this file runs LAST inside the one inline script block, so
 * everything up to and including the sidebar counts is built BEFORE the deferred
 * CDN libs (cytoscape/elkjs) and before first paint — that moment is
 * `performance.mark("plan:shell-paint")`.
 *
 * Components read only `vm` (PlanUI.buildViewModel) and `ui` (PlanUI.Store);
 * the raw snapshot is only ever handed to the legacy PlanPage hooks, which
 * must keep working untouched (page_ui.js hooks, graph in `#cy`, cy.resize()
 * after dock/undock/collapse).
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  /* ---- pure hash codec (§3 `#v=table&sel=atlas:T-31&q=…`) ---------------- */
  function enc(v) {
    try { return encodeURIComponent(v); } catch (e) { return ""; }
  }
  function dec(v) {
    try { return decodeURIComponent(v); } catch (e) { return v; }
  }
  /* state-like {view, sel, query, sidebar:{open,tab}} → "#…", defaults omitted */
  function hashEncode(st) {
    if (!st) return "";
    var p = [];
    if (st.view === "table") p.push("v=table");
    if (st.sel) p.push("sel=" + enc(st.sel));
    if (st.query) p.push("q=" + enc(st.query));
    var sb = st.sidebar || {};
    if (sb.tab && sb.tab !== "needs") p.push("sb=" + enc(sb.tab));
    if (sb.open === false) p.push("so=0");
    return p.length ? "#" + p.join("&") : "";
  }
  /* "#…" → partial patch object; malformed input never throws (R6) */
  function hashDecode(str) {
    var out = {};
    var s = String(str == null ? "" : str).replace(/^#/, "");
    if (!s) return out;
    s.split("&").forEach(function (pair) {
      var i = pair.indexOf("=");
      if (i < 1) return;
      var k = pair.slice(0, i), v = dec(pair.slice(i + 1));
      if (k === "v") out.view = v === "table" ? "table" : "graph";
      else if (k === "sel") out.sel = v;
      else if (k === "q") out.query = v;
      else if (k === "sb") out.tab = v;
      else if (k === "so") out.open = v !== "0";
    });
    return out;
  }
  PlanUI.hashEncode = hashEncode;
  PlanUI.hashDecode = hashDecode;

  if (typeof document === "undefined") return;
  var PP = global.PlanPage;
  if (!PP || !PlanUI.Store || !PlanUI.buildViewModel) return;

  var LS_KEY = "plangraph.ui.settings.v1";
  var SIZES = ["S", "M", "L"], THEMES = ["auto", "light", "dark"];
  var BOOLS = ["showCritical", "showCross", "autoCollapseDone", "animate", "showAllEdges"];
  var TABS = ["projects", "needs", "qa", "answers", "settings"];
  var body = document.body;

  function $(id) { return document.getElementById(id); }
  function is(str) {
    return body.getAttribute("data-screen") === str;
  }
  function drawer() {                       // off-canvas range 701–1199 px
    return global.matchMedia && global.matchMedia("(min-width: 701px) and (max-width: 1199px)").matches;
  }

  /* ---- settings: localStorage with a full try/catch fallback ------------- */
  function readSettings() {
    var out = {};
    var def = PlanUI.freshDefaults().settings;
    Object.keys(def).forEach(function (k) { out[k] = def[k]; });
    out.showAllEdges = true;                // sidebar key the Store defaults lack
    try {
      var raw = global.localStorage && global.localStorage.getItem(LS_KEY);
      if (raw) {
        var saved = JSON.parse(raw) || {};
        Object.keys(saved).forEach(function (k) { out[k] = saved[k]; });
      }
    } catch (e) { /* private mode / no storage: defaults are fine */ }
    return normalize(out);
  }

  function normalize(s) {
    var out = {};
    Object.keys(s || {}).forEach(function (k) { out[k] = s[k]; });
    out.textSize = SIZES.indexOf(out.textSize) >= 0 ? out.textSize : "M";
    out.theme = THEMES.indexOf(out.theme) >= 0 ? out.theme : "auto";
    BOOLS.forEach(function (k) { out[k] = !!out[k]; });
    return out;
  }

  function writeSettings(s) {
    try { global.localStorage && global.localStorage.setItem(LS_KEY, JSON.stringify(s)); }
    catch (e) { /* storage blocked: still fully functional */ }
  }

  /* Apply S/M/L root size and the forced theme (base CSS keeps `auto` = OS). */
  function applySettings(s) {
    var h = document.documentElement;
    h.setAttribute("data-textsize", s.textSize);
    h.setAttribute("data-theme", s.theme);
  }

  /* ---- shell attributes driven by the store ------------------------------ */
  function syncSB(ui) {
    var sb = ui.get("sidebar");
    body.setAttribute("data-sbopen", sb.open ? "1" : "0");
    var scrim = $("sb-scrim");
    if (scrim) scrim.hidden = !(sb.open && drawer());
  }

  function setScreen(name) {
    body.setAttribute("data-screen", name);
    var want = is("inbox") || is("menu") ? "" : (ui.get("view") === "table" ? "table" : "graph");
    Array.prototype.forEach.call(document.querySelectorAll("#tabbar .tab-btn"), function (b) {
      var on = b.getAttribute("data-screen") === body.getAttribute("data-screen");
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    return want;
  }

  var ui = null;                            // bound in boot()

  function syncView() {
    var view = ui.get("view") === "table" ? "table" : "graph";
    var cur = body.getAttribute("data-screen");
    if (cur !== "inbox" && cur !== "menu") setScreen(view);
    var g = $("tab-graph"), l = $("tab-list");
    if (g && l) {
      g.classList.toggle("active", view !== "table");
      l.classList.toggle("active", view === "table");
    }
  }

  /* cy must be resized whenever a column docks/undocks or the rail collapses. */
  var lastW = -1;
  function resizeGraph() {
    var run = function () {
      var st = PP.state;
      if (!st.cy) return;
      var c = $("cy");
      st.cy.resize();
      var w = c ? c.clientWidth : 0;
      if (w !== lastW) {
        lastW = w;
        if (PlanUI.GraphView && typeof PlanUI.GraphView.refreshInitialViewport === "function") PlanUI.GraphView.refreshInitialViewport();
      }
    };
    if (global.requestAnimationFrame) global.requestAnimationFrame(run);
    else setTimeout(run, 40);
  }

  function writeHash() {
    var h = hashEncode(ui.get());
    var pg = PlanUI.Pages && PlanUI.Pages.current();
    if (pg && pg !== "graph" && pg !== "table") h += (h ? "&" : "#") + "pg=" + pg;
    try {
      if (global.history && global.history.replaceState) {
        global.history.replaceState(null, "", h || (location.pathname + location.search));
      } else if (h && location.hash !== h) location.hash = h;
    } catch (e) { /* file:// quirks: the view still works, links just stay plain */ }
  }

  /* ---- keyboard map: `/ e c F N Esc` (+ R6 ←/→) -------------------------- */
  function typing(e) {
    var t = e.target;
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  function nextReady() {
    var V = PlanUI.app.vm, list = [];
    V.items.forEach(function (it) { if (it.display === "ready") list.push(it); });
    if (!list.length) return;
    var crit = list.filter(function (i) { return i.critical; });
    if (crit.length) list = crit.concat(list.filter(function (i) { return !i.critical; }));
    var idx = 0, cur = PP.state.selected;
    list.some(function (it, i) { if (it.key === cur) { idx = (i + 1) % list.length; return true; } return false; });
    PP.focusNode(list[idx].key);
  }

  function toggleExpand() {
    var k = PP.state.selected;
    if (!k) return;
    var next = new Set(ui.get("expanded"));      // ui.expanded survives in the hash (T3)
    if (next.has(k)) next.delete(k); else next.add(k);
    ui.set("expanded", next);
  }

  function collapsePhase() {
    var V = PlanUI.app.vm, k = PP.state.selected;
    var rec = k && V.items.get(k);
    if (!rec || !rec.phase) return;
    var next = new Set(ui.get("collapsedPhases"));
    next.add(rec.project + "|" + rec.phase);
    ui.set("collapsedPhases", next);
  }

  function moveSelection(dir) {                  // → successor, ← predecessor
    var rec = PP.state.selected && PlanUI.app.vm.items.get(PP.state.selected);
    if (!rec) return;
    var picks = dir > 0 ? rec.unblocks : rec.deps;
    if (picks && picks.length) PP.focusNode(picks[0]);
  }

  function clearAll() {
    if (body.getAttribute("data-search") === "1") body.removeAttribute("data-search");
    if (ui.get("sidebar").open && drawer()) ui.set("sidebar", { open: false, tab: ui.get("sidebar").tab });
    PP.selectNode(null);
    var s = $("search");
    if (s && s.value) { s.value = ""; ui.set("query", ""); }
  }

  function onKey(e) {
    if (e.key === "Escape") {
      if (typing(e)) e.target.blur();
      if (is("inbox") || is("menu")) closeOverlay(); else clearAll();  // Esc = #sb-close
      return;
    }
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); focusSearch(); }
    else if (e.key === "e") toggleExpand();
    else if (e.key === "c") collapsePhase();
    else if (e.key === "F" || e.key === "f") { if (PP.state.selected) PP.focusNode(PP.state.selected); }
    else if (e.key === "N" || e.key === "n") nextReady();
    else if (e.key === "ArrowRight") moveSelection(1);
    else if (e.key === "ArrowLeft") moveSelection(-1);
  }

  function focusSearch() {
    var s = $("search");
    if (!s) return;
    var wrap = s.closest ? s.closest(".searchwrap") : null;
    if (wrap && window.getComputedStyle(wrap).display === "none") body.setAttribute("data-search", "1");
    s.focus();
    s.select();
  }

  /* ---- a11y (T2c): #sb-close, Esc and app.jump all share closeOverlay();
   * focus then moves to the details-sheet heading when an item is selected,
   * else to the visible Graph tab (#tab-graph desktop / #tabbar phone). */
  function focusAfter() {
    var sheet = $("details");
    var head = PP.state.selected && sheet && !sheet.hidden && sheet.querySelector
      ? sheet.querySelector("h2") : null;
    var top = $("tab-graph");
    var bar = document.querySelector('#tabbar .tab-btn[data-screen="graph"]');
    var shown = function (el) { try { return !!(el && el.getClientRects().length); } catch (err) { return false; } };
    var target = head || (shown(top) ? top : (shown(bar) ? bar : (bar || top)));
    try { if (!target) return; if (head) target.setAttribute("tabindex", "-1"); target.focus(); }
    catch (err) { /* focus is best-effort, it never breaks the close */ }
  }

  function closeOverlay() {              // one exit path: #sb-close, Esc, Jump
    ui.set("view", "graph"); setScreen("graph"); focusAfter();
  }

  /* ---- controls ----------------------------------------------------------- */
  function debounced(fn, ms) {
    var t = null;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  function bindControls() {
    var s = $("search");
    if (s) {
      s.value = ui.get("query");
      s.addEventListener("input", debounced(function () {
        if (s.value !== ui.get("query")) ui.set("query", s.value);
      }, 150));                                // §7 SearchBox debounce
    }
    var st = $("search-toggle");
    if (st) st.addEventListener("click", function () {
      var on = body.getAttribute("data-search") === "1";
      if (on) body.removeAttribute("data-search");
      else { body.setAttribute("data-search", "1"); focusSearch(); }
      st.setAttribute("aria-expanded", on ? "false" : "true");
    });
    var scrim = $("sb-scrim");
    if (scrim) scrim.hidden = !(sb.open && drawer());
  }

  function setScreen(name) {
    body.setAttribute("data-screen", name);
    var want = is("inbox") || is("menu") ? "" : (ui.get("view") === "table" ? "table" : "graph");
    Array.prototype.forEach.call(document.querySelectorAll("#tabbar .tab-btn"), function (b) {
      var on = b.getAttribute("data-screen") === body.getAttribute("data-screen");
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    return want;
  }

  var ui = null;                            // bound in boot()

  function syncView() {
    var view = ui.get("view") === "table" ? "table" : "graph";
    var cur = body.getAttribute("data-screen");
    if (cur !== "inbox" && cur !== "menu") setScreen(view);
    var g = $("tab-graph"), l = $("tab-list");
    if (g && l) {
      g.classList.toggle("active", view !== "table");
      l.classList.toggle("active", view === "table");
    }
  }

  /* cy must be resized whenever a column docks/undocks or the rail collapses. */
  var lastW = -1;
  function resizeGraph() {
    var run = function () {
      var st = PP.state;
      if (!st.cy) return;
      var c = $("cy");
      st.cy.resize();
      var w = c ? c.clientWidth : 0;
      if (w !== lastW) {
        lastW = w;
        if (PlanUI.GraphView && typeof PlanUI.GraphView.refreshInitialViewport === "function") PlanUI.GraphView.refreshInitialViewport();
      }
    };
    if (global.requestAnimationFrame) global.requestAnimationFrame(run);
    else setTimeout(run, 40);
  }

  function writeHash() {
    var h = hashEncode(ui.get());
    var pg = PlanUI.Pages && PlanUI.Pages.current();
    if (pg && pg !== "graph" && pg !== "table") h += (h ? "&" : "#") + "pg=" + pg;
    try {
      if (global.history && global.history.replaceState) {
        global.history.replaceState(null, "", h || (location.pathname + location.search));
      } else if (h && location.hash !== h) location.hash = h;
    } catch (e) { /* file:// quirks: the view still works, links just stay plain */ }
  }

  /* ---- keyboard map: `/ e c F N Esc` (+ R6 ←/→) -------------------------- */
  function typing(e) {
    var t = e.target;
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  function nextReady() {
    var V = PlanUI.app.vm, list = [];
    V.items.forEach(function (it) { if (it.display === "ready") list.push(it); });
    if (!list.length) return;
    var crit = list.filter(function (i) { return i.critical; });
    if (crit.length) list = crit.concat(list.filter(function (i) { return !i.critical; }));
    var idx = 0, cur = PP.state.selected;
    list.some(function (it, i) { if (it.key === cur) { idx = (i + 1) % list.length; return true; } return false; });
    PP.focusNode(list[idx].key);
  }

  function toggleExpand() {
    var k = PP.state.selected;
    if (!k) return;
    var next = new Set(ui.get("expanded"));      // ui.expanded survives in the hash (T3)
    if (next.has(k)) next.delete(k); else next.add(k);
    ui.set("expanded", next);
  }

  function collapsePhase() {
    var V = PlanUI.app.vm, k = PP.state.selected;
    var rec = k && V.items.get(k);
    if (!rec || !rec.phase) return;
    var next = new Set(ui.get("collapsedPhases"));
    next.add(rec.project + "|" + rec.phase);
    ui.set("collapsedPhases", next);
  }

  function moveSelection(dir) {                  // → successor, ← predecessor
    var rec = PP.state.selected && PlanUI.app.vm.items.get(PP.state.selected);
    if (!rec) return;
    var picks = dir > 0 ? rec.unblocks : rec.deps;
    if (picks && picks.length) PP.focusNode(picks[0]);
  }

  function clearAll() {
    if (body.getAttribute("data-search") === "1") body.removeAttribute("data-search");
    if (ui.get("sidebar").open && drawer()) ui.set("sidebar", { open: false, tab: ui.get("sidebar").tab });
    PP.selectNode(null);
    var s = $("search");
    if (s && s.value) { s.value = ""; ui.set("query", ""); }
  }

  function onKey(e) {
    if (e.key === "Escape") {
      if (typing(e)) e.target.blur();
      if (is("inbox") || is("menu")) closeOverlay(); else clearAll();  // Esc = #sb-close
      return;
    }
    if (typing(e) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") { e.preventDefault(); focusSearch(); }
    else if (e.key === "e") toggleExpand();
    else if (e.key === "c") collapsePhase();
    else if (e.key === "F" || e.key === "f") { if (PP.state.selected) PP.focusNode(PP.state.selected); }
    else if (e.key === "N" || e.key === "n") nextReady();
    else if (e.key === "ArrowRight") moveSelection(1);
    else if (e.key === "ArrowLeft") moveSelection(-1);
  }

  function focusSearch() {
    var s = $("search");
    if (!s) return;
    var wrap = s.closest ? s.closest(".searchwrap") : null;
    if (wrap && window.getComputedStyle(wrap).display === "none") body.setAttribute("data-search", "1");
    s.focus();
    s.select();
  }

  /* ---- a11y (T2c): #sb-close, Esc and app.jump all share closeOverlay();
   * focus then moves to the details-sheet heading when an item is selected,
   * else to the visible Graph tab (#tab-graph desktop / #tabbar phone). */
  function focusAfter() {
    var sheet = $("details");
    var head = PP.state.selected && sheet && !sheet.hidden && sheet.querySelector
      ? sheet.querySelector("h2") : null;
    var top = $("tab-graph");
    var bar = document.querySelector('#tabbar .tab-btn[data-screen="graph"]');
    var shown = function (el) { try { return !!(el && el.getClientRects().length); } catch (err) { return false; } };
    var target = head || (shown(top) ? top : (shown(bar) ? bar : (bar || top)));
    try { if (!target) return; if (head) target.setAttribute("tabindex", "-1"); target.focus(); }
    catch (err) { /* focus is best-effort, it never breaks the close */ }
  }

  function closeOverlay() {              // one exit path: #sb-close, Esc, Jump
    ui.set("view", "graph"); setScreen("graph"); focusAfter();
  }

  /* ---- controls ----------------------------------------------------------- */
  function debounced(fn, ms) {
    var t = null;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  function bindControls() {
    var s = $("search");
    if (s) {
      s.value = ui.get("query");
      s.addEventListener("input", debounced(function () {
        if (s.value !== ui.get("query")) ui.set("query", s.value);
      }, 150));                                // §7 SearchBox debounce
    }
    var st = $("search-toggle");
    if (st) st.addEventListener("click", function () {
      var on = body.getAttribute("data-search") === "1";
      if (on) body.removeAttribute("data-search");
      else { body.setAttribute("data-search", "1"); focusSearch(); }
      st.setAttribute("aria-expanded", on ? "false" : "true");
    });
    var sbt = $("sb-toggle");
    if (sbt) sbt.addEventListener("click", function () {
      var cur = ui.get("sidebar");
      ui.set("sidebar", { open: !cur.open, tab: cur.tab });
    });
    var scrim = $("sb-scrim");
    if (scrim) scrim.addEventListener("click", function () {
      var cur = ui.get("sidebar");
      ui.set("sidebar", { open: false, tab: cur.tab });
    });
    var sbc = $("sb-close");               // phone overlay → back to Graph (§10)
    if (sbc) sbc.addEventListener("click", closeOverlay);
    Array.prototype.forEach.call(document.querySelectorAll("#tabbar .tab-btn"), function (b) {
      b.addEventListener("click", function () {
        var target = b.getAttribute("data-screen");
        if (target === "menu") { setScreen("menu"); return; }
        if (PlanUI.Pages) PlanUI.Pages.show(target);
        setScreen(target);
      });
    });
    document.addEventListener("keydown", onKey);
  }

  /* ---- boot: runs synchronously, before the deferred libs and first paint - */
  function boot() {
    PP.initData();                     // parse snapshot + header/chips/legend
    PP.state.booted = true;
    var vm = PlanUI.buildViewModel(PP.state.snapshot);
    var initial = { settings: readSettings() };
    ui = new PlanUI.Store(initial);    // defaults + our `settings` object on top

    var h = hashDecode(location.hash); // restore BEFORE subscribing (no echo)
    var patch = {};
    if (h.view) patch.view = h.view;
    if (h.sel) { patch.sel = h.sel; PP.state.selected = h.sel; }
    if (h.query) patch.query = h.query;
    patch.sidebar = {
      open: h.open === false ? false : true,
      tab: TABS.indexOf(h.tab) >= 0 ? h.tab : "needs"
    };
    ui.patch(patch);

    PlanUI.app = { ui: ui, store: ui, vm: vm, jump: function (k) {
      ui.set("view", "graph"); if (PlanUI.Pages) PlanUI.Pages.show("graph"); PP.focusNode(k); setScreen("graph"); focusAfter();
    } };
    applySettings(ui.get("settings"));
    syncSB(ui);
    syncView();

    PlanUI.Sidebar.mount(ui, vm);      // counts + panels (reads vm/ui only)
    if (PlanUI.Pages) PlanUI.Pages.mount(ui);
    PlanUI.DetailPanel.mount(ui, vm);  // docked panel, follows ui.sel

    /* A12: shell paint = chrome + sidebar counts built, still pre-lib. */
    try { performance.mark("plan:shell-paint"); } catch (e) { /* no Perf API */ }
    global.__planTimings = global.__planTimings || {};
    var t0 = (global.performance && performance.now) ? Math.round(performance.now()) : 0;
    global.__planTimings.shellPaintMs = t0;
    try { console.log("[plan] shell paint " + t0 + " ms"); } catch (e) { /* noop */ }

    bindControls();

    ui.subscribe(function (keys) {
      if (keys.indexOf("settings") >= 0) {
        var sett = normalize(ui.get("settings"));
        applySettings(sett);               // S/M/L on <html>, forced theme
        writeSettings(sett);
        PP.renderLegend();                 // swatches follow the token change
        PP.renderGraph();                  // no settings change is silent: theme,
                                           // edges, animation and collapse all draw
      }
      if (keys.indexOf("view") >= 0) {
        PP.setTab(ui.get("view") === "table" ? "list" : "graph");
        syncView();
      }
      if (keys.indexOf("sidebar") >= 0) { syncSB(ui); resizeGraph(); }
      if (keys.indexOf("sel") >= 0) resizeGraph();
      if (keys.indexOf("query") >= 0) {
        var s = $("search");
        if (s && s.value !== ui.get("query")) s.value = ui.get("query");
      }
      writeHash();
    });
  }

  function afterInit() {               // runs after legacy init(): cy exists here
    PP.initData();                     // idempotent when boot() already parsed
    if (ui.get("view") === "table") PP.setTab("list");
    else PP.setTab("graph");
    if (PlanUI.Pages) {
      var m = /[#&]pg=(qa|needs|settings)\b/.exec(location.hash);
      PlanUI.Pages.onChange = writeHash;
      var first = m ? m[1] : (ui.get("view") === "table" ? "table" : "graph");
      PlanUI.Pages.show(first);
      if (m) setScreen(first);
    }
    if (PP.state.selected) PP.selectNode(PP.state.selected);
    var c = $("cy");
    lastW = c ? c.clientWidth : 0;
    resizeGraph();
  }

  boot();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", afterInit);
  else afterInit();
})(typeof window !== "undefined" ? window : globalThis);
