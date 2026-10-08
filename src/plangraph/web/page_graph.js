/* PlanUI graph pure core — UI-PLAN §4, §3, §7 (T3a).
 * Pure elements builder, initial collapsed calculation, match counting,
 * and jump/fit support. No DOM, no cytoscape instance, ES5-safe, no modules:
 * attaches to global namespace `PlanUI` and exports via `module.exports`.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  function capitalise(name) {
    var s = String(name == null ? "" : name);
    return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
  }

  /* §3: Initial collapsedPhases rule */
  function initialCollapsed(vm) {
    var set = new Set();
    if (!vm || !vm.projects) return set;
    vm.projects.forEach(function (p) {
      var seenUnfinished = false;
      (p.phases || []).forEach(function (f) {
        var its = (f.keys || []).map(function (k) { return vm.items ? vm.items.get(k) : null; }).filter(Boolean);
        var allDone = its.length > 0 && its.every(function (i) { return i.state === "done" || i.state === "cancelled"; });
        var active = its.some(function (i) { return i.display === "ready" || i.state === "claimed" || i.state === "in_review"; });
        if (allDone || (!active && seenUnfinished)) set.add(p.name + "|" + f.name);
        if (!allDone) seenUnfinished = true;
      });
    });
    return set;
  }

  /* §4: buildElements(vm, ui) -> { nodes: [], edges: [] } */
  function buildElements(vm, ui) {
    vm = vm || { items: new Map(), projects: [], decisionById: {} };
    ui = ui || {};

    var cPhases = new Set(ui.collapsedPhases || []);
    var cProjs = new Set(ui.collapsedProjects || []);
    var settings = ui.settings || {};
    var showAllEdges = !!settings.showAllEdges;
    var showCross = settings.showCross !== false;
    var matchMode = ui.matchMode || "dim";

    var query = ui.query && String(ui.query).trim();
    var flt = ui.filters || {};
    var hasFlt = !!((flt.projects && flt.projects.size > 0) || (flt.phases && flt.phases.size > 0) ||
      (flt.states && flt.states.size > 0) || (flt.owners && flt.owners.size > 0) ||
      flt.operatorOnly || flt.loopOnly || (flt.edgeMode && flt.edgeMode !== "all"));
    var filtering = !!(query || hasFlt);

    function matches(it) {
      if (!it) return false;
      if (query && PlanUI.Query && !PlanUI.Query.match(it, query)) return false;
      if (hasFlt) {
        if (flt.projects && flt.projects.size && !flt.projects.has(it.project)) return false;
        if (flt.phases && flt.phases.size && !flt.phases.has(it.phase)) return false;
        if (flt.states && flt.states.size && !flt.states.has(it.display) && !flt.states.has(it.state)) return false;
        if (flt.owners && flt.owners.size && !flt.owners.has(it.owner)) return false;
        if (flt.operatorOnly && !it.operator) return false;
        if (flt.loopOnly && !it.loop) return false;
      }
      return true;
    }

    if (ui.revealMatches && filtering) {
      vm.items.forEach(function (it) {
        if (matches(it)) {
          cPhases.delete(it.project + "|" + it.phase);
          cProjs.delete(it.project);
        }
      });
    }

    var nodes = [], nodeMap = {};
    function putNode(n) { nodes.push(n); nodeMap[n.data.id] = n; }

    (vm.projects || []).forEach(function (proj) {
      var isProjCol = cProjs.has(proj.name);
      var tot = 0, done = 0, allKeys = [];
      (proj.phases || []).forEach(function (f) {
        tot += f.total; done += f.done;
        allKeys = allKeys.concat(f.keys || []);
      });
      var pct = tot > 0 ? Math.round((done / tot) * 100) : 0;
      var pLabel = proj.label + " — " + tot + " task" + (tot === 1 ? "" : "s") + " · " + pct + " % done";
      var projMatch = !filtering || allKeys.some(function (k) { return matches(vm.items.get(k)); });
      if (matchMode === "hide" && filtering && !projMatch) return;

      if (isProjCol) {
        putNode({
          data: { id: "pc:" + proj.name, project: proj.name, label: pLabel, total: tot, done: done, percent: pct, keys: allKeys, width: 300, height: 72 },
          classes: "project-collapsed" + (proj.wait ? " wait-" + proj.wait : "") + (filtering && !projMatch ? " dim" : "")
        });
        return;
      }

      putNode({
        data: { id: "p:" + proj.name, project: proj.name, label: pLabel, color: proj.color, total: tot, done: done, percent: pct },
        classes: "project-compound" + (proj.wait ? " wait-" + proj.wait : "") + (filtering && !projMatch ? " dim" : "")
      });

      (proj.phases || []).forEach(function (ph) {
        var phId = proj.name + "|" + ph.name;
        var isPhCol = cPhases.has(phId);
        var pKeys = ph.keys || [];
        var phMatch = !filtering || pKeys.some(function (k) { return matches(vm.items.get(k)); });
        if (matchMode === "hide" && filtering && !phMatch) return;
        var phLabel = ph.name + " ▾ " + ph.total + " task" + (ph.total === 1 ? "" : "s") + " · " + ph.done + " done";

        if (isPhCol) {
          putNode({
            data: { id: "phc:" + phId, parent: "p:" + proj.name, project: proj.name, phase: ph.name, label: phLabel, total: ph.total, done: ph.done, keys: pKeys.slice(), width: 300, height: 72 },
            classes: "phase-collapsed" + (ph.wait ? " wait-" + ph.wait : "") + (filtering && !phMatch ? " dim" : "")
          });
          return;
        }

        putNode({
          data: { id: "ph:" + phId, parent: "p:" + proj.name, project: proj.name, phase: ph.name, label: phLabel, total: ph.total, done: ph.done },
          classes: "phase-compound" + (ph.wait ? " wait-" + ph.wait : "") + (filtering && !phMatch ? " dim" : "")
        });

        pKeys.forEach(function (k) {
          var it = vm.items.get(k);
          if (!it) return;
          var m = matches(it);
          if (matchMode === "hide" && filtering && !m) return;

          var prefix = (it.operator ? "★ " : "") + (it.loop ? ("↺ " + (it.loop.iteration != null && it.loop.max != null ? (it.loop.iteration + "/" + it.loop.max + " ") : "")) : "");
          var cls = ["task-node"];
          if (it.loop) cls.push("loop-node");
          if (it.kind === "gate") cls.push("gate-node");
          if (it.kind === "goal") cls.push("goal-node");
          if (it.operator) cls.push("operator-node");
          if (it.wait) cls.push("wait-" + it.wait);   // D-677
          if (it.critical) cls.push("critical-node");
          if (it.display) cls.push("state-" + it.display);
          if (filtering && !m) cls.push("dim");

          putNode({
            data: { id: "t:" + it.key, key: it.key, parent: "ph:" + phId, project: it.project, phase: it.phase, label: prefix + (it.title || ""), title: it.title, display: it.display, state: it.state, kind: it.kind, operator: !!it.operator, critical: !!it.critical, width: 240, height: 64, color: proj.color,
              waitTip: it.blockedOnOperator ? ("waits on operator (" + it.blockedOnOperator.kind + ") via " + it.blockedOnOperator.via) : null },
            classes: cls.join(" ")
          });

          if (it.loop) {
            var edRaw = it.loop.exit_decision;
            var ed = edRaw && typeof edRaw === "object" ? edRaw.id : edRaw;
            var decided = !!(ed && vm.decisionById && vm.decisionById[ed]);
            var xCls = "exit-node exit-diamond " + (decided ? "filled" : "hollow") + (filtering && !m ? " dim" : "");
            putNode({
              data: { id: "x:" + it.key, key: it.key, parent: "ph:" + phId, project: it.project, phase: it.phase, label: "exit: " + (ed || ""), exitDecision: ed, decided: decided, width: 100, height: 40, shape: "diamond" },
              classes: xCls
            });
          }
        });
      });
    });

    if (showCross) {
      vm.items.forEach(function (it) {
        if (cProjs.has(it.project) || cPhases.has(it.project + "|" + it.phase) || !nodeMap["t:" + it.key]) return;
        (it.deps || []).forEach(function (dKey) {
          var dep = vm.items.get(dKey);
          if (!dep || dep.project === it.project) return;
          var gid = "g:" + it.key + ">" + dKey;
          var gm = !filtering || matches(it) || matches(dep);
          if (matchMode === "hide" && filtering && !gm) return;
          putNode({
            data: { id: gid, consumer: it.key, dep: dKey, parent: "ph:" + it.project + "|" + it.phase, project: it.project, phase: it.phase, label: "↗ " + capitalise(dep.project) + " · " + dep.title, width: 200, height: 40 },
            classes: "ghost-stub ghost-node dashed" + (filtering && !gm ? " dim" : "")
          });
        });
      });
    }

    function srcRep(k) {
      var i = vm.items.get(k);
      if (!i) return null;
      if (cProjs.has(i.project)) return "pc:" + i.project;
      if (cPhases.has(i.project + "|" + i.phase)) return "phc:" + i.project + "|" + i.phase;
      return i.loop ? ("x:" + k) : ("t:" + k);
    }
    function dstRep(k) {
      var i = vm.items.get(k);
      if (!i) return null;
      if (cProjs.has(i.project)) return "pc:" + i.project;
      if (cPhases.has(i.project + "|" + i.phase)) return "phc:" + i.project + "|" + i.phase;
      return "t:" + k;
    }

    var rawEdges = [];
    vm.items.forEach(function (it) {
      if (it.loop && nodeMap["t:" + it.key] && nodeMap["x:" + it.key]) {
        rawEdges.push({ s: "t:" + it.key, t: "x:" + it.key, crit: !!it.critical, sk: it.key, tk: it.key });
      }
    });

    if (showCross) {
      vm.items.forEach(function (it) {
        (it.deps || []).forEach(function (dk) {
          var dep = vm.items.get(dk);
          if (!dep || dep.project === it.project) return;
          var gid = "g:" + it.key + ">" + dk, tr = dstRep(it.key);
          if (nodeMap[gid] && tr && nodeMap[tr]) {
            rawEdges.push({ s: gid, t: tr, crit: !!(dep.critical && it.critical), sk: dk, tk: it.key, cross: true });
          }
        });
      });
    }

    vm.items.forEach(function (it) {
      (it.deps || []).forEach(function (dk) {
        var dep = vm.items.get(dk);
        if (!dep || dep.project !== it.project) return;
        if (!showAllEdges && dep.state === "done" && it.state === "done") return;
        var sr = srcRep(dk), tr = dstRep(it.key);
        if (!sr || !tr || sr === tr || !nodeMap[sr] || !nodeMap[tr]) return;
        rawEdges.push({ s: sr, t: tr, crit: !!(dep.critical && it.critical), sk: dk, tk: it.key, cross: false });
      });
    });

    var groups = {};
    rawEdges.forEach(function (e) {
      var k = e.s + "->" + e.t;
      (groups[k] = groups[k] || []).push(e);
    });

    var edges = [];
    Object.keys(groups).forEach(function (k) {
      var grp = groups[k], first = grp[0], cnt = grp.length;
      var crit = grp.some(function (e) { return e.crit; });
      var w = Math.round((2 + Math.log2(cnt)) * 100) / 100;
      if (crit) w = Math.max(w, 4);

      var em = !filtering;
      if (filtering) {
        var sn = nodeMap[first.s], tn = nodeMap[first.t];
        em = !!((sn && (sn.classes || "").indexOf("dim") < 0) || (tn && (tn.classes || "").indexOf("dim") < 0));
        if (em && flt.edgeMode && flt.edgeMode !== "all") {
          if (flt.edgeMode === "critical" && !crit) em = false;
          else if (flt.edgeMode === "cross" && !first.cross) em = false;
          else if (flt.edgeMode === "blocking") {
            var si = vm.items.get(first.sk);
            if (!si || si.display === "done") em = false;
          }
        }
        if (em && query && PlanUI.Query && query.indexOf("edge:") >= 0) {
          em = grp.some(function (e) { return PlanUI.Query.edgeMatch({ source: e.sk, target: e.tk }, query, vm.items); });
        }
      }
      if (matchMode === "hide" && filtering && !em) return;

      var ecls = [];
      if (crit) ecls.push("critical-edge");
      if (filtering && !em) ecls.push("dim");

      edges.push({
        data: { id: "e:" + first.s + "->" + first.t, source: first.s, target: first.t, count: cnt, label: cnt > 1 ? "×" + cnt : "", width: w, critical: crit },
        classes: ecls.join(" ")
      });
    });

    return { nodes: nodes, edges: edges };
  }

  /* §7: matchCounts(elements) -> { nodes, edges } */
  function matchCounts(elements) {
    var nodes = 0, edges = 0;
    if (!elements) return { nodes: 0, edges: 0 };
    (elements.nodes || []).forEach(function (n) {
      var cls = n.classes || "";
      if (cls.indexOf("dim") >= 0 || cls.indexOf("compound") >= 0) return;
      if (n.data && n.data.id && (n.data.id.indexOf("t:") === 0 || n.data.id.indexOf("phc:") === 0)) nodes++;
    });
    (elements.edges || []).forEach(function (e) {
      if ((e.classes || "").indexOf("dim") < 0) edges++;
    });
    return { nodes: nodes, edges: edges };
  }

  /* Jump/fit support: minimal ui patch to make task visible */
  function revealPatch(vm, ui, key) {
    if (!vm || !vm.items || !vm.items.has(key)) return null;
    var it = vm.items.get(key), phId = it.project + "|" + it.phase, patch = {};
    if (ui && ui.collapsedPhases && ui.collapsedPhases.has(phId)) {
      var np = new Set();
      ui.collapsedPhases.forEach(function (p) { if (p !== phId) np.add(p); });
      patch.collapsedPhases = np;
    }
    if (ui && ui.collapsedProjects && ui.collapsedProjects.has(it.project)) {
      var nj = new Set();
      ui.collapsedProjects.forEach(function (p) { if (p !== it.project) nj.add(p); });
      patch.collapsedProjects = nj;
    }
    return patch;
  }

  /* Jump/fit support: find bounding box of task or its visible representative */
  function fitTarget(elements, layout, key) {
    if (!elements || !layout || !key) return null;
    function box(b) { return b ? { x: b.x, y: b.y, w: b.w != null ? b.w : b.width, h: b.h != null ? b.h : b.height } : null; }
    var targetId = "t:" + key;
    var hasTarget = (elements.nodes || []).some(function (n) { return n.data && n.data.id === targetId; });
    if (hasTarget && layout[targetId]) return box(layout[targetId]);
    var rep = (elements.nodes || []).find(function (n) {
      var d = n.data || {};
      return (d.id && (d.id.indexOf("phc:") === 0 || d.id.indexOf("pc:") === 0)) &&
        Array.isArray(d.keys) && d.keys.indexOf(key) >= 0;
    });
    return rep && layout[rep.data.id] ? box(layout[rep.data.id]) : null;
  }

  PlanUI.buildElements = buildElements;
  PlanUI.initialCollapsed = initialCollapsed;
  PlanUI.matchCounts = matchCounts;
  PlanUI.revealPatch = revealPatch;
  PlanUI.fitTarget = fitTarget;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
