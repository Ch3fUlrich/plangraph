/* PlanGraph Graph Extensions Registry & Style Rules (GU-G2b0)
   ES5/ES2015 without modules, namespace PlanUI.
   File limit: <= 300 lines. */
(function (global) {
  "use strict";

  var PlanUI = global.PlanUI || (global.PlanUI = {});

  var extensions = [];
  var fieldRegistry = {};
  var errorsList = [];

  function addError(extId, hookName, err) {
    if (errorsList.length < 20) {
      errorsList.push({
        ext: extId || "",
        hook: hookName || "",
        message: String((err && err.message) || err)
      });
    }
  }

  function addField(name, fn, extId) {
    fieldRegistry[name] = { fn: fn, extId: extId || "fields" };
  }

  function fields(ctx) {
    var res = {};
    var keys = Object.keys(fieldRegistry);
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var item = fieldRegistry[k];
      try {
        res[k] = typeof item.fn === "function" ? item.fn(ctx) : item.fn;
      } catch (e) {
        res[k] = null;
        addError(item.extId, k, e);
      }
    }
    return res;
  }

  var GraphExt = {
    errors: errorsList,
    register: function (ext) {
      if (!ext || !ext.id) return;
      var foundIndex = -1;
      for (var i = 0; i < extensions.length; i++) {
        if (extensions[i].id === ext.id) {
          foundIndex = i;
          break;
        }
      }
      if (foundIndex >= 0) {
        extensions[foundIndex] = ext;
      } else {
        extensions.push(ext);
      }
      if (ext.fields && typeof ext.fields === "object") {
        var fkeys = Object.keys(ext.fields);
        for (var fi = 0; fi < fkeys.length; fi++) {
          var fk = fkeys[fi];
          addField(fk, ext.fields[fk], ext.id);
        }
      }
    },
    unregisterAll: function () {
      extensions.length = 0;
      fieldRegistry = {};
      errorsList.length = 0;
    },
    list: function () {
      var ids = [];
      for (var i = 0; i < extensions.length; i++) {
        ids.push(extensions[i].id);
      }
      return ids;
    },
    addField: addField,
    fields: fields,
    call: function (name, arg, ctx) {
      if (name === "styles") {
        var allStyles = [];
        for (var si = 0; si < extensions.length; si++) {
          var sext = extensions[si];
          if (typeof sext.styles === "function") {
            try {
              var sres = sext.styles(arg);
              if (Array.isArray(sres)) {
                allStyles = allStyles.concat(sres);
              }
            } catch (se) {
              addError(sext.id, name, se);
            }
          }
        }
        return allStyles;
      }

      if (name === "state") {
        var mergedPatch = null;
        for (var pi = 0; pi < extensions.length; pi++) {
          var pext = extensions[pi];
          if (typeof pext.state === "function") {
            try {
              var patch = pext.state(ctx);
              if (patch && typeof patch === "object") {
                if (!mergedPatch) mergedPatch = {};
                var pkeys = Object.keys(patch);
                for (var pki = 0; pki < pkeys.length; pki++) {
                  mergedPatch[pkeys[pki]] = patch[pkeys[pki]];
                }
              }
            } catch (pe) {
              addError(pext.id, name, pe);
            }
          }
        }
        return mergedPatch;
      }

      if (name === "transformElements") {
        var current = arg;
        for (var ti = 0; ti < extensions.length; ti++) {
          var text = extensions[ti];
          if (typeof text.transformElements === "function") {
            try {
              var next = text.transformElements(current, ctx);
              if (next && typeof next === "object") {
                current = next;
              }
            } catch (te) {
              addError(text.id, name, te);
            }
          }
        }
        return current;
      }

      if (name === "init" || name === "afterSync" || name === "bind") {
        for (var ci = 0; ci < extensions.length; ci++) {
          var cext = extensions[ci];
          if (typeof cext[name] === "function") {
            try {
              cext[name](ctx);
            } catch (ce) {
              addError(cext.id, name, ce);
            }
          }
        }
        return undefined;
      }

      return undefined;
    }
  };

  PlanUI.GraphExt = GraphExt;

  /* D-677 wait outlines. */
  GraphExt.register({ id: "operator-wait", styles: function (env) {
    var cv = (env && env.cssVar) || function () { return ""; };
    return [{ selector: "node.wait-answer", style: { "outline-width": 3, "outline-color": cv("--wait-answer") || "#b45309" } },
      { selector: "node.wait-task", style: { "outline-width": 3, "outline-color": cv("--wait-task") || "#7c3aed" } }];
  } });

  function graphStyleRules(env) {
    var cv = (env && typeof env.cssVar === "function") ? env.cssVar : function () { return ""; };
    var fg = cv("--fg") || "#15171c",
      panel = cv("--panel") || "#ffffff",
      bdr = cv("--border") || "#d5d8de",
      crit = cv("--critical") || "#cc79a7",
      mut = cv("--muted") || "#5b616e",
      acc = cv("--accent") || "#33415c";

    var s = [
      { selector: "node", style: { label: "data(label)", "font-family": "system-ui, -apple-system, Segoe UI, Roboto, sans-serif", "font-size": 15, "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 216, "text-overflow-wrap": "anywhere", "line-height": 1.25, color: fg, "background-color": panel, "border-width": 2, "border-color": bdr, shape: "round-rectangle" } },
      { selector: "node[width][height]", style: { width: "data(width)", height: "data(height)" } },
      { selector: "node.task-node", style: { "background-fill": "linear-gradient", "background-gradient-direction": "to-right", "background-gradient-stop-colors": function (el) { var c = el.data("color") || "#2f6f4f"; return c + " " + c + " " + panel + " " + panel; }, "background-gradient-stop-positions": "0% 2.5% 2.5% 100%" } },
      { selector: "node.critical-node", style: { "border-color": crit, "border-width": 3 } },
      { selector: "node.loop-node", style: { "border-style": "double", "border-width": 4 } },
      { selector: "node.more-node", style: { "border-style": "dotted", "border-width": 2, "border-color": mut, "background-fill": "solid", "font-style": "italic" } },
      { selector: "node.exit-node", style: { shape: "diamond", width: 100, height: 40, "font-size": 11, "text-wrap": "ellipsis", "text-max-width": 90, "background-fill": "solid" } },
      { selector: "node.exit-node.hollow", style: { "background-color": panel, "border-color": cv("--state-open") || "#e69f00", color: fg } },
      { selector: "node.exit-node.filled", style: { "background-color": cv("--state-ready") || "#009e73", "border-color": cv("--state-ready") || "#009e73", color: "#ffffff" } },
      { selector: "node.ghost-node, node.ghost-stub", style: { "border-style": "dashed", "border-width": 2, "border-color": mut, "font-size": 12, "text-wrap": "ellipsis", "text-max-width": 180, "background-fill": "solid", "background-color": panel } },
      { selector: "node.gate-node", style: { shape: "hexagon" } },
      { selector: "node.goal-node", style: { shape: "round-rectangle", "border-width": 3, "border-color": crit } },
      { selector: ":parent", style: { "padding-top": 44, "padding-left": 16, "padding-bottom": 16, "padding-right": 16, "compound-sizing-wrt-labels": "exclude", "text-valign": "top", "text-halign": "center", "background-opacity": 0.04, "background-color": acc, "text-wrap": "wrap", "text-max-width": function (ele) { return Math.max(120, ele.width() - 28); } } },
      { selector: "node.project-compound", style: { "font-size": 20, "font-weight": "bold", "border-width": 2, "border-color": function (ele) { return ele.data("color") || bdr; }, "text-wrap": "wrap" } },
      { selector: "node.phase-compound", style: { "font-size": 16, "font-weight": "normal", "border-width": 1.5, "border-color": function (ele) { var c = ele.data("color"); return c ? PlanUI.colorTint(c, -0.25) : bdr; }, "text-wrap": "wrap" } },
      { selector: "node.phase-collapsed, node.project-collapsed", style: { shape: "round-rectangle", width: 300, height: 72, label: function (ele) { return PlanUI.wrapLabel(ele.data("label"), 272, PlanUI.measurer(ele)); }, "text-wrap": "wrap", "text-max-width": 10000, "border-width": 2, "border-color": bdr, "text-valign": "center", "text-halign": "center", "background-fill": "linear-gradient", "background-gradient-direction": "to-right", "background-gradient-stop-colors": function (el) { var c = el.data("color") || "#2f6f4f"; return c + " " + c + " " + panel + " " + panel; }, "background-gradient-stop-positions": function (el) { var tot = el.data("total") || 0, d = el.data("done") || 0; var pct = tot > 0 ? Math.round((d / tot) * 100) : 0; return "0% " + pct + "% " + pct + "% 100%"; } } },
      { selector: "node.project-collapsed", style: { "font-size": 24, "font-weight": "bold" } },
      { selector: "node.phase-collapsed", style: { "font-size": 20, "font-weight": "normal" } },
      { selector: "node:selected", style: { "border-width": 4, "border-color": acc } },
      { selector: "edge", style: { "curve-style": "taxi", "taxi-direction": "rightward", "taxi-turn": "50%", width: 2, "target-arrow-shape": "triangle", "arrow-scale": 1.2, "line-color": bdr, "target-arrow-color": bdr, label: "data(label)", "font-size": 11, color: mut, "text-rotation": "autorotate", "text-background-opacity": 0.8, "text-background-color": cv("--bg") || "#ffffff", "text-background-padding": 2 } },
      { selector: "edge[width]", style: { width: "data(width)" } },
      { selector: "edge.critical-edge", style: { "line-color": crit, "target-arrow-color": crit, width: 4 } },
      { selector: ".dim", style: { opacity: 0.15 } }
    ];
    ["ready", "blocked", "claimed", "done", "open"].forEach(function (st) { s.push({ selector: "node.state-" + st, style: { "border-color": cv("--state-" + st), "border-style": "solid" } }); });
    ["in_review", "cancelled"].forEach(function (st) { s.push({ selector: "node.state-" + st, style: { "border-color": cv("--state-" + st), "border-style": "dashed" } }); });

    var extRules = GraphExt.call("styles", env);
    if (Array.isArray(extRules) && extRules.length) {
      s = s.concat(extRules);
    }
    return s;
  }

  PlanUI.graphStyleRules = graphStyleRules;

  function freshCanvasSize(cy) {
    if (!cy) return { w: 0, h: 0 };
    if (typeof cy.resize === "function") cy.resize();
    var w = typeof cy.width === "function" ? cy.width() : 0;
    var h = typeof cy.height === "function" ? cy.height() : 0;
    return { w: w, h: h };
  }

  PlanUI.freshCanvasSize = freshCanvasSize;

  function installHook() {
    if (typeof window === "undefined") return;
    window.__planGraphUi = function () {
      var cy = PlanUI.GraphView && typeof PlanUI.GraphView.getCy === "function" ? PlanUI.GraphView.getCy() : null;
      var nodeCount = 0, edgeCount = 0, visibleEdgeCount = 0;
      var collapsedPhaseCount = 0, expandedPhaseCount = 0;
      var collapsedProjectCount = 0, projectCompoundCount = 0, taskCount = 0;

      if (cy && typeof cy.nodes === "function") {
        nodeCount = cy.nodes().length;
        collapsedPhaseCount = cy.nodes(".phase-collapsed").length;
        expandedPhaseCount = cy.nodes(".phase-compound").length;
        collapsedProjectCount = cy.nodes(".project-collapsed").length;
        projectCompoundCount = cy.nodes(".project-compound").length;
        taskCount = cy.nodes(".task-node").length;
      }
      if (cy && typeof cy.edges === "function") {
        var edges = cy.edges();
        edgeCount = edges.length;
        for (var i = 0; i < edges.length; i++) {
          var e = edges[i];
          var disp = typeof e.style === "function" ? e.style("display") : "";
          var op = typeof e.style === "function" ? parseFloat(e.style("opacity")) : 1;
          if (disp !== "none" && (isNaN(op) || op > 0)) {
            visibleEdgeCount++;
          }
        }
      }

      return {
        extensions: GraphExt.list(),
        extErrors: GraphExt.errors.length,
        fields: GraphExt.fields(),
        nodeCount: nodeCount,
        edgeCount: edgeCount,
        visibleEdgeCount: visibleEdgeCount,
        collapsedPhaseCount: collapsedPhaseCount,
        expandedPhaseCount: expandedPhaseCount,
        collapsedProjectCount: collapsedProjectCount,
        projectCompoundCount: projectCompoundCount,
        taskCount: taskCount
      };
    };
  }

  installHook();

})(typeof window !== "undefined" ? window : globalThis);
