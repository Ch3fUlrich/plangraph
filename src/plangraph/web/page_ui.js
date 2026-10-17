/* plangraph page — shared state, controls and the cytoscape graph.
 *
 * Read-only: nothing here mutates the snapshot or sends a request. Pure
 * derivations live in page.js (PlanGraph); the details/list/tab views live in
 * page_details.js and share this file's state through `window.PlanPage`. */
(function (global) {
  "use strict";

  var PG = global.PlanGraph;
  if (!PG || typeof document === "undefined") return;

  var state = {
    snapshot: null,
    items: [],
    byKey: {},
    projects: [],
    tracks: [],
    projectsOff: {},
    tracksOff: {},
    collapseDone: true,
    display: {},
    unblocks: {},
    critical: { keys: [], hours: 0 },
    criticalIndex: {},
    ready: [],
    blocked: {},
    inProgress: [],
    selected: null,
    cy: null
  };

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function clear(n) {
    while (n.firstChild) n.removeChild(n.firstChild);
  }

  function setText(n, t) {
    n.textContent = t === undefined || t === null ? "" : String(t);
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function uniqueSorted(values) {
    var seen = {};
    var out = [];
    values.forEach(function (v) {
      if (v !== undefined && v !== null && !seen[v]) {
        seen[v] = true;
        out.push(v);
      }
    });
    return out.sort();
  }

  function effectivePhase(it) {
    return it.phase || it.track || "";
  }

  function visible(it) {
    return !state.projectsOff[it.project] && !state.tracksOff[effectivePhase(it)];
  }

  function visibleItems() {
    return state.items.filter(visible);
  }

  function recompute() {
    state.byKey = PG.indexByKey(state.items);
    state.display = PG.computeDisplayState(state.items);
    state.unblocks = PG.unblocksMap(state.items);
    state.critical = PG.criticalPath(state.items);
    state.criticalIndex = {};
    state.critical.keys.forEach(function (k, i) {
      state.criticalIndex[k] = i;
    });
    state.ready = PG.readyList(state.items);
    state.blocked = PG.blockedBy(state.items);
    state.inProgress = PG.inProgress(state.items);
  }

  /* ---- header, chips, legend --------------------------------------------- */

  /* page_stamp.js loads after this file (JS_FILES order), so register the
   * 60 s clock-refresh callback on the first render, when Stamp is present. */
  var clockRegistered = false;

  function renderHeader() {
    var n = state.items.length;
    var snap = state.snapshot || {};
    var meta = $("meta-line");
    var suffix = " · " + n + " item" + (n === 1 ? "" : "s");
    var Stamp = global.PlanUI.Stamp;
    if (!clockRegistered) {
      clockRegistered = true;
      Stamp.setRefresh(renderHeader); // V-SHELL-4: start the 60 s re-render clock
    }
    var st = Stamp.format(Stamp.buildSha(), snap.generated, snap.head_seq, snap.head, Date.now());
    setText(meta, st.text + suffix);
    if (st.title) meta.setAttribute("title", st.title);
    else meta.removeAttribute("title");
    var cls = String(meta.className || "").split(/\s+/).filter(function (c) {
      return c && c !== "stamp-stale";
    });
    if (st.stale) cls.push("stamp-stale"); // V-SHELL-4: amber past 15 min
    meta.className = cls.join(" ");
    var pe = Math.round(PG.percentDone(state.items, "estimate") * 100);
    var pc = Math.round(PG.percentDone(state.items, "count") * 100);
    $("progress-bar").style.width = pe + "%";
    setText($("progress-text"), pe + "% done by estimate · " + pc + "% by count");
  }

  function chip(label, on, onToggle, color) {
    var b = el("button", "chip" + (on ? " on" : ""));
    b.type = "button";
    b.setAttribute("aria-pressed", on ? "true" : "false");
    if (color) {
      var dot = el("span", "chip-dot");
      dot.style.background = color;
      b.appendChild(dot);
      b.appendChild(el("span", "chip-label", label));
    } else {
      b.textContent = String(label);
    }
    b.addEventListener("click", function () {
      onToggle(!on);
    });
    return b;
  }

  /* The project/phase filter helpers (tracksOf/pushFilters/toggleFilter and
   * the chip stub) live in page_ui_filters.js — same PlanPage hooks. */
  function renderLegend() {
    var box = $("legend");
    clear(box);
    box.appendChild(el("span", "legend-title", "Legend"));
    [
      ["state-ready", "ready"],
      ["state-blocked", "blocked"],
      ["state-claimed", "claimed"],
      ["state-in_review", "in review"],
      ["state-done", "done"],
      ["state-cancelled", "cancelled"],
      ["critical", "critical path"],
      ["group", "done (collapsed)"],
      ["wait-answer", "Needs your answer"], ["wait-task", "Needs your task"]   // D-677
    ].forEach(function (pair) {
      var item = el("span", "legend-item");
      var sw = el("span", "swatch");
      sw.style.background = cssVar("--" + pair[0]);
      item.appendChild(sw);
      item.appendChild(el("span", null, pair[1]));
      box.appendChild(item);
    });
  }

  /* ---- graph -------------------------------------------------------------- */

  function nodeData(it) {
    return {
      id: it.key,
      key: it.key,
      label: String(it.key).split(":").pop(),
      title: it.title || "",
      kind: it.kind || "lane",
      track: it.track || "",
      project: it.project || "",
      state: state.display[it.key] || it.state,
      critical: Object.prototype.hasOwnProperty.call(state.criticalIndex, it.key),
      operator: !!it.operator,
      estimate: PG.estimate(it),
      unblocks: state.unblocks[it.key] || 0
    };
  }

  function buildElements() {
    var vis = visibleItems();
    var nodes = [];
    var edges = [];
    var visibleKeys = {};
    var doneGroup = {};
    var groups = {};

    vis.forEach(function (it) {
      if (it.state === "done" && state.collapseDone) {
        groups[it.track] = (groups[it.track] || 0) + 1;
        doneGroup[it.key] = "__done__" + it.track;
        return;
      }
      visibleKeys[it.key] = true;
      var st = state.display[it.key] || it.state;
      var cls = "state-" + st + " kind-" + (it.kind || "lane");
      if (Object.prototype.hasOwnProperty.call(state.criticalIndex, it.key)) cls += " critical";
      nodes.push({ data: nodeData(it), classes: cls });
    });

    Object.keys(groups).forEach(function (tr) {
      nodes.push({
        data: {
          id: "__done__" + tr,
          key: "__done__" + tr,
          label: groups[tr] + " done",
          kind: "group",
          track: tr,
          project: "",
          state: "done",
          synthetic: true
        },
        classes: "state-done kind-group"
      });
    });

    var seen = {};
    vis.forEach(function (it) {
      if (!visibleKeys[it.key]) return;
      var targetIdx = Object.prototype.hasOwnProperty.call(state.criticalIndex, it.key)
        ? state.criticalIndex[it.key]
        : -1;
      (it.depends_on || []).forEach(function (d) {
        var src = visibleKeys[d] ? d : doneGroup[d];
        if (!src) return;
        var id = src + "->" + it.key;
        if (seen[id]) return;
        seen[id] = true;
        var srcIdx = Object.prototype.hasOwnProperty.call(state.criticalIndex, src)
          ? state.criticalIndex[src]
          : -2;
        edges.push({
          data: {
            id: id,
            source: src,
            target: it.key,
            critical: srcIdx >= 0 && targetIdx === srcIdx + 1,
            blocked: state.display[it.key] === "blocked",
            goal: it.kind === "goal"
          },
          classes: srcIdx >= 0 && targetIdx === srcIdx + 1 ? "critical" : ""
        });
      });
    });

    var incoming = {};
    edges.forEach(function (e) {
      incoming[e.data.target] = true;
    });
    nodes.forEach(function (n) {
      if (!incoming[n.data.key]) {
        edges.push({
          data: { id: "__start__->" + n.data.key, source: "__start__", target: n.data.key, start: true },
          classes: "start"
        });
      }
    });
    nodes.unshift({
      data: { id: "__start__", key: "__start__", label: "START", kind: "start", synthetic: true, state: "start" },
      classes: "start"
    });
    return nodes.concat(edges);
  }

  function cyStyles() {
    var list = [
      {
        selector: "node",
        style: {
          label: "data(label)",
          color: cssVar("--fg"),
          "font-size": 11,
          "text-valign": "center",
          "text-halign": "center",
          "text-wrap": "ellipsis",
          "text-max-width": "110px",
          width: 118,
          height: 40,
          "background-color": cssVar("--state-done"),
          "border-width": 2,
          "border-color": cssVar("--border")
        }
      }
    ];
    // state fills first, so kind/start/critical rules below win on a tie
    ["ready", "blocked", "claimed", "in_review", "open", "done", "cancelled"].forEach(function (s) {
      list.push({
        selector: "node.state-" + s,
        style: { "background-color": cssVar("--state-" + s), color: cssVar("--state-" + s + "-fg") }
      });
    });
    list.push(
      { selector: "node.kind-goal", style: { shape: "round-rectangle", "border-width": 3, "border-color": cssVar("--critical"), width: 120 } },
      { selector: "node.kind-group", style: { shape: "round-rectangle", "background-color": cssVar("--group"), color: "#ffffff", "border-style": "dashed" } },
      { selector: "node.start", style: { "background-color": cssVar("--state-start"), color: cssVar("--state-start-fg"), width: 70, height: 34 } },
      { selector: "node.critical", style: { "border-width": 4, "border-color": cssVar("--critical") } },
      { selector: "edge", style: { width: 1.5, "curve-style": "bezier", "target-arrow-shape": "triangle", "arrow-scale": 0.8, "line-color": cssVar("--border"), "target-arrow-color": cssVar("--border") } },
      { selector: "edge.start", style: { "line-style": "dashed", "line-color": cssVar("--muted"), "target-arrow-color": cssVar("--muted") } },
      { selector: "edge.critical", style: { width: 3, "line-color": cssVar("--critical"), "target-arrow-color": cssVar("--critical") } },
      { selector: "node:selected", style: { "border-width": 4, "border-color": cssVar("--accent") } }
    );
    return list;
  }

  function registerDagre() {
    try {
      if (global.cytoscape && global.cytoscapeDagre) global.cytoscape.use(global.cytoscapeDagre);
    } catch (e) {
      /* fall back to the built-in layout */
    }
  }

  function updateEmpty() {
    var empty = $("graph-empty");
    var none = visibleItems().length === 0;
    empty.hidden = !none;
    if (none) empty.textContent = state.items.length ? "No items match the current filters." : "This snapshot has no items.";
  }

  function renderGraph() {
    updateEmpty();
    var container = $("cy");
    if (!global.cytoscape) {
      container.textContent = "Graph library unavailable.";
      return;
    }
    registerDagre();
    if (state.cy) {
      state.cy.destroy();
      state.cy = null;
    }
    if (!container.clientWidth || !container.clientHeight) return; // shown later
    var options = {
      container: container,
      elements: buildElements(),
      style: cyStyles(),
      layout: { name: "dagre", rankDir: "LR", nodeSep: 34, rankSep: 90, edgeSep: 16, animate: false, fit: true, padding: 24 },
      wheelSensitivity: 0.2,
      minZoom: 0.15,
      maxZoom: 3,
      userZoomingEnabled: true,
      userPanningEnabled: true,
      boxSelectionEnabled: false,
      autoungrabify: true,
      selectionType: "single"
    };
    try {
      state.cy = global.cytoscape(options);
    } catch (e) {
      // dagre extension missing — fall back to a layered built-in layout
      options.layout = { name: "breadthfirst", directed: true, spacingFactor: 1.2, animate: false, fit: true, padding: 24 };
      state.cy = global.cytoscape(options);
    }
    state.cy.on("tap", "node", function (ev) {
      if (global.PlanPage.selectNode) global.PlanPage.selectNode(ev.target.data("key"));
    });
  }

  global.PlanPage = {
    state: state,
    $: $,
    el: el,
    clear: clear,
    setText: setText,
    cssVar: cssVar,
    uniqueSorted: uniqueSorted,
    visible: visible,
    visibleItems: visibleItems,
    recompute: recompute,
    renderHeader: renderHeader,
    /* toggleFilter/isFilterOn/renderChips attach here from page_ui_filters.js. */
    renderLegend: renderLegend,
    renderGraph: renderGraph
  };
})(typeof window !== "undefined" ? window : globalThis);
