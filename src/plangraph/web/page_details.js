/* plangraph page — DetailPanel (docked 380 px, BottomSheet ≤ 700 px), the
 * legacy list view, tabs and init (UI-PLAN §9/§10, T2).
 *
 * Legacy contract kept intact: PlanPage.selectNode / focusNode / renderDetails /
 * renderList / setTab / rerender / init still exist and behave as before for
 * page_ui.js (graph tap, project chips). The NEW panel reads only `vm`
 * (PlanUI.buildViewModel) and `ui` (PlanUI.Store) — `ui.sel` drives it — never
 * the raw snapshot; all content is built with createElement/textContent.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  var PP = global.PlanPage;
  var PG = global.PlanGraph;
  if (!PP || !PG || typeof document === "undefined") return;

  var state = PP.state;
  var box = PP.$("details");
  var body = null;                                     // cleared per render
  var app = PlanUI.app || null;                        // set by page_boot (fallback below)

  function A() { return app || PlanUI.app; }           // {ui, vm} — vm/ui only (§2)
  function UI() { var a = A(); return a && a.ui; }
  function VM() { var a = A(); return a && a.vm; }
  function noData(msg) { var p = PP.el("p", "sb-empty", msg); return p; }

  /* ---- panel pieces ------------------------------------------------------- */

  function addField(dl, label, value) {
    dl.appendChild(PP.el("dt", null, label));
    dl.appendChild(PP.el("dd", null, value == null || value === "" ? "—" : String(value)));
  }

  /* deps/unblocks as clickable TITLES (§9): label = vm.humanize(key). */
  function addKeys(dl, label, keys) {
    dl.appendChild(PP.el("dt", null, label));
    var dd = PP.el("dd", "keys");
    var V = VM();
    if (!keys || !keys.length) dd.appendChild(PP.el("span", "muted", "none"));
    else keys.forEach(function (k) {
      var known = !!(V && V.items.has(k));
      var b = PP.el("button", "keylink" + (known ? "" : " unknown"),
                    V && known ? V.humanize(k) : k);
      b.type = "button";
      b.title = k;
      if (known) b.addEventListener("click", function () { focusNode(k); });
      else b.disabled = true;
      dd.appendChild(b);
    });
    dl.appendChild(dd);
  }

  function badgeRow(rec) {
    var b = PP.el("div", "badges");
    b.appendChild(PP.el("span", "badge badge-" + (rec.display || "open"), rec.display || rec.state));
    if (rec.kind) b.appendChild(PP.el("span", "badge badge-kind", rec.kind));
    if (rec.operator) b.appendChild(PP.el("span", "badge badge-op", "OPERATOR"));
    if (rec.critical) b.appendChild(PP.el("span", "badge badge-critical", "critical path"));
    else if (rec.unblocks && rec.unblocks.length) b.appendChild(PP.el("span", "badge", "unblocks " + rec.unblocks.length));
    if (rec.loop != null) b.appendChild(PP.el("span", "badge", "loop " + rec.loop));
    return b;
  }

  function evidenceList(rec) {
    if (!rec.evidence || !rec.evidence.length) return noData("No evidence recorded.");
    var ul = PP.el("ul", "evidence");
    rec.evidence.forEach(function (e) {
      var li = PP.el("li");
      li.appendChild(PP.el("span", "ev-kind", e.kind || "?"));
      var ref = e && e.ref && typeof e.ref === "object" ? (e.ref.id || e.ref.sha || e.ref.run_id || JSON.stringify(e.ref)) : e.ref;
      li.appendChild(PP.el("span", "ev-ref", ref || ""));
      if (e.verified) li.appendChild(PP.el("span", "ev-ok", "verified"));
      if (e.at) li.appendChild(PP.el("span", "muted", e.at));
      ul.appendChild(li);
    });
    return ul;
  }

  function actions(rec) {
    var row = PP.el("div", "dp-act");
    var show = PP.el("button", null, "Show in table");
    show.type = "button";
    show.addEventListener("click", function () {
      var U = UI();
      if (U) U.set("view", "table");
      setTab("list");
    });
    var expand = PP.el("button", null, "Expand in graph");
    expand.type = "button";
    expand.addEventListener("click", function () { focusNode(rec.key); });
    var copy = PP.el("button", null, "Copy key");
    copy.type = "button";
    copy.addEventListener("click", function () { copyKey(rec.key, copy); });
    row.appendChild(show); row.appendChild(expand); row.appendChild(copy);
    return row;
  }

  function copyKey(key, btn) {
    var done = function (ok) { btn.textContent = ok ? "Copied" : "Copy failed"; };
    try {
      if (global.navigator && navigator.clipboard && navigator.clipboard.writeText)
        navigator.clipboard.writeText(key).then(function () { done(true); }, function () { done(legacyCopy(key)); });
      else done(legacyCopy(key));
    } catch (e) { done(false); }
  }

  function legacyCopy(text) {                        // offline/artifact-safe path
    try {
      var t = document.createElement("textarea");
      t.value = text;
      t.setAttribute("readonly", "");
      t.style.position = "fixed";
      t.style.opacity = "0";
      document.body.appendChild(t);
      t.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(t);
      return ok;
    } catch (e) { return false; }
  }

  /* ---- render(key): breadcrumb, fields, deps/unblocks, loop, evidence ----- */
  function render(key) {
    if (!body) return;
    PP.clear(body);
    if (!key) { box.hidden = true; return; }
    box.hidden = false;
    var V = VM();
    var rec = V && V.items.get(key);
    if (!rec) {
      var noHead = PP.el("div", "details-head");
      noHead.appendChild(PP.el("h2", null, key));
      body.appendChild(noHead);
      body.appendChild(noData("Not in this snapshot."));
      return;
    }
    body.appendChild(PP.el("p", "dp-crumb",
      [rec.project, rec.phase, rec.title || key].filter(Boolean).join(" › ")));

    var head = PP.el("div", "details-head");
    head.appendChild(PP.el("h2", null, key));
    var close = PP.el("button", "details-close", "\u00d7");
    close.type = "button";
    close.setAttribute("aria-label", "Close details");
    close.addEventListener("click", function () { selectNode(null); });
    head.appendChild(close);
    body.appendChild(head);
    body.appendChild(PP.el("p", "details-title", rec.title || key));
    body.appendChild(badgeRow(rec));

    var dl = PP.el("dl", "fields");
    [["project", rec.project], ["phase", rec.phase], ["id", rec.id], ["kind", rec.kind],
     ["state", rec.state], ["display state", rec.display], ["owner", rec.owner],
     ["operator", rec.operator ? "true" : "false"], ["priority", rec.priority],
     ["estimate_h", rec.estimate_h], ["slack_h", rec.slack],
     ["review_tier", rec.reviewTier], ["updated_at", rec.updatedAt]
    ].forEach(function (f) { addField(dl, f[0], f[1]); });
    addKeys(dl, "depends_on", rec.deps);
    addKeys(dl, "unblocks", rec.unblocks);
    if (rec.crossDeps && rec.crossDeps.length) addKeys(dl, "cross-project", rec.crossDeps);
    body.appendChild(dl);

    if (rec.doneCriteria) {
      body.appendChild(PP.el("h3", null, "Done criteria"));
      body.appendChild(PP.el("p", "criteria", rec.doneCriteria));
    }
    if (rec.loop != null) {
      body.appendChild(PP.el("h3", null, "Loop"));
      var loop = PP.el("div", "dp-loop");
      loop.appendChild(PP.el("span", null, "Loop iteration " + rec.loop));
      var linked = (V.decisions || []).filter(function (d) {
        return (d.items || []).indexOf(key) >= 0;
      });
      loop.appendChild(PP.el("span", "dp-k",
        linked.length ? "decisions " + linked.map(function (d) { return d.id; }).join(", ")
                      : "no linked decision"));
      body.appendChild(loop);
    }
    body.appendChild(PP.el("h3", null, "Evidence"));
    body.appendChild(evidenceList(rec));
    body.appendChild(actions(rec));
  }

  /* ---- BottomSheet snaps: peek 120 px / half 50 vh / full 92 vh ---------- */
  function snap(dir) {
    var order = ["peek", "half", "full"];
    var i = order.indexOf(box.getAttribute("data-snap"));
    i = Math.max(0, Math.min(order.length - 1, (i < 0 ? 0 : i) + dir));
    box.setAttribute("data-snap", order[i]);
  }

  function bindHandle(h) {
    var startY = null, moved = 0;
    h.addEventListener("pointerdown", function (e) {
      startY = e.clientY; moved = 0;
      try { h.setPointerCapture(e.pointerId); } catch (err) { /* older engine */ }
    });
    h.addEventListener("pointermove", function (e) {
      if (startY === null) return;
      moved = Math.max(moved, Math.abs(e.clientY - startY));
    });
    h.addEventListener("pointerup", function (e) {
      if (startY === null) return;
      var dy = e.clientY - startY;
      startY = null;
      snap(moved < 6 ? 1 : (dy < 0 ? 1 : -1));       // tap cycles, swipe sets
    });
    h.addEventListener("keydown", function (e) {      // keyboard-equivalent drag
      if (e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") { snap(1); e.preventDefault(); }
      else if (e.key === "ArrowDown") { snap(-1); e.preventDefault(); }
    });
  }

  /* mount(): panel chrome once, then follow `ui.sel` (A10 keeps it docked). */
  function mount(ui, vm) {
    app = { ui: ui, vm: vm };
    PP.clear(box);
    var handle = PP.el("button", "dp-handle");
    handle.type = "button";
    handle.setAttribute("aria-label", "Resize details sheet (ArrowUp/ArrowDown)");
    handle.title = "Drag or press arrows to resize";
    bindHandle(handle);
    body = PP.el("div", "dp-body");
    box.appendChild(handle);
    box.appendChild(body);
    var U = UI();
    if (U) U.subscribe(function (keys) {
      if (keys.indexOf("sel") >= 0) {
        state.selected = U.get("sel");               // single reducer for sel
        render(state.selected);
      }
    });
    render(state.selected);
  }

  /* ---- legacy hooks (page_ui.js and the old page call these) -------------- */

  function selectNode(key) {
    state.selected = key;
    var U = UI();
    if (U && U.get("sel") !== key) U.set("sel", key);   // mount's subscriber renders
    else render(key);
    if (state.cy) {
      state.cy.nodes().unselect();
      if (key) {
        var n = state.cy.getElementById(key);
        if (n && n.length) n.select();
      }
    }
  }

  function focusNode(key) {
    setTab("graph");
    selectNode(key);
    var U = UI();
    if (U) U.set("view", "graph");
    if (state.cy) {
      var n = state.cy.getElementById(key);
      if (n && n.length) {
        state.cy.animate({ center: { eles: n } }, { duration: 250 });
        n.select();
      }
    }
  }

  /* ---- list view (legacy table stand-in until T4) ------------------------- */

  function row(it, st, blockers) {
    var li = PP.el("li", "row row-" + st);
    var b = PP.el("button", "row-main");
    b.type = "button";
    b.appendChild(PP.el("span", "row-key", it.key));
    b.appendChild(PP.el("span", "row-title", it.title || ""));
    var meta = "est " + PG.estimate(it) + "h";
    if (state.unblocks[it.key]) meta += " · unblocks " + state.unblocks[it.key];
    if (it.owner) meta += " · " + it.owner;
    b.appendChild(PP.el("span", "row-meta", meta));
    b.addEventListener("click", function () { selectNode(it.key); });
    li.appendChild(b);
    if (blockers && blockers.length) li.appendChild(PP.el("div", "row-blockers", "blocked by " + blockers.join(", ")));
    return li;
  }

  function section(box, title, rows) {
    box.appendChild(PP.el("h2", "list-h", title + " (" + rows.length + ")"));
    if (!rows.length) { box.appendChild(PP.el("p", "muted", "none")); return; }
    var ul = PP.el("ul", "list");
    rows.forEach(function (r) { ul.appendChild(r); });
    box.appendChild(ul);
  }

  function renderList() {
    var bx = PP.$("list-content");
    PP.clear(bx);
    var vis = PP.visibleItems();
    if (!vis.length) {
      bx.appendChild(PP.el("p", "muted", state.items.length ? "No items match the current filters." : "This snapshot has no items."));
      return;
    }
    var ready = state.ready.filter(PP.visible);
    var inprog = state.inProgress.filter(PP.visible);
    var blocked = state.items.filter(function (it) { return PP.visible(it) && state.blocked[it.key]; });
    var done = vis.filter(function (it) { return it.state === "done"; });
    var cancelled = vis.filter(function (it) { return it.state === "cancelled"; });
    section(bx, "Ready", ready.map(function (it) { return row(it, "ready", null); }));
    section(bx, "Blocked-by", blocked.map(function (it) { return row(it, state.display[it.key], state.blocked[it.key]); }));
    section(bx, "In progress", inprog.map(function (it) { return row(it, state.display[it.key], null); }));
    section(bx, "Done", done.map(function (it) { return row(it, "done", null); }));
    if (cancelled.length) section(bx, "Cancelled", cancelled.map(function (it) { return row(it, "cancelled", null); }));
  }

  /* ---- tabs, rerender, boot (legacy entry points) ------------------------- */

  function setTab(name) {
    PP.$("tab-graph").classList.toggle("active", name === "graph");
    PP.$("tab-list").classList.toggle("active", name === "list");
    PP.$("tab-graph").setAttribute("aria-selected", name === "graph" ? "true" : "false");
    PP.$("tab-list").setAttribute("aria-selected", name === "list" ? "true" : "false");
    PP.$("panel-graph").hidden = name !== "graph";
    PP.$("panel-list").hidden = name !== "list";
    if (name === "graph") {
      var ready = function () {
        if (!state.cy) PP.renderGraph();
        else { state.cy.resize(); }
      };
      if (typeof global.requestAnimationFrame === "function") global.requestAnimationFrame(ready);
      else setTimeout(ready, 20);
    }
  }

  function rerender() {
    PP.renderGraph();
    if (PP.renderTable) PP.renderTable(); else renderList();
    PP.renderChips();
    PP.renderLegend();
    PP.renderHeader();
    if (state.selected) render(state.selected);
  }

  function bind() {
    if (bind.done) return;
    bind.done = true;
    if (!global.PlanUI || !global.PlanUI.Pages) {      // legacy path without the page router
      PP.$("tab-graph").addEventListener("click", function () { setTab("graph"); var U = UI(); if (U) U.set("view", "graph"); });
      PP.$("tab-list").addEventListener("click", function () { setTab("list"); var U = UI(); if (U) U.set("view", "table"); });
    }
    PP.$("collapse-done").addEventListener("change", function (e) {
      state.collapseDone = !!e.target.checked;
      rerender();
    });
    if (global.matchMedia) {
      var mq = global.matchMedia("(prefers-color-scheme: dark)"), re = function () { PP.renderLegend(); if (state.cy) PP.renderGraph(); };
      if (mq.addEventListener) mq.addEventListener("change", re);
      else if (mq.addListener) mq.addListener(re);
    }
  }

  function initData() {                                // legacy path without boot
    var data = PP.$("snapshot-data");
    if (!data) return false;
    try { state.snapshot = JSON.parse(data.textContent); }
    catch (e) { state.snapshot = { items: [] }; }
    if (!state.snapshot || typeof state.snapshot !== "object") state.snapshot = { items: [] };
    state.items = (state.snapshot.items || []).slice();
    state.projects = PP.uniqueSorted(state.items.map(function (i) { return i.project; }));
    state.tracks = PP.uniqueSorted(state.items.map(function (i) { return i.track; }));
    PP.recompute();
    PP.renderHeader();
    PP.renderChips();
    PP.renderLegend();
    return true;
  }

  function init() {
    if (!state.booted && !state.snapshot) initData(); // page_boot pre-boot skips this
    bind();
    if (body) render(state.selected);
    renderList();
    PP.renderGraph();
    setTab("graph");
  }

  PP.selectNode = selectNode;
  PP.focusNode = focusNode;
  PP.renderList = renderList;
  PP.renderDetails = render;        // legacy name kept for page_ui.js callers
  PP.setTab = setTab;
  PP.rerender = rerender;
  PP.init = init;
  PP.initData = initData;
  PlanUI.DetailPanel = { mount: mount, render: render, snap: snap };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(typeof window !== "undefined" ? window : globalThis);
