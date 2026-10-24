/* PlanUI GraphView — UI-PLAN §4, §5, §10, §11, §12 (T3b-1).
 * Creates and manages Cytoscape graph instance with §4 styles,
 * debounced reactive re-layout via ELK + stackProjects, semantic zoom,
 * anchor-stable panBy, phone focus-chip row, and jump/reveal support.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  var PP = global.PlanPage;
  PlanUI.cardHeights = PlanUI.cardHeights || {};
  PlanUI.freshCanvasSize = PlanUI.freshCanvasSize || function (c) { if (!c) return { w: 0, h: 0 }; if (typeof c.resize === "function") c.resize(); return { w: typeof c.width === "function" ? c.width() : 0, h: typeof c.height === "function" ? c.height() : 0 }; };
  var GraphExt = { call: function (n, a, c) { if (PlanUI.GraphExt && typeof PlanUI.GraphExt.call === "function") return PlanUI.GraphExt.call(n, a, c); return n === "transformElements" ? a : (n === "styles" ? [] : null); } };

  var cy = null, currentUI = null, currentVM = null, lastElements = null, lastStackedLayout = null;
  var projectSignatures = {}, projectLayouts = {}, syncTimer = null, syncPromise = null, syncResolve = null;
  var initialViewportDone = false, positionsApplied = false, userTouched = false, applying = false;
  var lastToggledId = null, lastToggledRenderedPos = null;

  var ctx = {
    get cy() { return cy; }, set cy(v) { cy = v; }, vm: function () { return currentVM; }, ui: function () { return currentUI; },
    sync: function (immediate) { return sync(immediate); }, selectNode: function (key) { return selectNode(key); }, isPhone: function () { return isPhone(); }, get container() { return typeof document !== "undefined" && document.getElementById ? document.getElementById("cy") : null; }, set container(v) {}
  };

  function cssVar(name) {
    if (typeof document === "undefined") return "";
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function isPhone() {
    if (typeof window === "undefined") return false;
    return typeof window.matchMedia === "function" ? window.matchMedia("(max-width: 700px)").matches : (typeof window.innerWidth === "number" ? window.innerWidth <= 700 : false);
  }

  /* §4 Cytoscape Styles ("node.phase-collapsed, node.project-collapsed": { "text-max-width": 10000, label: PlanUI.wrapLabel(t, 272) }) */
  function buildStyles() { return PlanUI.graphStyleRules ? PlanUI.graphStyleRules({ cssVar: cssVar, isPhone: isPhone }) : []; }

  /* Engine Missing Banner */
  function checkEngineBanner(hasError) {
    if (typeof document === "undefined") return;
    var banner = document.getElementById("graph-engine-banner");
    var missing = typeof ELK === "undefined" && (!PlanUI.LayoutService || !PlanUI.LayoutService._engine);
    if (missing || hasError) {
      if (!banner) {
        banner = document.createElement("div");
        banner.id = "graph-engine-banner"; banner.className = "engine-banner";
        var msg = document.createElement("span"); msg.textContent = "Graph engine unavailable — use Table";
        var btn = document.createElement("button"); btn.type = "button"; btn.textContent = "Use Table";
        btn.addEventListener("click", function () { var u = currentUI || (PlanUI.app && PlanUI.app.ui); if (u) u.set("view", "table"); });
        banner.appendChild(msg); banner.appendChild(btn);
        var wrap = document.querySelector(".graph-wrap") || document.getElementById("panel-graph");
        if (wrap) wrap.insertBefore(banner, wrap.firstChild);
      }
      banner.hidden = false;
    } else if (banner) banner.hidden = true;
  }

  /* Phone focus-chip row */
  function renderFocusChips() {
    if (typeof document === "undefined") return;
    var row = document.getElementById("focus-chip-row");
    if (!row) {
      row = document.createElement("div");
      row.id = "focus-chip-row"; row.className = "focus-chip-row phone-only";
      row.setAttribute("role", "toolbar"); row.setAttribute("aria-label", "Project filter");
      var wrap = document.querySelector(".graph-wrap") || document.getElementById("panel-graph");
      if (wrap && wrap.parentNode) wrap.parentNode.insertBefore(row, wrap);
    }
    while (row.firstChild) row.removeChild(row.firstChild);
    var cur = currentUI ? currentUI.get("focusProject") : null;
    var list = [{ name: "all", label: "All" }].concat((currentVM && currentVM.projects) || []);
    list.forEach(function (p) {
      var btn = document.createElement("button"); btn.type = "button";
      var on = (!cur && p.name === "all") || cur === p.name;
      btn.className = "chip-btn" + (on ? " active" : "");
      btn.setAttribute("data-project", p.name); btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = p.label || p.name;
      btn.addEventListener("click", function () { if (currentUI) currentUI.set("focusProject", p.name); });
      row.appendChild(btn);
    });
  }

  function updateFocusChips() {
    if (typeof document === "undefined") return;
    var cur = currentUI ? currentUI.get("focusProject") : null;
    Array.prototype.forEach.call(document.querySelectorAll("#focus-chip-row .chip-btn"), function (b) {
      var p = b.getAttribute("data-project"), on = (!cur && p === "all") || cur === p;
      b.classList.toggle("active", on); b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* Semantic Zoom */
  function updateSemanticZoom() { if (PlanUI.updateSemanticZoom) PlanUI.updateSemanticZoom(cy, isPhone()); }

  /* Initial Viewport */
  function setInitialViewport() {
    if (!positionsApplied || !cy || !currentVM) return;
    cy.resize();
    if (cy.width() <= 0 || cy.height() <= 0) return;
    applying = true;
    try {
      PlanUI.GraphViewport.initial(cy, currentVM, currentUI, isPhone(), function (v) { applying = v; });
    } finally {
      applying = false;
    }
    initialViewportDone = true;
    updateSemanticZoom();
  }

  function refreshInitialViewport() { if (userTouched) return; setInitialViewport(); }

  /* Reactive Re-layout: debounced 60 ms with epoch */
  function sync(immediate) {
    if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
    if (!syncPromise) syncPromise = new Promise(function (resolve) { syncResolve = resolve; });
    if (immediate) runSync(); else syncTimer = setTimeout(runSync, 60);
    return syncPromise;
  }

  function runSync() {
    syncTimer = null;
    if (!cy || !currentVM || !currentUI) {
      if (syncResolve) { syncResolve(); syncResolve = null; syncPromise = null; }
      return;
    }
    var epoch = PlanUI.LayoutService.bumpEpoch(), uiState = currentUI.get();
    var effectiveUI = uiState;
    if (isPhone()) {
      var focus = uiState.focusProject, cProjs = new Set(uiState.collapsedProjects || []);
      if (focus === "all") {
        (currentVM.projects || []).forEach(function (p) { cProjs.add(p.name); });
        effectiveUI = Object.assign({}, uiState, { collapsedProjects: cProjs });
      } else if (focus) {
        (currentVM.projects || []).forEach(function (p) { if (p.name !== focus) cProjs.add(p.name); });
        effectiveUI = Object.assign({}, uiState, { collapsedProjects: cProjs });
      }
    }
    var els = PlanUI.buildElements(currentVM, effectiveUI);
    els = GraphExt.call("transformElements", els, ctx);
    lastElements = els;
    var currentEles = {}, newEles = {};
    cy.elements().forEach(function (e) { currentEles[e.id()] = e; });
    (els.nodes || []).concat(els.edges || []).forEach(function (el) { newEles[el.data.id] = el; });
    cy.batch(function () {
      Object.keys(currentEles).forEach(function (id) { if (!newEles[id]) cy.remove(currentEles[id]); });
      var toAdd = [];
      (els.nodes || []).forEach(function (n) {
        var ex = currentEles[n.data.id];
        if (ex) { ex.data(n.data); ex.classes(n.classes); } else toAdd.push(n);
      });
      (els.edges || []).forEach(function (e) {
        var ex = currentEles[e.data.id];
        if (ex) { ex.data(e.data); ex.classes(e.classes); } else toAdd.push(e);
      });
      if (toAdd.length) {
        toAdd.sort(function (a, b) {
          var ap = (a.classes || "").indexOf("compound") >= 0, bp = (b.classes || "").indexOf("compound") >= 0;
          return (ap && !bp) ? -1 : (!ap && bp) ? 1 : 0;
        });
        cy.add(toAdd);
      }
    });

    var heights = PlanUI.cardHeights || {}, projects = currentVM.projects || [];
    var layoutPromises = projects.map(function (p) {
      var sig = PlanUI.layoutSignature(p.name, els, heights);
      if (projectSignatures[p.name] === sig && projectLayouts[p.name]) {
        return Promise.resolve({ project: p.name, layout: projectLayouts[p.name] });
      }
      return PlanUI.LayoutService.layoutProject(p.name, els, heights).then(function (res) {
        if (res) { projectSignatures[p.name] = sig; projectLayouts[p.name] = res; }
        return { project: p.name, layout: res };
      });
    });

    Promise.all(layoutPromises).then(function (results) {
      if (epoch !== PlanUI.LayoutService.getEpoch()) return;
      checkEngineBanner(false);
      var layoutsToStack = [];
      results.forEach(function (r) { if (r && r.layout) layoutsToStack.push(r.layout); });
      var stacked = PlanUI.stackProjects(layoutsToStack, 80);
      lastStackedLayout = stacked;
      var reducedMotion = typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      var dur = (initialViewportDone && uiState.settings && uiState.settings.animate !== false && !reducedMotion) ? 250 : 0;
      cy.batch(function () {
        cy.nodes().forEach(function (n) {
          if (!n.isParent() && stacked[n.id()]) {
            var l = stacked[n.id()], cx = l.x + (l.width / 2), cyPos = l.y + (l.height / 2);
            if (dur > 0) n.animate({ position: { x: cx, y: cyPos } }, { duration: dur });
            else n.position({ x: cx, y: cyPos });
          }
        });
      });
      positionsApplied = true;
      if (lastToggledId && lastToggledRenderedPos) {
        var node = cy.getElementById("ph:" + lastToggledId);
        if (!node.length) node = cy.getElementById("phc:" + lastToggledId);
        if (!node.length) node = cy.getElementById("p:" + lastToggledId);
        if (!node.length) node = cy.getElementById("pc:" + lastToggledId);
        if (node.length) {
          try {
            var rp = node.renderedPosition();
            if (rp) cy.panBy({ x: lastToggledRenderedPos.x - rp.x, y: lastToggledRenderedPos.y - rp.y });
          } catch (e) {}
        }
        lastToggledId = null; lastToggledRenderedPos = null;
      }
      if (!initialViewportDone) setInitialViewport();
      else updateSemanticZoom();
      if (typeof window !== "undefined") {
        window.__planTimings = window.__planTimings || {};
        if (!window.__planTimings.graphFirstPaintMs) {
          try { performance.mark("plan:graph-first-paint"); } catch (e) {}
          window.__planTimings.graphFirstPaintMs = (window.performance && window.performance.now) ? Math.round(window.performance.now()) : Date.now();
        }
      }
      if (PlanUI.CardLayer && typeof PlanUI.CardLayer.sync === "function") PlanUI.CardLayer.sync(cy, currentUI, currentVM);
      GraphExt.call("afterSync", null, ctx);
      var res = syncResolve; syncResolve = null; syncPromise = null; if (res) res();
    }).catch(function () {
      checkEngineBanner(true);
      var res = syncResolve; syncResolve = null; syncPromise = null; if (res) res();
    });
  }

  function selectNode(key) {
    if (PP && PP.state) PP.state.selected = key;
    if (currentUI && currentUI.get("sel") !== key) currentUI.set("sel", key);
    if (cy) {
      cy.nodes().unselect();
      if (key) { var n = cy.getElementById("t:" + key); if (!n.length) n = cy.getElementById(key); if (n.length) n.select(); }
    }
    if (PP && PP.renderDetails) PP.renderDetails(key);
  }

  function focusNode(key) {
    if (!cy || !currentVM || !key) return;
    var targetProj = PlanUI.focusProjectFor ? PlanUI.focusProjectFor(currentUI, currentVM, key) : null;
    var prep = Promise.resolve();
    if (targetProj && currentUI) {
      if (typeof currentUI.patch === "function") currentUI.patch({ focusProject: targetProj });
      else if (typeof currentUI.set === "function") currentUI.set("focusProject", targetProj);
      prep = sync(true);
    }
    prep.then(function () {
      if (!cy) return;
      selectNode(key);
      var patch = PlanUI.revealPatch(currentVM, currentUI ? currentUI.get() : null, key);
      if (patch && currentUI) currentUI.patch(patch);
      sync(true).then(function () {
        if (!cy) return;
        var z = Math.max(cy.zoom(), isPhone() ? 0.8 : 0.9);
        var b = PlanUI.fitTarget(lastElements, lastStackedLayout, key);
        var inset = PlanUI.sheetInset ? PlanUI.sheetInset() : null;
        if (isPhone() && (!inset || !inset.bottom)) inset = { top: 0, bottom: 120, left: 0, right: 0 };
        if (b) {
          var v = PlanUI.panZoomFor(b, PlanUI.freshCanvasSize(cy), z, inset);
          applying = true;
          cy.animate({ pan: v.pan, zoom: v.zoom }, { duration: 250, complete: function () { applying = false; } });
          setTimeout(function () { applying = false; }, 300);
        } else {
          var node = cy.getElementById("t:" + key);
          if (!node.length) node = cy.getElementById(key);
          if (node && node.length) {
            var nb = node.boundingBox();
            var nv = PlanUI.panZoomFor({ x: nb.x1, y: nb.y1, w: nb.w, h: nb.h }, PlanUI.freshCanvasSize(cy), z, inset);
            cy.animate({ pan: nv.pan, zoom: nv.zoom }, { duration: 250 });
          }
        }
        var n = cy.getElementById("t:" + key);
        if (!n.length) n = cy.getElementById(key);
        if (n && n.length) n.select();
      });
    });
  }

  function bindEvents() {
    var pan = PlanUI.PanDrag && PlanUI.PanDrag.install(cy), rawOn = cy.on.bind(cy);
    function inHeader(e) { return !pan || pan.inHeader(e); }
    function on(ev, sel, fn) {
      var f = typeof sel === "function" ? sel : fn, g = function (e) { if (!(pan && pan.wasDragged())) f(e); };
      return typeof sel === "function" ? rawOn(ev, g) : rawOn(ev, sel, g);
    }
    on("tap", "node.task-node", function (evt) { selectNode(evt.target.data("key")); });
    on("tap", "node.exit-node", function (evt) { selectNode(evt.target.data("key")); });
    on("tap", function (evt) { if (evt.target === cy) selectNode(null); });
    on("tap", "node.ghost-node, node.ghost-stub", function (evt) {
      var k = evt.target.data("dep"); if (!k) return; selectNode(k);
      if (currentVM && currentUI && currentVM.items.get(k)) currentUI.set("focusProject", currentVM.items.get(k).project);
    });
    on("tap", "node.phase-compound, node.phase-collapsed", function (evt) {
      var d = evt.target.data(); if (!d || !d.project || !d.phase || !currentUI || !inHeader(evt)) return;
      var k = d.project + "|" + d.phase, c = new Set(currentUI.get("collapsedPhases") || []);
      lastToggledId = k; try { var rp = evt.target.renderedPosition(); lastToggledRenderedPos = rp ? { x: rp.x, y: rp.y } : null; } catch (e) {}
      if (c.has(k)) c.delete(k); else c.add(k);
      currentUI.set("collapsedPhases", c);
    });
    on("tap", "node.project-compound, node.project-collapsed", function (evt) {
      var d = evt.target.data(); if (!d || !d.project || !currentUI || !inHeader(evt)) return;
      var p = d.project, c = new Set(currentUI.get("collapsedProjects") || []);
      lastToggledId = p; try { var rp = evt.target.renderedPosition(); lastToggledRenderedPos = rp ? { x: rp.x, y: rp.y } : null; } catch (e) {}
      if (c.has(p)) c.delete(p); else c.add(p);
      currentUI.set("collapsedProjects", c);
    });
    on("dbltap", "node.task-node", function (evt) {
      var k = evt.target.data("key"); if (!k || !currentUI) return;
      var exp = new Set(currentUI.get("expanded") || []);
      if (exp.has(k)) exp.delete(k); else exp.add(k);
      currentUI.set("expanded", exp);
    });
    cy.on("zoom", updateSemanticZoom);
    cy.on("viewport", function () { if (!applying) userTouched = true; });
    GraphExt.call("bind", null, ctx);
  }

  function initGraphView() {
    var container = document.getElementById("cy");
    if (!container) return;
    if (!global.cytoscape) { container.textContent = "Graph library unavailable."; return; }
    if (!currentUI && PlanUI.app) currentUI = PlanUI.app.ui;
    if (!currentVM && PlanUI.app) currentVM = PlanUI.app.vm;
    if (!currentVM && PP && PP.state && PP.state.snapshot) currentVM = PlanUI.buildViewModel(PP.state.snapshot);

    if (currentUI && currentVM) {
      var extPatch = GraphExt.call("state", null, ctx);
      if (extPatch && typeof extPatch === "object") currentUI.patch(extPatch);
      else {
        var cPh = currentUI.get("collapsedPhases"), cPr = currentUI.get("collapsedProjects");
        if ((!cPh || cPh.size === 0) && (!cPr || cPr.size === 0)) {
          var ov = PlanUI.overviewState ? PlanUI.overviewState(currentVM, { level: "project" }) : { collapsedPhases: PlanUI.initialCollapsed(currentVM), collapsedProjects: new Set() };
          currentUI.patch({ collapsedPhases: ov.collapsedPhases, collapsedProjects: ov.collapsedProjects });
        }
      }
    }

    if (!cy) {
      renderFocusChips(); checkEngineBanner(false);
      cy = global.cytoscape({
        container: container, elements: [], style: buildStyles(),
        minZoom: 0.35, maxZoom: 2.5, wheelSensitivity: 2,
        userZoomingEnabled: true, userPanningEnabled: true,
        boxSelectionEnabled: false, autoungrabify: true, selectionType: "single"
      });
      if (PP && PP.state) PP.state.cy = cy;
      bindEvents();
      GraphExt.call("init", null, ctx);
      if (typeof ResizeObserver !== "undefined" && container) new ResizeObserver(function () { refreshInitialViewport(); }).observe(container);
      if (typeof window !== "undefined") window.addEventListener("resize", function () { refreshInitialViewport(); });

      if (currentUI) {
        var KEYS = ["collapsedPhases", "collapsedProjects", "expanded", "query", "filters", "matchMode", "revealMatches", "focusProject", "settings"];
        currentUI.subscribe(function (keys) {
          if (keys.some(function (k) { return KEYS.indexOf(k) >= 0; })) sync();
          if (keys.indexOf("focusProject") >= 0) updateFocusChips();
          if (keys.indexOf("sel") >= 0) {
            var selKey = currentUI.get("sel");
            if (cy) {
              cy.nodes().unselect();
              if (selKey) { var n = cy.getElementById("t:" + selKey); if (!n.length) n = cy.getElementById(selKey); if (n && n.length) n.select(); }
            }
          }
        });
      }
    } else {
      cy.style(buildStyles());
    }
    sync(true);
  }

  if (PP) {
    PP.renderGraph = initGraphView;
    PP.selectNode = selectNode;
    PP.focusNode = focusNode;
  }
  PlanUI.GraphView = {
    mount: function (ui, vm) { currentUI = ui; currentVM = vm; initGraphView(); },
    sync: sync, getCy: function () { return cy; },
    refreshInitialViewport: refreshInitialViewport
  };
  if (typeof window !== "undefined") window.__pgLayoutIdle = function () {
    return new Promise(function (resolve) { var waits = 0; (function pump() {
      if (syncTimer || syncPromise) { if (syncPromise) Promise.resolve(syncPromise).then(pump, pump); else setTimeout(pump, 16); return; }
      if (!cy && waits++ < 250) { setTimeout(pump, 16); return; }
      requestAnimationFrame(function () { requestAnimationFrame(resolve); }); })(); });
  };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
