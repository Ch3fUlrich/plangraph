/* PlanUI ELK layout builder and service — UI-PLAN §4, §12 (T3a).
 * Builds ELK layered hierarchy per project, accumulates relative coords to absolute,
 * stacks projects with an 80px gap, caches by signature, and handles async layout.
 * No DOM access except window.__planTimings guarded by typeof. ES5-safe, no modules.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var ROOT_LAYOUT_OPTIONS = {
    'elk.algorithm': 'layered',
    'elk.direction': 'RIGHT',
    'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
    'elk.spacing.nodeNode': '28',
    'elk.layered.spacing.nodeNodeBetweenLayers': '72',
    'elk.layered.spacing.edgeNodeBetweenLayers': '20',
    'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
    'elk.layered.nodePlacement.strategy': 'NETWORK_SIMPLEX',
    'elk.padding': '[top=12,left=12,bottom=12,right=12]'
  };

  var PHASE_LAYOUT_OPTIONS = {
    'elk.padding': '[top=44,left=16,bottom=16,right=16]'
  };

  /* §4: buildElkGraph(elements, project, sizes) -> ELK graph JSON */
  function buildElkGraph(elements, project, sizes) {
    sizes = sizes || {};
    var rootId = "p:" + project;

    var pcNode = (elements.nodes || []).find(function (n) {
      return n.data && n.data.id === "pc:" + project;
    });
    if (pcNode) {
      return {
        id: pcNode.data.id,
        width: pcNode.data.width || 300,
        height: pcNode.data.height || 72,
        children: [],
        edges: []
      };
    }

    var phaseNodes = [];
    var phaseChildren = {};
    var projectNodeIds = {};
    projectNodeIds[rootId] = true;

    (elements.nodes || []).forEach(function (n) {
      var d = n.data || {};
      if (d.parent === rootId) {
        phaseNodes.push(n);
        projectNodeIds[d.id] = true;
      }
    });

    (elements.nodes || []).forEach(function (n) {
      var d = n.data || {};
      if (d.parent && projectNodeIds[d.parent]) {
        (phaseChildren[d.parent] = phaseChildren[d.parent] || []).push(n);
        projectNodeIds[d.id] = true;
      }
    });

    function getNodeSize(n) {
      var d = n.data || {};
      var id = d.id || n.id;
      var s = sizes[id] || (d.key && sizes[d.key]);
      var w = (s && s.width != null) ? s.width : (d.width || 240);
      var h = (s && (s.height != null ? s.height : s)) ? (s.height != null ? s.height : s) : (d.height || 64);
      return { width: Number(w) || 240, height: Number(h) || 64 };
    }

    var elkChildren = phaseNodes.map(function (pn) {
      var pd = pn.data || {};
      var pId = pd.id;
      var isCompound = (pn.classes || "").indexOf("phase-compound") >= 0;

      if (isCompound) {
        var children = (phaseChildren[pId] || []).map(function (cn) {
          var cd = cn.data || {};
          var sz = getNodeSize(cn);
          return { id: cd.id, width: sz.width, height: sz.height };
        });
        return {
          id: pId,
          layoutOptions: PHASE_LAYOUT_OPTIONS,
          children: children
        };
      } else {
        var sz = getNodeSize(pn);
        return { id: pId, width: sz.width, height: sz.height };
      }
    });

    var elkEdges = [];
    (elements.edges || []).forEach(function (e) {
      var ed = e.data || {};
      if (projectNodeIds[ed.source] && projectNodeIds[ed.target]) {
        elkEdges.push({
          id: ed.id,
          sources: [ed.source],
          targets: [ed.target],
          source: ed.source,
          target: ed.target
        });
      }
    });

    return {
      id: rootId,
      layoutOptions: ROOT_LAYOUT_OPTIONS,
      children: elkChildren,
      edges: elkEdges
    };
  }

  /* §4: accumulate relative coords to absolute */
  function accumulate(elkResult) {
    var out = {};
    function walk(node, px, py) {
      if (!node) return;
      var x = px + (node.x || 0);
      var y = py + (node.y || 0);
      var w = node.width || 0;
      var h = node.height || 0;
      out[node.id] = { x: x, y: y, width: w, height: h, w: w, h: h };
      if (Array.isArray(node.children)) {
        node.children.forEach(function (c) { walk(c, x, y); });
      }
    }
    if (elkResult) walk(elkResult, 0, 0);
    return out;
  }

  /* §4: stackProjects(layouts, gap=80) */
  function stackProjects(layouts, gap) {
    if (gap === undefined) gap = 80;
    var out = {};
    var list = Array.isArray(layouts) ? layouts : Object.keys(layouts || {}).map(function (k) { return layouts[k]; });
    var curY = 0;

    list.forEach(function (layout) {
      if (!layout) return;
      var keys = Object.keys(layout);
      if (!keys.length) return;
      var minY = Infinity, maxY = -Infinity;
      keys.forEach(function (k) {
        var n = layout[k];
        var y = n.y || 0, h = n.height != null ? n.height : (n.h || 0);
        if (y < minY) minY = y;
        if (y + h > maxY) maxY = y + h;
      });
      var projH = (isFinite(maxY) && isFinite(minY)) ? (maxY - minY) : 0;
      var yShift = curY - (isFinite(minY) ? minY : 0);

      keys.forEach(function (k) {
        var n = layout[k];
        var w = n.width != null ? n.width : (n.w || 0);
        var h = n.height != null ? n.height : (n.h || 0);
        out[k] = { x: n.x || 0, y: (n.y || 0) + yShift, width: w, height: h, w: w, h: h };
      });

      curY += projH + gap;
    });

    return out;
  }

  /* §4: layoutSignature(project, elements, heights) */
  function layoutSignature(project, elements, heights) {
    var ids = [];
    if (elements && elements.nodes) {
      elements.nodes.forEach(function (n) {
        var id = n.data ? n.data.id : n.id;
        if (!id) return;
        if ((n.data && n.data.project === project) ||
            id.indexOf("p:" + project) === 0 || id.indexOf("pc:" + project) === 0 ||
            id.indexOf("ph:" + project + "|") === 0 || id.indexOf("phc:" + project + "|") === 0) {
          ids.push(id);
        }
      });
    }
    ids.sort();
    var hEntries = [];
    if (heights) {
      Object.keys(heights).sort().forEach(function (k) {
        hEntries.push(k + "=" + heights[k]);
      });
    }
    return project + ";" + ids.join(",") + ";" + hEntries.join(",");
  }

  /* §4, §12: LayoutService */
  var LayoutService = {
    _engine: null,
    _cache: {},
    _epoch: 0,

    setEngine: function (engine) {
      this._engine = engine;
    },

    getEpoch: function () {
      return this._epoch;
    },

    bumpEpoch: function () {
      return ++this._epoch;
    },

    clearCache: function () {
      this._cache = {};
    },

    layoutProject: function (proj, elements, heights) {
      var self = this;
      var sig = layoutSignature(proj, elements, heights);
      if (self._cache[sig]) {
        return Promise.resolve(self._cache[sig]);
      }

      var engine = self._engine;
      if (!engine) {
        if (typeof ELK !== "undefined") {
          engine = new ELK();
        } else {
          return Promise.reject(new Error("ELK layout engine is not available"));
        }
      }

      var elkGraph = buildElkGraph(elements, proj, heights);
      var runEpoch = self._epoch;
      var t0 = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();

      return Promise.resolve(engine.layout(elkGraph)).then(function (result) {
        var t1 = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
        var elapsed = t1 - t0;
        if (typeof window !== "undefined") {
          window.__planTimings = window.__planTimings || {};
          window.__planTimings.layoutMs = window.__planTimings.layoutMs || {};
          window.__planTimings.layoutMs[proj] = elapsed;
        }
        if (runEpoch !== self._epoch) {
          return null;
        }
        var absLayout = accumulate(result);
        self._cache[sig] = absLayout;
        return absLayout;
      });
    }
  };

  PlanUI.buildElkGraph = buildElkGraph;
  PlanUI.accumulate = accumulate;
  PlanUI.stackProjects = stackProjects;
  PlanUI.layoutSignature = layoutSignature;
  PlanUI.LayoutService = LayoutService;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
