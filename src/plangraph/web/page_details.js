/* plangraph page — details panel, list view, tabs and boot.
 *
 * Uses the shared state and graph from page_ui.js via `window.PlanPage`.
 * Read-only. */
(function (global) {
  "use strict";

  var PP = global.PlanPage;
  var PG = global.PlanGraph;
  if (!PP || !PG || typeof document === "undefined") return;

  var state = PP.state;

  /* ---- details ------------------------------------------------------------ */

  function addField(dl, label, value) {
    dl.appendChild(PP.el("dt", null, label));
    var dd = PP.el("dd");
    if (value && value.nodeType) dd.appendChild(value);
    else PP.setText(dd, value);
    dl.appendChild(dd);
  }

  function addKeys(dl, label, keys) {
    dl.appendChild(PP.el("dt", null, label));
    var dd = PP.el("dd", "keys");
    if (!keys || !keys.length) {
      dd.appendChild(PP.el("span", "muted", "none"));
    } else {
      keys.forEach(function (k) {
        var known = !!state.byKey[k];
        var b = PP.el("button", "keylink" + (known ? "" : " unknown"), k);
        b.type = "button";
        if (known) {
          b.addEventListener("click", function () {
            focusNode(k);
          });
        } else {
          b.disabled = true;
        }
        dd.appendChild(b);
      });
    }
    dl.appendChild(dd);
  }

  function dependentsOf(key) {
    var out = [];
    state.items.forEach(function (it) {
      if (PG.isTerminal(it)) return; // match the SPEC "unblocks" definition
      if ((it.depends_on || []).indexOf(key) >= 0) out.push(it.key);
    });
    return out;
  }

  function renderDetails(key) {
    var box = PP.$("details");
    PP.clear(box);
    var it = key ? state.byKey[key] : null;
    if (!it) {
      box.hidden = true;
      return;
    }
    box.hidden = false;

    var head = PP.el("div", "details-head");
    head.appendChild(PP.el("h2", null, key));
    var close = PP.el("button", "details-close", "\u00d7");
    close.type = "button";
    close.setAttribute("aria-label", "Close details");
    close.addEventListener("click", function () {
      box.hidden = true;
    });
    head.appendChild(close);
    box.appendChild(head);
    box.appendChild(PP.el("p", "details-title", it.title || ""));

    var st = state.display[key] || it.state;
    var badges = PP.el("div", "badges");
    badges.appendChild(PP.el("span", "badge badge-" + st, st));
    if (it.kind) badges.appendChild(PP.el("span", "badge badge-kind", it.kind));
    if (it.operator) badges.appendChild(PP.el("span", "badge badge-op", "OPERATOR"));
    if (Object.prototype.hasOwnProperty.call(state.criticalIndex, key)) {
      badges.appendChild(PP.el("span", "badge badge-critical", "critical path"));
    } else if (state.unblocks[key]) {
      badges.appendChild(PP.el("span", "badge", "unblocks " + state.unblocks[key]));
    }
    box.appendChild(badges);

    var dl = PP.el("dl", "fields");
    [
      ["project", it.project],
      ["id", it.id],
      ["track", it.track],
      ["owner", it.owner],
      ["state", it.state],
      ["display state", st],
      ["priority", it.priority],
      ["estimate_h", it.estimate_h],
      ["estimate_src", it.estimate_src],
      ["operator", it.operator],
      ["source", it.source],
      ["epoch", it.epoch],
      ["claimed_by", it.claimed_by],
      ["worker_run", it.worker_run],
      ["reviewer_run", it.reviewer_run],
      ["lease_until", it.lease_until],
      ["model", it.model],
      ["updated_at", it.updated_at]
    ].forEach(function (f) {
      if (f[1] === undefined || f[1] === null || f[1] === "") return;
      addField(dl, f[0], typeof f[1] === "boolean" ? String(f[1]) : f[1]);
    });
    addKeys(dl, "depends_on", it.depends_on || []);
    addKeys(dl, "unblocks", dependentsOf(key));
    if (it.corrects) addKeys(dl, "corrects", [it.corrects]);
    addKeys(dl, "corrected_by", it.corrected_by || []);
    box.appendChild(dl);

    if (it.done_criteria) {
      box.appendChild(PP.el("h3", null, "Done criteria"));
      box.appendChild(PP.el("p", "criteria", it.done_criteria));
    }
    if (it.evidence_kinds && it.evidence_kinds.length) {
      box.appendChild(PP.el("h3", null, "Evidence kinds"));
      box.appendChild(PP.el("p", null, it.evidence_kinds.join(", ")));
    }
    box.appendChild(PP.el("h3", null, "Evidence"));
    var evidence = it.evidence || [];
    if (!evidence.length) {
      box.appendChild(PP.el("p", "muted", "No evidence recorded."));
    } else {
      var ul = PP.el("ul", "evidence");
      evidence.forEach(function (e) {
        var li = PP.el("li");
        li.appendChild(PP.el("span", "ev-kind", e.kind || "?"));
        li.appendChild(PP.el("span", "ev-ref", e.ref || ""));
        if (e.verified) li.appendChild(PP.el("span", "ev-ok", "verified"));
        if (e.at) li.appendChild(PP.el("span", "muted", e.at));
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
  }

  function selectNode(key) {
    state.selected = key;
    if (state.cy) {
      var n = state.cy.getElementById(key);
      if (n && n.length) n.select();
    }
    renderDetails(key);
  }

  function focusNode(key) {
    setTab("graph");
    selectNode(key);
    if (state.cy) {
      var n = state.cy.getElementById(key);
      if (n && n.length) {
        state.cy.animate({ center: { eles: n } }, { duration: 250 });
        n.select();
      }
    }
  }

  /* ---- list view ---------------------------------------------------------- */

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
    b.addEventListener("click", function () {
      selectNode(it.key);
    });
    li.appendChild(b);
    if (blockers && blockers.length) li.appendChild(PP.el("div", "row-blockers", "blocked by " + blockers.join(", ")));
    return li;
  }

  function section(box, title, rows) {
    box.appendChild(PP.el("h2", "list-h", title + " (" + rows.length + ")"));
    if (!rows.length) {
      box.appendChild(PP.el("p", "muted", "none"));
      return;
    }
    var ul = PP.el("ul", "list");
    rows.forEach(function (r) {
      ul.appendChild(r);
    });
    box.appendChild(ul);
  }

  function renderList() {
    var box = PP.$("list-content");
    PP.clear(box);
    var vis = PP.visibleItems();
    if (!vis.length) {
      box.appendChild(PP.el("p", "muted", state.items.length ? "No items match the current filters." : "This snapshot has no items."));
      return;
    }
    var ready = state.ready.filter(PP.visible);
    var inprog = state.inProgress.filter(PP.visible);
    var blocked = state.items.filter(function (it) {
      return PP.visible(it) && state.blocked[it.key];
    });
    var done = vis.filter(function (it) {
      return it.state === "done";
    });
    var cancelled = vis.filter(function (it) {
      return it.state === "cancelled";
    });

    section(box, "Ready", ready.map(function (it) {
      return row(it, "ready", null);
    }));
    section(box, "Blocked-by", blocked.map(function (it) {
      return row(it, state.display[it.key], state.blocked[it.key]);
    }));
    section(box, "In progress", inprog.map(function (it) {
      return row(it, state.display[it.key], null);
    }));
    section(box, "Done", done.map(function (it) {
      return row(it, "done", null);
    }));
    if (cancelled.length) {
      section(box, "Cancelled", cancelled.map(function (it) {
        return row(it, "cancelled", null);
      }));
    }
  }

  /* ---- tabs, filters, boot ------------------------------------------------ */

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
        else {
          state.cy.resize();
          state.cy.fit(undefined, 24);
        }
      };
      if (typeof global.requestAnimationFrame === "function") global.requestAnimationFrame(ready);
      else setTimeout(ready, 20);
    }
  }

  function rerender() {
    PP.renderGraph();
    renderList();
    PP.renderChips();
    PP.renderLegend();
    PP.renderHeader();
    if (state.selected) renderDetails(state.selected);
  }

  function bind() {
    PP.$("tab-graph").addEventListener("click", function () {
      setTab("graph");
    });
    PP.$("tab-list").addEventListener("click", function () {
      setTab("list");
    });
    PP.$("collapse-done").addEventListener("change", function (e) {
      state.collapseDone = !!e.target.checked;
      rerender();
    });
    if (global.matchMedia) {
      var mq = global.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () {
        PP.renderLegend();
        PP.renderGraph();
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  function init() {
    var data = PP.$("snapshot-data");
    if (!data) return;
    try {
      state.snapshot = JSON.parse(data.textContent);
    } catch (e) {
      state.snapshot = { items: [] };
    }
    if (!state.snapshot || typeof state.snapshot !== "object") state.snapshot = { items: [] };
    state.items = (state.snapshot.items || []).slice();
    state.projects = PP.uniqueSorted(state.items.map(function (i) {
      return i.project;
    }));
    state.tracks = PP.uniqueSorted(state.items.map(function (i) {
      return i.track;
    }));
    PP.recompute();
    PP.renderHeader();
    PP.renderChips();
    PP.renderLegend();
    bind();
    renderDetails(null);
    PP.renderGraph();
    renderList();
    var mobile = global.matchMedia && global.matchMedia("(max-width: 700px)").matches;
    setTab(mobile ? "list" : "graph");
  }

  PP.selectNode = selectNode;
  PP.focusNode = focusNode;
  PP.renderList = renderList;
  PP.renderDetails = renderDetails;
  PP.setTab = setTab;
  PP.rerender = rerender;
  PP.init = init;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(typeof window !== "undefined" ? window : globalThis);
