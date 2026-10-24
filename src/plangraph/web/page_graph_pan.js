/* PlanUI PanDrag — drag-to-pan that works when the press lands on an element.
 *
 * Cytoscape only starts a pan on empty background, but the project compound
 * covers the whole canvas, so we pan by hand when the press starts on an element
 * and report whether the gesture was a drag so the tap that follows can be
 * ignored. inHeader() limits compound collapse/expand taps to the header strip.
 */
(function (global) {
  "use strict";
  var PlanUI = global.PlanUI = global.PlanUI || {};
  var DRAG_PX = 5, HEADER_UNITS = 44;

  function install(cy) {
    var pd = null;
    /* D-677: a dependent of an operator-waiting item shows its waitTip as the native tooltip. */
    cy.on("mouseover", "node", function (e) { var c = cy.container(); if (c) c.title = e.target.data("waitTip") || ""; });
    cy.on("mouseout", "node", function () { var c = cy.container(); if (c) c.title = ""; });
    var cc = cy.container(); if (cc && cc.addEventListener) cc.addEventListener("mouseleave", function () { cc.title = ""; });
    cy.on("vmousedown", function (e) {
      var p = cy.pan();
      pd = { x: e.renderedPosition.x, y: e.renderedPosition.y, pan: { x: p.x, y: p.y }, moved: false, manual: e.target !== cy };
    });
    cy.on("vmousemove", function (e) {
      if (!pd) return;
      var dx = e.renderedPosition.x - pd.x, dy = e.renderedPosition.y - pd.y;
      if (!pd.moved) { if (Math.sqrt(dx * dx + dy * dy) < DRAG_PX) return; pd.moved = true; }
      if (pd.manual && e.originalEvent && e.originalEvent.buttons) cy.pan({ x: pd.pan.x + dx, y: pd.pan.y + dy });
    });
    return {
      wasDragged: function () { return !!(pd && pd.moved); },
      inHeader: function (evt) {
        var n = evt.target;
        if (n.hasClass("project-collapsed") || n.hasClass("phase-collapsed")) return true;
        var bb = n.boundingBox({ includeLabels: false, includeOverlays: false });
        return evt.position.y - bb.y1 <= HEADER_UNITS;
      }
    };
  }

  /* Fresh-load overview (D-639 browser check): every project collapsed and no
   * focus -> GraphViewport.initial() fits all elements instead of anchoring. */
  function uiVal(ui, key) {
    try {
      if (ui && typeof ui.get === "function") { var v = ui.get(key); if (v !== undefined) return v; }
      if (ui && ui[key] !== undefined) return ui[key];
    } catch (e) {}
    return undefined;
  }

  function isOverviewState(ui, vm) {
    var focus = uiVal(ui, "focusProject");
    if (focus && focus !== "all" && focus !== "All") return false;
    var cPr = uiVal(ui, "collapsedProjects"), projs = (vm && vm.projects) || [];
    if (!cPr || !projs.length) return false;
    return projs.every(function (p) {
      var n = p && p.name;
      if (typeof cPr.has === "function") return cPr.has(n);
      return Array.isArray(cPr) ? cPr.indexOf(n) >= 0 : !!cPr[n];
    });
  }

  function fitOverview(cy, isPh, onApply, bbOpts) {
    var GV = PlanUI.GraphViewport || {};
    var eles = (typeof cy.elements === "function") ? cy.elements() : null;
    if (!eles || !eles.length || typeof cy.fit !== "function") return null;
    var inset = GV.sheetInset ? GV.sheetInset() : null;
    if (typeof onApply === "function") onApply(true);
    try { cy.fit(eles, 24); } catch (e) {}
    if (isPh && inset && inset.bottom) {
      try { var p = cy.pan(); cy.pan({ x: p.x, y: p.y - inset.bottom / 2 }); } catch (e2) {}
    }
    try { if (cy.zoom() > 1) cy.zoom(1); } catch (e3) {}
    if (typeof onApply === "function") onApply(false);
    if (GV.installHook) GV.installHook(cy);
    var bb = null;
    try { bb = eles.boundingBox(bbOpts); } catch (e4) {}
    return { zoom: cy.zoom(), pan: cy.pan(), box: bb };
  }

  PlanUI.PanDrag = { install: install };
  PlanUI.isOverviewState = isOverviewState; PlanUI.fitOverview = fitOverview;
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
