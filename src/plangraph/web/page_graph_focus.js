/* PlanUI graph focus pure model — GU-G3a.
 * Decides focus core, 1-hop neighbours, context, and faded sets.
 * No DOM, no cy instance, ES5-safe, pure functions only.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  function setToSortedArray(s) {
    var arr = [];
    s.forEach(function (x) { arr.push(x); });
    return arr.sort();
  }

  function copyObject(obj) {
    if (!obj || typeof obj !== "object") return null;
    var copy = {};
    for (var k in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, k)) {
        copy[k] = obj[k];
      }
    }
    return copy;
  }

  function focusSets(graph, id, opts) {
    if (!graph || typeof graph !== "object" || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges) || !id) return null;
    var hops = (opts && typeof opts.hops === "number" && !isNaN(opts.hops) && opts.hops >= 1 && Math.floor(opts.hops) === opts.hops) ? opts.hops : 1;
    if (hops > 3) hops = 3;

    var nodeMap = {};
    for (var i = 0; i < graph.nodes.length; i++) {
      var n = graph.nodes[i];
      if (n && n.data && n.data.id) nodeMap[n.data.id] = n;
    }
    if (!nodeMap[id]) return null;

    var coreSet = new Set([id]);

    // Compound: add all descendants
    if (id.indexOf("p:") === 0 || id.indexOf("ph:") === 0) {
      var childrenMap = {};
      for (var j = 0; j < graph.nodes.length; j++) {
        var nd = graph.nodes[j];
        if (nd && nd.data && nd.data.parent && nd.data.id) {
          (childrenMap[nd.data.parent] = childrenMap[nd.data.parent] || []).push(nd.data.id);
        }
      }
      var q = [id];
      while (q.length > 0) {
        var curr = q.shift();
        var ch = childrenMap[curr] || [];
        for (var c = 0; c < ch.length; c++) {
          if (!coreSet.has(ch[c])) { coreSet.add(ch[c]); q.push(ch[c]); }
        }
      }
    }

    // Ghost stub: add dep task if present
    if (id.indexOf("g:") === 0) {
      var gNode = nodeMap[id];
      if (gNode && gNode.data && gNode.data.dep && nodeMap["t:" + gNode.data.dep]) {
        coreSet.add("t:" + gNode.data.dep);
      }
    }

    // Undirected adjacency list
    var adj = {};
    for (var eI = 0; eI < graph.edges.length; eI++) {
      var e = graph.edges[eI];
      if (!e || !e.data) continue;
      var src = e.data.source, tgt = e.data.target;
      if (!src || !tgt || !nodeMap[src] || !nodeMap[tgt]) continue;
      (adj[src] = adj[src] || []).push(tgt);
      (adj[tgt] = adj[tgt] || []).push(src);
    }

    // BFS distance from core
    var dist = {}, bfsQ = [];
    coreSet.forEach(function (cid) { dist[cid] = 0; bfsQ.push(cid); });
    while (bfsQ.length > 0) {
      var u = bfsQ.shift(), d = dist[u];
      if (d >= hops) continue;
      var nbrs = adj[u] || [];
      for (var k = 0; k < nbrs.length; k++) {
        var nxt = nbrs[k];
        if (dist[nxt] === undefined) { dist[nxt] = d + 1; bfsQ.push(nxt); }
      }
    }

    var nearSet = new Set();
    Object.keys(dist).forEach(function (nid) {
      if (dist[nid] > 0 && dist[nid] <= hops && !coreSet.has(nid) && nodeMap[nid]) {
        nearSet.add(nid);
      }
    });

    var contextSet = new Set();
    function addAncestors(nid) {
      var cur = nodeMap[nid];
      var visited = new Set();
      while (cur && cur.data && cur.data.parent) {
        var pid = cur.data.parent;
        if (visited.has(pid)) break;
        visited.add(pid);
        if (!nodeMap[pid]) break;
        if (!coreSet.has(pid) && !nearSet.has(pid)) contextSet.add(pid);
        cur = nodeMap[pid];
      }
    }
    coreSet.forEach(addAncestors);
    nearSet.forEach(addAncestors);

    var fadedSet = new Set();
    for (var nI = 0; nI < graph.nodes.length; nI++) {
      var node = graph.nodes[nI];
      if (!node || !node.data || !node.data.id) continue;
      var nid2 = node.data.id;
      if (!coreSet.has(nid2) && !nearSet.has(nid2) && !contextSet.has(nid2)) fadedSet.add(nid2);
    }

    var keepEdgeSet = new Set(), fadeEdgeSet = new Set();
    for (var edI = 0; edI < graph.edges.length; edI++) {
      var ed = graph.edges[edI];
      if (!ed || !ed.data || !ed.data.id) continue;
      var es = ed.data.source, et = ed.data.target;
      var keep = false;
      if (es && et && nodeMap[es] && nodeMap[et]) {
        var inS = coreSet.has(es) || nearSet.has(es);
        var inT = coreSet.has(et) || nearSet.has(et);
        if (inS && inT) {
          var ds = dist[es], dt = dist[et];
          if ((coreSet.has(es) || ds < hops) || (coreSet.has(et) || dt < hops)) {
            keep = true;
          }
        }
      }
      (keep ? keepEdgeSet : fadeEdgeSet).add(ed.data.id);
    }

    return {
      id: id,
      core: setToSortedArray(coreSet),
      near: setToSortedArray(nearSet),
      context: setToSortedArray(contextSet),
      faded: setToSortedArray(fadedSet),
      keepEdges: setToSortedArray(keepEdgeSet),
      fadeEdges: setToSortedArray(fadeEdgeSet)
    };
  }

  function focusClasses(sets) {
    if (!sets) return { nodes: {}, edges: {} };
    var nodes = {}, edges = {};
    (sets.core || []).forEach(function (id) { nodes[id] = "core"; });
    (sets.near || []).forEach(function (id) { nodes[id] = "near"; });
    (sets.context || []).forEach(function (id) { nodes[id] = "context"; });
    (sets.faded || []).forEach(function (id) { nodes[id] = "faded"; });
    (sets.keepEdges || []).forEach(function (id) { edges[id] = "keep"; });
    (sets.fadeEdges || []).forEach(function (id) { edges[id] = "fade"; });
    return { nodes: nodes, edges: edges };
  }

  function focusReduce(state, action) {
    state = state || { focusId: null, expand: null };
    action = action || {};
    var type = action.type;
    if (type !== "tap" && type !== "tapEmpty" && type !== "esc" && type !== "dbltap") {
      return { focusId: state.focusId, expand: null };
    }
    if (type === "tap") {
      return { focusId: action.id ? action.id : state.focusId, expand: null };
    }
    if (type === "tapEmpty" || type === "esc") {
      return { focusId: null, expand: null };
    }
    if (type === "dbltap") {
      var id = action.id;
      if (!id) return { focusId: state.focusId, expand: null };
      if (id.indexOf("phc:") === 0) {
        var part = id.slice(4);
        return { focusId: "ph:" + part, expand: { kind: "phase", id: part } };
      }
      if (id.indexOf("pc:") === 0) {
        var proj = id.slice(3);
        return { focusId: "p:" + proj, expand: { kind: "project", id: proj } };
      }
      return { focusId: id, expand: null };
    }
    return { focusId: state.focusId, expand: null };
  }

  function focusAfterRebuild(graph, focusId) {
    if (!graph || !graph.nodes || !focusId) return null;
    var nodeMap = {};
    for (var i = 0; i < graph.nodes.length; i++) {
      var n = graph.nodes[i];
      if (n && n.data && n.data.id) nodeMap[n.data.id] = n;
    }
    if (nodeMap[focusId]) return focusId;

    if (focusId.indexOf("phc:") === 0 && nodeMap["ph:" + focusId.slice(4)]) return "ph:" + focusId.slice(4);
    if (focusId.indexOf("ph:") === 0 && nodeMap["phc:" + focusId.slice(3)]) return "phc:" + focusId.slice(3);
    if (focusId.indexOf("pc:") === 0 && nodeMap["p:" + focusId.slice(3)]) return "p:" + focusId.slice(3);
    if (focusId.indexOf("p:") === 0 && nodeMap["pc:" + focusId.slice(2)]) return "pc:" + focusId.slice(2);

    if (focusId.indexOf("t:") === 0) {
      var taskKey = focusId.slice(2);
      var sep = taskKey.indexOf(":");
      var projName = sep > 0 ? taskKey.slice(0, sep) : "";
      for (var j = 0; j < graph.nodes.length; j++) {
        var c = graph.nodes[j];
        if (!c || !c.data) continue;
        var cid = c.data.id || "";
        if (cid.indexOf("phc:") === 0 && c.data.keys && c.data.keys.indexOf(taskKey) >= 0) return cid;
      }
      for (var k = 0; k < graph.nodes.length; k++) {
        var cp = graph.nodes[k];
        if (!cp || !cp.data) continue;
        var cpid = cp.data.id || "";
        if (cpid.indexOf("pc:") === 0 && ((cp.data.keys && cp.data.keys.indexOf(taskKey) >= 0) || cpid === "pc:" + projName)) {
          return cpid;
        }
      }
    }
    return null;
  }

  function focusLabel(vm, id) {
    if (!id || typeof id !== "string") return "";
    vm = vm || {};
    if (id.indexOf("t:") === 0) {
      var key = id.slice(2);
      var item = (vm.items && typeof vm.items.get === "function") ? vm.items.get(key) : null;
      if (!item) return "";
      var title = String(item.title || "");
      if (title.length > 60) title = title.slice(0, 60) + "…";
      return key + " - " + title;
    }
    if (id.indexOf("ph:") === 0 || id.indexOf("phc:") === 0) {
      var phRest = id.indexOf("ph:") === 0 ? id.slice(3) : id.slice(4);
      var parts = phRest.split("|");
      return parts[0] + " > " + (parts[1] || "");
    }
    if (id.indexOf("p:") === 0 || id.indexOf("pc:") === 0) {
      return id.indexOf("p:") === 0 ? id.slice(2) : id.slice(3);
    }
    return "";
  }

  PlanUI.focusSets = focusSets;
  PlanUI.focusClasses = focusClasses;
  PlanUI.focusReduce = focusReduce;
  PlanUI.focusAfterRebuild = focusAfterRebuild;
  PlanUI.focusLabel = focusLabel;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = PlanUI;
  }
})(typeof window !== "undefined" ? window : global);
