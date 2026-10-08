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

  PlanUI.PanDrag = { install: install };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
