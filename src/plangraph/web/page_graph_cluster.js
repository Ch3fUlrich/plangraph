/* PlanUI graph cluster model — pure overview state, meta-edges with edge budget,
 * cluster summaries, and edge breakdown (UI-PLAN GU-G1).
 * Pure model: no DOM, no graph library instance, ES5/ES2015-safe, no modules.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  /* C1: Edge budget configuration and calculation */
  var EDGE_BUDGET = { overview: 40, phase: 150, perExtraPhase: 50, max: 400 };

  function edgeBudget(vm, state) {
    var cProjs = (state && state.collapsedProjects) || new Set();
    var cPhases = (state && state.collapsedPhases) || new Set();
    var n = 0;
    if (vm && Array.isArray(vm.projects)) {
      vm.projects.forEach(function (p) {
        if (!cProjs.has(p.name)) {
          (p.phases || []).forEach(function (f) {
            if (!cPhases.has(p.name + "|" + f.name)) n++;
          });
        }
      });
    }
    if (n === 0) return EDGE_BUDGET.overview;
    if (n === 1) return EDGE_BUDGET.phase;
    return Math.min(EDGE_BUDGET.max, EDGE_BUDGET.phase + EDGE_BUDGET.perExtraPhase * (n - 1));
  }

  /* C2: Default-collapsed overview state */
  function overviewState(vm, opts) {
    var collapsedProjects = new Set(), collapsedPhases = new Set();
    if (!vm || !Array.isArray(vm.projects) || vm.projects.length === 0) {
      return { collapsedProjects: collapsedProjects, collapsedPhases: collapsedPhases };
    }
    opts = opts || {};
    var knownProjects = new Set(), phaseToProj = {};
    vm.projects.forEach(function (p) {
      knownProjects.add(p.name);
      if (opts.level === "project") collapsedProjects.add(p.name);
      (p.phases || []).forEach(function (f) {
        var phId = p.name + "|" + f.name;
        phaseToProj[phId] = p.name;
        collapsedPhases.add(phId);
      });
    });
    if (Array.isArray(opts.expandProjects)) {
      opts.expandProjects.forEach(function (proj) {
        if (knownProjects.has(proj)) collapsedProjects.delete(proj);
      });
    }
    if (Array.isArray(opts.expandPhases)) {
      opts.expandPhases.forEach(function (phId) {
        if (phaseToProj[phId] !== undefined) {
          collapsedPhases.delete(phId);
          collapsedProjects.delete(phaseToProj[phId]);
        }
      });
    }
    return { collapsedProjects: collapsedProjects, collapsedPhases: collapsedPhases };
  }

  /* C3: Toggle cluster and expand all */
  function toggleCluster(state, kind, id) {
    var cProjs = new Set((state && state.collapsedProjects) || []);
    var cPhases = new Set((state && state.collapsedPhases) || []);
    if (kind === "project") {
      if (cProjs.has(id)) cProjs.delete(id); else cProjs.add(id);
    } else if (kind === "phase") {
      if (cPhases.has(id)) {
        cPhases.delete(id);
        var sep = String(id).indexOf("|");
        cProjs.delete(sep >= 0 ? String(id).slice(0, sep) : String(id));
      } else {
        cPhases.add(id);
      }
    }
    return { collapsedProjects: cProjs, collapsedPhases: cPhases };
  }

  function expandAllIn(state, vm, projectName) {
    var cProjs = new Set((state && state.collapsedProjects) || []);
    var cPhases = new Set((state && state.collapsedPhases) || []);
    cProjs.delete(projectName);
    if (vm && Array.isArray(vm.projects)) {
      vm.projects.forEach(function (p) {
        if (p.name === projectName) {
          (p.phases || []).forEach(function (f) { cPhases.delete(projectName + "|" + f.name); });
        }
      });
    }
    var prefix = projectName + "|";
    cPhases.forEach(function (phId) {
      if (phId.indexOf(prefix) === 0) cPhases.delete(phId);
    });
    return { collapsedProjects: cProjs, collapsedPhases: cPhases };
  }

  /* C4: Visible representative of a task key */
  function repOf(vm, state, key) {
    if (!vm || !vm.items || typeof vm.items.get !== "function" || !vm.items.has(key)) return null;
    var it = vm.items.get(key);
    if (!it) return null;
    var cProjs = (state && state.collapsedProjects) || new Set();
    var cPhases = (state && state.collapsedPhases) || new Set();
    if (cProjs.has(it.project)) return { id: "pc:" + it.project, level: "project" };
    var phId = it.project + "|" + (it.phase || "");
    if (cPhases.has(phId)) return { id: "phc:" + phId, level: "phase" };
    return { id: "t:" + key, level: "task" };
  }

  /* Deterministic priority comparator for meta-edges */
  function edgeCompare(a, b) {
    if (a.critical !== b.critical) return a.critical ? -1 : 1;
    if (a.crossProject !== b.crossProject) return a.crossProject ? -1 : 1;
    if (a.count !== b.count) return b.count - a.count;
    if (a.id < b.id) return -1;
    if (a.id > b.id) return 1;
    return 0;
  }

  function taskEdgeCompare(a, b) {
    var aT = (a.source.indexOf("t:") === 0 || a.target.indexOf("t:") === 0);
    var bT = (b.source.indexOf("t:") === 0 || b.target.indexOf("t:") === 0);
    if (aT !== bT) return aT ? -1 : 1;
    return edgeCompare(a, b);
  }

  function cloneEdge(e) {
    return {
      id: e.id, source: e.source, target: e.target, count: e.count,
      pairs: e.pairs.slice(), level: e.level, crossProject: e.crossProject,
      critical: e.critical, exact: e.exact
    };
  }

  function mergeInto(map, list, newId, newSource, newTarget, sEdge, exact) {
    var dest = map[newId];
    if (dest) {
      dest.count += sEdge.count;
      dest.pairs = dest.pairs.concat(sEdge.pairs);
      if (sEdge.crossProject) dest.crossProject = true;
      if (sEdge.critical) dest.critical = true;
      dest.exact = false;
      dest.level = "meta";
    } else {
      dest = {
        id: newId, source: newSource, target: newTarget,
        count: sEdge.count, pairs: sEdge.pairs.slice(),
        level: "meta", crossProject: !!sEdge.crossProject,
        critical: !!sEdge.critical, exact: !!exact
      };
      map[newId] = dest;
      list.push(dest);
    }
  }

  /* C5: Meta-edges aggregation with edge budget */
  function metaEdges(vm, state, opts) {
    var items = (vm && vm.items) || new Map(), optsObj = opts || {};
    var showAll = !!optsObj.showAllEdges, total = 0, intraHidden = 0;
    var rawEdgeMap = {}, internalPhaseMap = {};

    items.forEach(function (it) {
      if (!it.key) return;
      (it.deps || []).forEach(function (dk) {
        if (!items.has(dk)) return;
        var dep = items.get(dk);
        if (!showAll && dep.state === "done" && it.state === "done") return;
        total++;
        var fromRep = repOf(vm, state, dk), toRep = repOf(vm, state, it.key);
        if (!fromRep || !toRep) return;
        if (fromRep.id === toRep.id) { intraHidden++; return; }
        var edgeId = "m:" + fromRep.id + "->" + toRep.id;
        var cross = (dep.project !== it.project), crit = !!(dep.critical && it.critical);
        var pairObj = { from: dk, to: it.key };
        if (rawEdgeMap[edgeId]) {
          var e = rawEdgeMap[edgeId];
          e.count++; e.pairs.push(pairObj);
          if (cross) e.crossProject = true;
          if (crit) e.critical = true;
        } else {
          var isTaskPair = (fromRep.level === "task" && toRep.level === "task");
          if (isTaskPair && dep.project === it.project && dep.phase === it.phase) {
            internalPhaseMap[edgeId] = dep.project + "|" + (dep.phase || "");
          }
          rawEdgeMap[edgeId] = {
            id: edgeId, source: fromRep.id, target: toRep.id, count: 1,
            pairs: [pairObj], level: isTaskPair ? "task" : "meta",
            crossProject: cross, critical: crit, exact: true
          };
        }
      });
    });

    var limit = (typeof optsObj.limit === "number" && optsObj.limit > 0 &&
      Math.floor(optsObj.limit) === optsObj.limit && isFinite(optsObj.limit)) ?
      optsObj.limit : edgeBudget(vm, state);

    var phaseInternals = {}, remList = [];
    Object.keys(rawEdgeMap).forEach(function (k) {
      var e = rawEdgeMap[k], ph = internalPhaseMap[k];
      if (ph) {
        if (!phaseInternals[ph]) phaseInternals[ph] = [];
        phaseInternals[ph].push(e);
      } else {
        remList.push(e);
      }
    });

    var internalEdges = [];
    Object.keys(phaseInternals).forEach(function (phId) {
      var pEdges = phaseInternals[phId];
      pEdges.sort(edgeCompare);
      for (var i = 0; i < pEdges.length; i++) {
        if (i < EDGE_BUDGET.perExtraPhase) internalEdges.push(pEdges[i]);
        else intraHidden += pEdges[i].count;
      }
    });
    internalEdges.sort(edgeCompare);

    var hidden = 0;
    if (internalEdges.length > limit) {
      for (var h = limit; h < internalEdges.length; h++) hidden += internalEdges[h].count;
      internalEdges = internalEdges.slice(0, limit);
    }

    var remLimit = Math.max(0, limit - internalEdges.length);
    remList.sort(taskEdgeCompare);

    var currentEdges = [], currentIntraHidden = intraHidden;
    if (remList.length <= remLimit) {
      currentEdges = remList;
    } else {
      var keep = remLimit;
      while (true) {
        currentIntraHidden = intraHidden;
        var mergedMap = {}, mergedList = [];
        for (var i = 0; i < keep && i < remList.length; i++) {
          var kept = cloneEdge(remList[i]);
          mergedMap[kept.id] = kept;
          mergedList.push(kept);
        }
        for (var j = keep; j < remList.length; j++) {
          var sEdge = remList[j], sEnd = sEdge.source, tEnd = sEdge.target;
          if (sEnd.indexOf("t:") === 0) {
            var fIt = items.get(sEnd.slice(2));
            sEnd = "phc:" + fIt.project + "|" + fIt.phase;
          }
          if (tEnd.indexOf("t:") === 0) {
            var tIt = items.get(tEnd.slice(2));
            tEnd = "phc:" + tIt.project + "|" + tIt.phase;
          }
          if (sEnd === tEnd) {
            currentIntraHidden += sEdge.count;
          } else {
            mergeInto(mergedMap, mergedList, "m:" + sEnd + "->" + tEnd, sEnd, tEnd, sEdge, false);
          }
        }
        currentEdges = mergedList;
        if (currentEdges.length > remLimit && keep > 0) {
          keep = Math.max(0, keep - (currentEdges.length - remLimit));
        } else {
          break;
        }
      }

      if (currentEdges.length > remLimit) {
        var projMap = {}, projList = [];
        currentEdges.forEach(function (e) {
          var sEnd = e.source.indexOf("phc:") === 0 ? ("pc:" + e.source.slice(4).split("|")[0]) : e.source;
          var tEnd = e.target.indexOf("phc:") === 0 ? ("pc:" + e.target.slice(4).split("|")[0]) : e.target;
          if (sEnd === tEnd) {
            currentIntraHidden += e.count;
          } else {
            var isExact = (e.source === sEnd && e.target === tEnd) ? e.exact : false;
            mergeInto(projMap, projList, "m:" + sEnd + "->" + tEnd, sEnd, tEnd, e, isExact);
          }
        });
        currentEdges = projList;
      }

      currentEdges.sort(taskEdgeCompare);

      if (currentEdges.length > remLimit) {
        for (var h2 = remLimit; h2 < currentEdges.length; h2++) hidden += currentEdges[h2].count;
        currentEdges = currentEdges.slice(0, remLimit);
      }
    }

    var finalEdges = internalEdges.concat(currentEdges);
    var shown = 0, aggregated = 0;
    finalEdges.forEach(function (e) {
      shown += e.count;
      if (!e.exact) aggregated++;
    });

    return {
      edges: finalEdges, limit: limit, total: total, shown: shown,
      intraHidden: currentIntraHidden, aggregated: aggregated, hidden: hidden
    };
  }

  /* C6: Edge breakdown for hover/focus reveal */
  function edgeBreakdown(vm, edge, max) {
    var limit = (typeof max === "number" && max >= 0) ? max : 20;
    if (!edge || !Array.isArray(edge.pairs) || edge.pairs.length === 0) {
      return { rows: [], more: 0 };
    }
    var rows = [];
    edge.pairs.slice(0, limit).forEach(function (p) {
      var fromItem = (vm && vm.items) ? vm.items.get(p.from) : null;
      var toItem = (vm && vm.items) ? vm.items.get(p.to) : null;
      rows.push({
        from: p.from, to: p.to,
        fromTitle: fromItem ? (fromItem.title || "") : "",
        toTitle: toItem ? (toItem.title || "") : "",
        fromProject: fromItem ? (fromItem.project || "") : "",
        toProject: toItem ? (toItem.project || "") : ""
      });
    });
    return { rows: rows, more: edge.pairs.length - rows.length };
  }

  /* C7: Cluster summary */
  function clusterSummary(vm, state) {
    var cProjs = (state && state.collapsedProjects) || new Set();
    var cPhases = (state && state.collapsedPhases) || new Set();
    var projectsOut = [], phasesOut = [];
    var visCollapsedPhases = 0, visExpandedPhases = 0, visTasks = 0;
    var projs = (vm && Array.isArray(vm.projects)) ? vm.projects : [];

    projs.forEach(function (p) {
      var isPCollapsed = cProjs.has(p.name);
      var pTotal = 0, pDone = 0;
      (p.phases || []).forEach(function (f) {
        pTotal += (f.total || 0);
        pDone += (f.done || 0);
      });
      var pPct = pTotal > 0 ? Math.round((pDone / pTotal) * 100) : 0;
      projectsOut.push({
        id: isPCollapsed ? ("pc:" + p.name) : ("p:" + p.name),
        project: p.name, label: p.label, color: p.color,
        collapsed: isPCollapsed, total: pTotal, done: pDone, percent: pPct
      });

      if (!isPCollapsed) {
        (p.phases || []).forEach(function (f) {
          var phId = p.name + "|" + f.name;
          var isPhCollapsed = cPhases.has(phId);
          var fTotal = f.total || 0, fDone = f.done || 0;
          var fPct = fTotal > 0 ? Math.round((fDone / fTotal) * 100) : 0;
          phasesOut.push({
            id: isPhCollapsed ? ("phc:" + phId) : ("ph:" + phId),
            project: p.name, phase: f.name, collapsed: isPhCollapsed,
            total: fTotal, done: fDone, percent: fPct,
            ready: f.ready || 0, blocked: f.blocked || 0
          });
          if (isPhCollapsed) {
            visCollapsedPhases++;
          } else {
            visExpandedPhases++;
            visTasks += fTotal;
          }
        });
      }
    });

    return {
      projects: projectsOut, phases: phasesOut,
      visible: {
        projects: projs.length,
        collapsedPhases: visCollapsedPhases,
        expandedPhases: visExpandedPhases,
        tasks: visTasks
      }
    };
  }

  PlanUI.EDGE_BUDGET = EDGE_BUDGET;
  PlanUI.edgeBudget = edgeBudget;
  PlanUI.overviewState = overviewState;
  PlanUI.toggleCluster = toggleCluster;
  PlanUI.expandAllIn = expandAllIn;
  PlanUI.repOf = repOf;
  PlanUI.edgeCompare = edgeCompare;
  PlanUI.metaEdges = metaEdges;
  PlanUI.edgeBreakdown = edgeBreakdown;
  PlanUI.clusterSummary = clusterSummary;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
