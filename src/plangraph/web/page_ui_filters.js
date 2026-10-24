/* plangraph page — project/phase filter helpers, split from page_ui.js (W8).
 *
 * Owns the filter off-switches on PlanPage.state (projectsOff/tracksOff),
 * mirrors them into the store's allow-lists for the graph, and re-renders
 * the dependent views. Attaches to the shared `window.PlanPage` namespace
 * (ES5/ES2015, no modules); loaded right after page_ui.js, called only at
 * call time (page_boot.js stays last). */
(function (global) {
  "use strict";

  var PP = global.PlanPage;
  if (!PP || typeof document === "undefined") return;

  function effectivePhase(it) {
    return it.phase || it.track || "";
  }

  /* Tracks of ONE project, in first-appearance (phase progression) order.
   * Uses effective phase (phase || track) so phase-only snapshots work. */
  function tracksOf(p) {
    var seen = {}, out = [];
    PP.state.items.forEach(function (it) {
      if (it.project === p) {
        var ph = effectivePhase(it);
        if (ph && !seen[ph]) {
          seen[ph] = true;
          out.push(ph);
        }
      }
    });
    return out;
  }

  /* Mirror the Projects-tab switches into the store's allow-lists (empty set =
   * no filter) so the graph, which reads ui.filters, hides what is off. */
  function pushFilters() {
    var app = global.PlanUI && global.PlanUI.app, ui = app && app.ui;
    if (!ui || typeof ui.patch !== "function") return;
    var NONE = "__none__";
    function allow(all, off) {
      var on = all.filter(function (x) { return !off[x]; });
      if (on.length === all.length) return new Set();
      return new Set(on.length ? on : [NONE]);
    }
    // Build phase list from effective phases (phase || track) across all items
    var allPhases = PP.uniqueSorted(PP.state.items.map(function (it) { return effectivePhase(it); }));
    var cur = ui.get("filters") || {};
    var f = Object.assign({}, cur, { projects: allow(PP.state.projects, PP.state.projectsOff), phases: allow(allPhases, PP.state.tracksOff) });
    var active = f.projects.size > 0 || f.phases.size > 0;
    ui.patch({ filters: f, matchMode: active ? "hide" : "dim" });
  }

  /* kind: "project" | "phase"; returns the new on/off. Phases are global by name. */
  function toggleFilter(kind, name) {
    var off = kind === "project" ? PP.state.projectsOff : PP.state.tracksOff;
    off[name] = !off[name];
    pushFilters();
    if (global.PlanUI.Sidebar && global.PlanUI.Sidebar.renderProjects) global.PlanUI.Sidebar.renderProjects();
    if (global.PlanUI.QAPage) global.PlanUI.QAPage.render();
    if (global.PlanPage.renderTable) global.PlanPage.renderTable();
    return !off[name];
  }

  function isOn(kind, name) {
    return !(kind === "project" ? PP.state.projectsOff : PP.state.tracksOff)[name];
  }

  /* The old chip/matrix filter UI lives in the sidebar Projects tab now. */
  function renderChips() {
    if (global.PlanUI.Sidebar && global.PlanUI.Sidebar.renderProjects) global.PlanUI.Sidebar.renderProjects();
  }

  PP.toggleFilter = toggleFilter;
  PP.isFilterOn = isOn;
  PP.renderChips = renderChips;
})(typeof window !== "undefined" ? window : globalThis);
