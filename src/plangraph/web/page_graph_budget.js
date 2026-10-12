/* PlanUI graph budget model — edge budget applied to built elements (UI-PLAN GU-G4a).
 * Pure model: no DOM, no graph library instance, ES5/ES2015-safe, no modules.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  function isNodeDim(n) {
    if (!n) return true;
    return (" " + (n.classes || "") + " ").indexOf(" dim ") >= 0;
  }

  function cutTitle(str) {
    if (str == null) return "";
    str = String(str);
    if (str.length > 48) {
      return str.slice(0, 48) + "…";
    }
    return str;
  }

  /* B1: budgetElements(vm, ui, els, opts) -> { nodes, edges, report } */
  function budgetElements(vm, ui, els, opts) {
    var emptyReport = {
      limit: 0,
      total: 0,
      shown: 0,
      intraHidden: 0,
      hidden: 0,
      aggregated: 0,
      filteredOut: 0,
      ghostRouted: 0,
      compoundRouted: 0,
      structural: 0,
      dependency: 0
    };
    if (!vm || !els || !ui) {
      return { nodes: [], edges: [], report: emptyReport };
    }

    var nodes = Array.isArray(els.nodes) ? els.nodes.slice() : [];
    var elsEdges = Array.isArray(els.edges) ? els.edges : [];

    // B2: Structural edges
    var structuralEdges = [];
    elsEdges.forEach(function (e) {
      var s = (e && e.data && e.data.source) || (e && e.source) || "";
      var t = (e && e.data && e.data.target) || (e && e.target) || "";
      if (s.indexOf("g:") === 0 || t.indexOf("x:") === 0) {
        structuralEdges.push(e);
      }
    });

    var nodeMap = {};
    nodes.forEach(function (n) {
      if (n && n.data && n.data.id) {
        nodeMap[n.data.id] = n;
      }
    });

    var limitOpt = (opts && typeof opts.limit === "number" && opts.limit > 0 &&
      Math.floor(opts.limit) === opts.limit && isFinite(opts.limit)) ? opts.limit : undefined;

    var showAllEdges = !!(ui.settings && ui.settings.showAllEdges);
    var model = PlanUI.metaEdges(vm, ui, { showAllEdges: showAllEdges, limit: limitOpt });

    var report = {
      limit: model.limit,
      total: model.total,
      shown: model.shown,
      intraHidden: model.intraHidden,
      hidden: model.hidden,
      aggregated: model.aggregated,
      filteredOut: 0,
      ghostRouted: 0,
      compoundRouted: 0,
      structural: structuralEdges.length,
      dependency: 0
    };

    var flt = ui.filters || {};
    var edgeMode = flt.edgeMode;
    var query = ui.query ? String(ui.query).trim() : "";
    var matchMode = ui.matchMode || "dim";
    var hasEdgeFilter = !!(
      (edgeMode && edgeMode !== "all" && (edgeMode === "critical" || edgeMode === "cross" || edgeMode === "blocking")) ||
      (query && query.indexOf("edge:") >= 0)
    );

    var dependencyEdges = [];

    (model.edges || []).forEach(function (modelEdge) {
      var src = modelEdge.source;
      var tgt = modelEdge.target;

      // B3a: Loop exit source
      if (src.indexOf("t:") === 0) {
        var k = src.slice(2);
        if (nodeMap["x:" + k]) {
          src = "x:" + k;
        }
      }

      // B3b: Compound re-target
      var retargeted = false;
      if (!nodeMap[src]) {
        if (src.indexOf("phc:") === 0) {
          var phComp = "ph:" + src.slice(4);
          if (nodeMap[phComp]) {
            src = phComp;
            retargeted = true;
          }
        } else if (src.indexOf("pc:") === 0) {
          var pComp = "p:" + src.slice(3);
          if (nodeMap[pComp]) {
            src = pComp;
            retargeted = true;
          }
        }
      }

      if (!nodeMap[tgt]) {
        if (tgt.indexOf("phc:") === 0) {
          var phCompT = "ph:" + tgt.slice(4);
          if (nodeMap[phCompT]) {
            tgt = phCompT;
            retargeted = true;
          }
        } else if (tgt.indexOf("pc:") === 0) {
          var pCompT = "p:" + tgt.slice(3);
          if (nodeMap[pCompT]) {
            tgt = pCompT;
            retargeted = true;
          }
        }
      }

      // B3a cont: Drop if endpoint still not a node
      if (!nodeMap[src] || !nodeMap[tgt]) {
        report.filteredOut += modelEdge.count;
        return;
      }

      // B3c: Ghost routing
      if (modelEdge.exact && modelEdge.crossProject && tgt.indexOf("t:") === 0) {
        var pairs = modelEdge.pairs || [];
        var allGhosts = pairs.length > 0 && pairs.every(function (p) {
          return !!nodeMap["g:" + p.to + ">" + p.from];
        });
        if (allGhosts) {
          report.ghostRouted += modelEdge.count;
          return;
        }
      }

      // B3d: Filter
      var sn = nodeMap[src], tn = nodeMap[tgt];
      var bothDim = isNodeDim(sn) && isNodeDim(tn);
      var keep = !bothDim;

      if (hasEdgeFilter) {
        if (edgeMode === "critical" && !modelEdge.critical) {
          keep = false;
        } else if (edgeMode === "cross" && !modelEdge.crossProject) {
          keep = false;
        } else if (edgeMode === "blocking") {
          var hasBlocking = (modelEdge.pairs || []).some(function (p) {
            var it = (vm.items && typeof vm.items.get === "function") ? vm.items.get(p.from) : null;
            return it && it.display !== "done";
          });
          if (!hasBlocking) keep = false;
        }
        if (keep && query && query.indexOf("edge:") >= 0 && PlanUI.Query && typeof PlanUI.Query.edgeMatch === "function") {
          var hasMatch = (modelEdge.pairs || []).some(function (p) {
            return PlanUI.Query.edgeMatch({ source: p.from, target: p.to }, query, vm.items);
          });
          if (!hasMatch) keep = false;
        }
      }

      if (hasEdgeFilter && !keep && matchMode === "hide") {
        report.filteredOut += modelEdge.count;
        return;
      }

      // B3e: Emit edge
      var cnt = modelEdge.count;
      var crit = !!modelEdge.critical;
      var w = Math.round((2 + Math.log2(cnt)) * 100) / 100;
      if (crit) w = Math.max(w, 4);
      w = Math.min(8, w);

      var isMeta = (!modelEdge.exact || modelEdge.level === "meta");
      var isCross = !!modelEdge.crossProject;

      var cls = [];
      if (isMeta) cls.push("meta-edge");
      if (crit) cls.push("critical-edge");
      if (isCross) cls.push("cross-edge");
      if (retargeted) cls.push("compound-edge");

      var edgeDim = bothDim || (hasEdgeFilter && !keep);
      if (edgeDim) cls.push("dim");

      var memberCopies = (modelEdge.pairs || []).map(function (p) {
        return { from: p.from, to: p.to };
      });

      var edgeData = {
        id: modelEdge.id,
        source: src,
        target: tgt,
        count: cnt,
        label: cnt > 1 ? "×" + cnt : "",
        width: w,
        critical: crit,
        meta: isMeta,
        exact: !!modelEdge.exact,
        cross: isCross,
        compound: retargeted,
        members: memberCopies
      };

      if (retargeted) {
        report.compoundRouted += cnt;
      }
      report.dependency++;

      dependencyEdges.push({
        data: edgeData,
        classes: cls.join(" ")
      });
    });

    var finalEdges = structuralEdges.concat(dependencyEdges);
    return {
      nodes: nodes,
      edges: finalEdges,
      report: report
    };
  }

  /* B5: budgetSummary(report) -> string */
  function budgetSummary(report) {
    if (!report || typeof report.dependency !== "number") return "";
    var dep = report.dependency;
    var agg = report.aggregated || 0;
    var hid = report.hidden || 0;
    if (hid > 0 || agg > 0) {
      return dep + " edges (" + agg + " aggregated, " + hid + " hidden)";
    }
    return dep + " edges";
  }

  /* B6: edgeMembersTitle(vm, edgeData, max) -> string[] */
  function edgeMembersTitle(vm, edgeData, max) {
    var limit = (typeof max === "number" && max > 0 && Math.floor(max) === max) ? max : 12;
    var members = (edgeData && Array.isArray(edgeData.members)) ? edgeData.members : [];
    if (!members.length) return [];

    var items = (vm && vm.items) || null;
    var rows = [];
    if (typeof PlanUI.edgeBreakdown === "function") {
      var bd = PlanUI.edgeBreakdown(vm, { pairs: members }, limit);
      if (bd && Array.isArray(bd.rows)) {
        rows = bd.rows;
      }
    } else {
      rows = members.slice(0, limit).map(function (p) {
        var fi = (items && typeof items.get === "function") ? items.get(p.from) : null;
        var ti = (items && typeof items.get === "function") ? items.get(p.to) : null;
        return {
          from: p.from,
          to: p.to,
          fromTitle: fi ? (fi.title || "") : "",
          toTitle: ti ? (ti.title || "") : ""
        };
      });
    }

    var lines = rows.map(function (r) {
      var fromT = (r.fromTitle && String(r.fromTitle).trim()) ? r.fromTitle : r.from;
      var toT = (r.toTitle && String(r.toTitle).trim()) ? r.toTitle : r.to;
      return cutTitle(fromT) + " -> " + cutTitle(toT);
    });

    if (members.length > limit) {
      lines.push("+" + (members.length - limit) + " more");
    }
    return lines;
  }

  PlanUI.budgetElements = budgetElements;
  PlanUI.budgetSummary = budgetSummary;
  PlanUI.edgeMembersTitle = edgeMembersTitle;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
