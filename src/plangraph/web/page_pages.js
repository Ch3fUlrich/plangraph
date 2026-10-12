/* PlanUI.Pages — the header page router and the sidebar resize handle.
 *
 * Pages: graph | table | qa (Decisions & Q&A) | needs (Needs You) |
 * activity (Activity) | progress (Progress) | settings.
 * graph/table stay in the store (`ui.view`); qa/needs/settings are plain pages
 * remembered in the hash as `pg=`. The sidebar holds only the project filter
 * tree, so nothing here touches it except its width.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var PANELS = { graph: "panel-graph", table: "panel-list", qa: "panel-qa", needs: "panel-needs", activity: "panel-activity", progress: "panel-progress", knowledge: "panel-knowledge", settings: "panel-settings" };
  var TABS = { graph: "tab-graph", table: "tab-list", qa: "tab-qa", needs: "tab-needs", activity: "tab-activity", progress: "tab-progress", knowledge: "tab-knowledge", settings: "tab-settings" };
  var SBW_KEY = "plangraph.ui.sbw.v1", SBW_MIN = 180, SBW_MAX = 560;
  var ui = null, cur = "graph";

  function $(id) { return document.getElementById(id); }

  function show(name) {
    if (!PANELS[name] || (name === "knowledge" && !(PlanUI.Knowledge && PlanUI.Knowledge.enabled()))) name = "graph";
    cur = name;
    var PP = global.PlanPage;
    if (name === "graph" || name === "table") {
      if (ui && ui.get("view") !== (name === "table" ? "table" : "graph")) ui.set("view", name === "table" ? "table" : "graph");
      if (PP && PP.setTab) PP.setTab(name === "table" ? "list" : "graph");
    }
    Object.keys(PANELS).forEach(function (k) {
      var p = $(PANELS[k]);
      if (p) p.hidden = k !== name;
      var t = $(TABS[k]);
      if (t) {
        t.setAttribute("aria-selected", k === name ? "true" : "false");
        t.classList.toggle("active", k === name);
      }
    });
    document.body.setAttribute("data-page", name);
    if (name === "table" && PP && PP.renderTable) PP.renderTable();
    if (name === "graph" && PP && PP.state && PP.state.cy) { try { PP.state.cy.resize(); } catch (e) { /* not ready */ } }
    if (name === "activity" && PlanUI.ActivityPage && PlanUI.ActivityPage.render) PlanUI.ActivityPage.render();
    if (name === "progress" && PlanUI.ProgressPage && PlanUI.ProgressPage.render) PlanUI.ProgressPage.render();
    if (name === "knowledge" && PlanUI.KnowledgePage) PlanUI.KnowledgePage.render();
    if (PlanUI.Pages.onChange) PlanUI.Pages.onChange(name);
  }

  function storedWidth() {
    try { var v = parseInt(global.localStorage.getItem(SBW_KEY), 10); return isFinite(v) ? v : null; }
    catch (e) { return null; }
  }
  function setWidth(px, persist) {
    px = Math.max(SBW_MIN, Math.min(SBW_MAX, Math.round(px)));
    document.documentElement.style.setProperty("--sb-w", px + "px");
    var h = $("sb-resize");
    if (h) { h.setAttribute("aria-valuenow", String(px)); h.setAttribute("aria-valuemin", String(SBW_MIN)); h.setAttribute("aria-valuemax", String(SBW_MAX)); }
    if (persist) { try { global.localStorage.setItem(SBW_KEY, String(px)); } catch (e) { /* storage blocked */ } }
    var PP = global.PlanPage;
    if (PP && PP.state && PP.state.cy) { try { PP.state.cy.resize(); } catch (e) { /* not ready */ } }
    return px;
  }

  function bindResize() {
    var h = $("sb-resize"), sb = $("sidebar");
    if (!h || !sb) return;
    var w0 = storedWidth();
    if (w0) setWidth(w0, false);
    var startX = 0, startW = 0, dragging = false;
    h.addEventListener("pointerdown", function (e) {
      dragging = true; startX = e.clientX; startW = sb.getBoundingClientRect().width;
      document.body.classList.add("sb-resizing");
      try { h.setPointerCapture(e.pointerId); } catch (x) { /* synthetic events */ }
      e.preventDefault();
    });
    h.addEventListener("pointermove", function (e) { if (dragging) setWidth(startW + (e.clientX - startX), false); });
    function end(e) {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("sb-resizing");
      setWidth(sb.getBoundingClientRect().width, true);
    }
    h.addEventListener("pointerup", end);
    h.addEventListener("pointercancel", end);
    h.addEventListener("dblclick", function () { setWidth(260, true); });
    h.addEventListener("keydown", function (e) {
      var w = sb.getBoundingClientRect().width;
      if (e.key === "ArrowLeft") { setWidth(w - 16, true); e.preventDefault(); }
      else if (e.key === "ArrowRight") { setWidth(w + 16, true); e.preventDefault(); }
    });
  }

  function mount(store) {
    ui = store;
    var bound = {};
    Object.keys(TABS).forEach(function (k) {
      bound[TABS[k]] = true;
      var t = $(TABS[k]);
      if (t) t.addEventListener("click", function () { show(k); });
    });
    /* The phone menu drawer (#sidebar) reaches pages with no tabbar entry. */
    ["activity", "progress"].forEach(function (pg) {
      Array.prototype.forEach.call(document.querySelectorAll('[data-page="' + pg + '"]'), function (b) {
        if (b && !bound[b.getAttribute("id")]) b.addEventListener("click", function () { show(pg); });
      });
    });
    bindResize();
  }

  PlanUI.Pages = { mount: mount, show: show, current: function () { return cur; }, setSidebarWidth: setWidth, onChange: null };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
