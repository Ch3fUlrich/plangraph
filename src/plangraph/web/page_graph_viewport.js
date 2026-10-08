/* PlanUI Graph Viewport — UI-PLAN §4, §10, §13 (T3b-1fix).
 * Pure viewport calculation, initial viewport positioning,
 * and test inspection hook window.__planViewport.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  function clusterLabel(text) {
    if (typeof text !== "string") return text;
    var idx = text.indexOf(" · ");
    if (idx < 0) return text;
    return text.slice(0, idx) + "\n" + text.slice(idx + 3);
  }

  function wrapLabel(text, maxPx, measure) {
    if (typeof text !== "string" || !text) return text;
    var m = typeof measure === "function" ? measure : function (s) { return s.length * 10; };
    var limit = (typeof maxPx === "number" && maxPx > 0) ? maxPx : 272;
    var idx = text.indexOf(" · "), parts = idx >= 0 ? [text.slice(0, idx), text.slice(idx + 3)] : [text], allLines = [];
    for (var p = 0; p < parts.length; p++) {
      var part = parts[p];
      if (part.indexOf(" ") < 0) { allLines.push(part); continue; }
      var words = part.split(" "), curLine = words[0];
      for (var i = 1; i < words.length; i++) {
        var candidate = curLine + " " + words[i];
        if (m(candidate) <= limit) curLine = candidate;
        else { allLines.push(curLine); curLine = words[i]; }
      }
      allLines.push(curLine);
    }
    return allLines.join("\n");
  }

  var _measureCtx = null;
  function measurer(ele) {
    if (!_measureCtx && typeof document !== "undefined" && document.createElement) {
      var cvs = document.createElement("canvas");
      _measureCtx = cvs.getContext ? cvs.getContext("2d") : null;
    }
    if (!_measureCtx) return function (s) { return typeof s === "string" ? s.length * 10 : 0; };
    var weight = (ele && typeof ele.style === "function" && ele.style("font-weight")) || "normal";
    var size = (ele && typeof ele.style === "function" && parseFloat(ele.style("font-size"))) || 20;
    var family = (ele && typeof ele.style === "function" && ele.style("font-family")) || "system-ui";
    var fontStr = weight + " " + size + "px " + family;
    return function (s) { _measureCtx.font = fontStr; return _measureCtx.measureText(s).width; };
  }

  function isPhoneWin(w) {
    if (!w) return false;
    if (typeof w.matchMedia === "function") {
      var m = w.matchMedia("(max-width: 700px)");
      if (m && typeof m.matches === "boolean") return m.matches;
    }
    return typeof w.innerWidth === "number" ? w.innerWidth <= 700 : false;
  }

  function getActiveSheet(doc, win) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!d || typeof d.getElementById !== "function") return null;
    var w = win || (d && d.defaultView) || (typeof window !== "undefined" ? window : null);
    if (!isPhoneWin(w)) return null;
    var det = d.getElementById("details");
    if (!det || det.hidden) return null;
    var hasAttr = (typeof det.hasAttribute === "function") ? det.hasAttribute("hidden") : (typeof det.getAttribute === "function" && det.getAttribute("hidden") != null);
    if (hasAttr || typeof det.getBoundingClientRect !== "function") return null;
    var r = det.getBoundingClientRect(), h = (r && typeof r.height === "number") ? r.height : (r ? r.bottom - r.top : 0);
    return (r && h > 0) ? r : null;
  }

  function sheetInset(doc, win) {
    var d = doc || (typeof document !== "undefined" ? document : null), sr = getActiveSheet(d, win);
    if (!sr) return { top: 0, bottom: 0, left: 0, right: 0 };
    var cyEl = d.getElementById("cy");
    if (!cyEl || typeof cyEl.getBoundingClientRect !== "function") return { top: 0, bottom: 0, left: 0, right: 0 };
    var cr = cyEl.getBoundingClientRect();
    return { top: 0, bottom: Math.max(Math.max(0, cr.bottom - sr.top), 120), left: 0, right: 0 };
  }

  function focusProjectFor(ui, vm, key) {
    if (!ui || !vm || !vm.items || !key) return null;
    var cur = (typeof ui.get === "function") ? ui.get("focusProject") : ui.focusProject;
    if (!cur || cur === "all" || cur === "All") return null;
    var item = (typeof vm.items.get === "function") ? vm.items.get(key) : vm.items[key];
    if (!item || !item.project) return null;
    if (cur === item.project) return null;
    return item.project;
  }

  function viewportFor(box, size, phone) {
    var isPh = !!phone, zmin = isPh ? 0.8 : 0.75, zmax = isPh ? 0.8 : 0.9;
    var bx1 = (box && typeof box.x1 === "number") ? box.x1 : 0;
    var by1 = (box && typeof box.y1 === "number") ? box.y1 : 0;
    var bw = (box && typeof box.w === "number") ? box.w : ((box && typeof box.x2 === "number") ? box.x2 - bx1 : 0);
    var bh = (box && typeof box.h === "number") ? box.h : ((box && typeof box.y2 === "number") ? box.y2 - by1 : 0);
    var sw = (size && typeof size.w === "number") ? size.w : 0;
    var sh = (size && typeof size.h === "number") ? size.h : 0;

    var zoom;
    if (bw <= 0 || bh <= 0) {
      zoom = zmax;
    } else {
      var raw = Math.min((sw - 48) / bw, (sh - 48) / bh);
      zoom = !isFinite(raw) ? zmax : Math.max(zmin, Math.min(zmax, raw));
    }
    return { zoom: zoom, pan: { x: 24 - bx1 * zoom, y: 24 - by1 * zoom } };
  }

  function anchorBoxFor(boxes, expIdx, availW) {
    if (!boxes || !boxes.length) return null;
    var idx = (typeof expIdx === "number" && expIdx >= 0 && expIdx < boxes.length) ? expIdx : 0;
    var targetX1 = boxes[idx].x1;
    var maxDiff = (typeof availW === "number") ? (availW - 60) : 0;
    var s = idx;
    for (var i = 0; i <= idx; i++) {
      if (targetX1 - boxes[i].x1 <= maxDiff) {
        s = i;
        break;
      }
    }
    var minX1 = boxes[s].x1;
    var minY1 = boxes[s].y1;
    var maxX2 = (boxes[s].x2 != null) ? boxes[s].x2 : (boxes[s].x1 + boxes[s].w);
    var maxY2 = (boxes[s].y2 != null) ? boxes[s].y2 : (boxes[s].y1 + boxes[s].h);
    for (var k = s + 1; k <= idx; k++) {
      var b = boxes[k];
      if (b.y1 < minY1) minY1 = b.y1;
      var bx2 = (b.x2 != null) ? b.x2 : (b.x1 + b.w);
      var by2 = (b.y2 != null) ? b.y2 : (b.y1 + b.h);
      if (bx2 > maxX2) maxX2 = bx2;
      if (by2 > maxY2) maxY2 = by2;
    }
    return {
      x1: minX1,
      y1: minY1,
      w: maxX2 - minX1,
      h: maxY2 - minY1,
      x2: maxX2,
      y2: maxY2
    };
  }

  function cleanInset(v) { return (typeof v === "number" && isFinite(v) && v >= 0) ? v : 0; }

  function panZoomFor(box, size, z, inset) {
    var bx = (box && typeof box.x === "number") ? box.x : 0, by = (box && typeof box.y === "number") ? box.y : 0;
    var bw = (box && typeof box.w === "number") ? box.w : ((box && typeof box.width === "number") ? box.width : 0);
    var bh = (box && typeof box.h === "number") ? box.h : ((box && typeof box.height === "number") ? box.height : 0);
    var sw = (size && typeof size.w === "number") ? size.w : ((size && typeof size.width === "number") ? size.width : 0);
    var sh = (size && typeof size.h === "number") ? size.h : ((size && typeof size.height === "number") ? size.height : 0);
    var zoom = (typeof z === "number") ? z : 1;
    var iTop = inset ? cleanInset(inset.top) : 0, iBottom = inset ? cleanInset(inset.bottom) : 0;
    var iLeft = inset ? cleanInset(inset.left) : 0, iRight = inset ? cleanInset(inset.right) : 0;
    var cx = bx + bw / 2, cy = by + bh / 2;
    return {
      zoom: zoom,
      pan: {
        x: (sw - iLeft - iRight) / 2 + iLeft - cx * zoom,
        y: (sh - iTop - iBottom) / 2 + iTop - cy * zoom
      }
    };
  }

  function updateSemanticZoom(c, phone) {
    if (!c || typeof c.zoom !== "function") return;
    var z = c.zoom();
    /* Keep headers readable when zoomed out, but never larger than the box that
     * holds them: compound headers sit in a 44-unit strip; collapsed cards are a
     * fixed 300x72, so they keep their stylesheet size (text must stay inside). */
    var clusterFont = Math.min(30, Math.max(20, Math.round(20 / z)));
    var doBatch = (typeof c.batch === "function") ? c.batch.bind(c) : function (fn) { fn(); };
    doBatch(function () {
      if (typeof c.nodes === "function") {
        c.nodes(".project-compound, .phase-compound").style("font-size", clusterFont);
        if (typeof c.style === "function") {
          var st = c.style();
          if (st && typeof st.update === "function") st.update();
        }
        var tasks = c.nodes(".task-node, .exit-node, .ghost-node");
        var edges = (typeof c.edges === "function") ? c.edges() : null;
        if (phone && z < 0.7) {
          tasks.style("display", "none"); if (edges) edges.style("display", "none");
        } else {
          tasks.style("display", "element"); if (edges) edges.style("display", "element");
          tasks.style("text-opacity", z < 0.5 ? 0 : 1);
        }
      }
    });
  }

  function installHook(cyInstance) {
    if (typeof window === "undefined") return;
    window.__planViewport = function () {
      var c = cyInstance || (PlanUI.GraphView && PlanUI.GraphView.getCy && PlanUI.GraphView.getCy());
      if (!c) return null;
      var z = c.zoom(), pan = c.pan(), ext = c.extent();
      var ex1 = Math.min(ext.x1, ext.x2), ex2 = Math.max(ext.x1, ext.x2);
      var ey1 = Math.min(ext.y1, ext.y2), ey2 = Math.max(ext.y1, ext.y2);
      var visibleTasks = 0, selectedVisible = false;

      if (typeof c.nodes === "function") {
        c.nodes(".task-node").forEach(function (n) {
          var nb = n.boundingBox(), nx1 = Math.min(nb.x1, nb.x2), nx2 = Math.max(nb.x1, nb.x2);
          var ny1 = Math.min(nb.y1, nb.y2), ny2 = Math.max(nb.y1, nb.y2);
          if (nx1 <= ex2 && nx2 >= ex1 && ny1 <= ey2 && ny2 >= ey1) visibleTasks++;
        });
        c.nodes(":selected").forEach(function (n) {
          var nb = n.boundingBox(), nx1 = Math.min(nb.x1, nb.x2), nx2 = Math.max(nb.x1, nb.x2);
          var ny1 = Math.min(nb.y1, nb.y2), ny2 = Math.max(nb.y1, nb.y2);
          if (nx1 <= ex2 && nx2 >= ex1 && ny1 <= ey2 && ny2 >= ey1) selectedVisible = true;
        });
      }

      var wrapSet = new Set();
      if (typeof c.nodes === "function") {
        c.nodes(":parent, .project-compound, .phase-compound, .project-collapsed, .phase-collapsed").forEach(function (n) {
          var w = n.style("text-wrap");
          if (w) wrapSet.add(w);
        });
      }
      var firstTask = (typeof c.nodes === "function") ? c.nodes(".task-node").first() : null;
      var taskNodePx = (firstTask && firstTask.length)
        ? (typeof firstTask.renderedHeight === "function" ? firstTask.renderedHeight() : (firstTask.renderedBoundingBox ? firstTask.renderedBoundingBox().h : 64 * z))
        : 64 * z;

      var colPhase = (typeof c.nodes === "function") ? c.nodes(".phase-collapsed").first() : null;
      var expPhase = (typeof c.nodes === "function") ? c.nodes(".phase-compound").first() : null;
      var colFs = (colPhase && colPhase.length && typeof colPhase.style === "function") ? parseFloat(colPhase.style("font-size")) : null;
      var expFs = (expPhase && expPhase.length && typeof expPhase.style === "function") ? parseFloat(expPhase.style("font-size")) : null;
      var collapsedLabelPx = (colFs != null && !isNaN(colFs)) ? colFs * z : null;
      var headerLabelPx = (expFs != null && !isNaN(expFs)) ? expFs * z : null;
      var collapsedLabelWeight = (colPhase && colPhase.length && typeof colPhase.style === "function") ? (colPhase.style("font-weight") || null) : null;
      var headerLabelWeight = (expPhase && expPhase.length && typeof expPhase.style === "function") ? (expPhase.style("font-weight") || null) : null;

      var doc = (typeof document !== "undefined") ? document : null;
      var cyEl = doc && doc.getElementById ? doc.getElementById("cy") : null;
      var cRect = (cyEl && typeof cyEl.getBoundingClientRect === "function") ? cyEl.getBoundingClientRect() : { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
      var sr = getActiveSheet(doc, typeof window !== "undefined" ? window : null);
      var sheetTop = sr ? sr.top : null;

      var selNode = (typeof c.nodes === "function") ? c.nodes(":selected").first() : null, selectedRenderedBox = null;
      if (selNode && selNode.length && typeof selNode.renderedBoundingBox === "function") {
        var rbb = selNode.renderedBoundingBox();
        var rx1 = cRect.left + (typeof rbb.x1 === "number" ? rbb.x1 : 0), ry1 = cRect.top + (typeof rbb.y1 === "number" ? rbb.y1 : 0);
        var rx2 = cRect.left + (typeof rbb.x2 === "number" ? rbb.x2 : (rbb.x1 + (rbb.w || 0))), ry2 = cRect.top + (typeof rbb.y2 === "number" ? rbb.y2 : (rbb.y1 + (rbb.h || 0)));
        selectedRenderedBox = { x1: rx1, y1: ry1, x2: rx2, y2: ry2 };
      }
      var selectedAboveSheet = (!selectedRenderedBox || sheetTop == null) ? true : (selectedRenderedBox.y2 <= sheetTop);
      var selectedInsideCanvasX = selectedRenderedBox ? (selectedRenderedBox.x1 >= cRect.left - 1e-3 && selectedRenderedBox.x2 <= cRect.right + 1e-3) : false;

      var colNode = (colPhase && colPhase.length) ? colPhase : ((typeof c.nodes === "function") ? c.nodes(".phase-collapsed, .project-collapsed").first() : null);
      var rawLabel = (colNode && colNode.length && typeof colNode.data === "function") ? (colNode.data("label") || "") : "", styleLabel = "";
      if (colNode && colNode.length && typeof colNode.style === "function") {
        var sVal = colNode.style("label");
        styleLabel = (typeof sVal === "function") ? sVal(colNode) : (typeof sVal === "string" ? sVal : "");
      }
      if (!styleLabel && rawLabel) styleLabel = wrapLabel(rawLabel, 272, measurer(colNode));
      var collapsedLabelLines = (typeof styleLabel === "string" && styleLabel) ? styleLabel.split("\n") : [];
      var normOrig = rawLabel.replace(/\s+/g, " ").trim(), normLines = collapsedLabelLines.join(" ").replace(/\s+/g, " ").trim();
      var origWithoutDot = normOrig.replace(" · ", " ").replace(/\s+/g, " ").trim(), collapsedLabelMidWord = true;
      if (collapsedLabelLines.length === 0 || normLines === normOrig || normLines === origWithoutDot) {
        collapsedLabelMidWord = false;
      } else {
        var lw = normLines.split(" ").filter(Boolean), ow = origWithoutDot.split(" ").filter(Boolean);
        if (lw.length === ow.length && lw.every(function (w, i) { return w === ow[i]; })) collapsedLabelMidWord = false;
      }
      var colTmw = (colNode && colNode.length && typeof colNode.style === "function") ? parseFloat(colNode.style("text-max-width")) : null;
      var collapsedTextMaxWidth = (colTmw != null && !isNaN(colTmw)) ? colTmw : null;
      var colMeasurer = measurer(colNode), collapsedLineMaxPx = 0;
      for (var li = 0; li < collapsedLabelLines.length; li++) {
        var lwPx = colMeasurer(collapsedLabelLines[li]);
        if (lwPx > collapsedLineMaxPx) collapsedLineMaxPx = lwPx;
      }

      return {
        zoom: z, pan: pan,
        extent: { x1: ext.x1, y1: ext.y1, x2: ext.x2, y2: ext.y2 },
        labelPx: 15 * z, visibleTasks: visibleTasks, selectedVisible: selectedVisible,
        clusterTextWrap: Array.from(wrapSet), taskNodePx: taskNodePx,
        collapsedLabelPx: collapsedLabelPx, headerLabelPx: headerLabelPx,
        collapsedLabelWeight: collapsedLabelWeight, headerLabelWeight: headerLabelWeight,
        collapsedLabelLines: collapsedLabelLines, collapsedLabelMidWord: collapsedLabelMidWord,
        collapsedTextMaxWidth: collapsedTextMaxWidth, collapsedLineMaxPx: collapsedLineMaxPx,
        selectedRenderedBox: selectedRenderedBox, sheetTop: sheetTop,
        selectedAboveSheet: selectedAboveSheet, selectedInsideCanvasX: selectedInsideCanvasX
      };
    };
  }

  function initial(cy, vm, ui, phone, onApply) {
    if (cy && typeof cy.resize === "function") cy.resize();
    var isPh = (phone !== undefined) ? !!phone : (typeof window !== "undefined" && window.innerWidth <= 700);
    var p0 = (vm && vm.projects && vm.projects.length) ? vm.projects[0] : null;
    var pName = p0 ? p0.name : null, box = null;
    var bbOpts = { includeLabels: true, includeOverlays: false };

    if (cy && PlanUI.isOverviewState && PlanUI.isOverviewState(ui, vm)) {   // fresh load: fit all hulls
      var over = PlanUI.fitOverview(cy, isPh, onApply, bbOpts);
      if (over) return over;
    }
    if (isPh) {
      if (ui && pName) {
        var curFocus = (typeof ui.get === "function") ? ui.get("focusProject") : ui.focusProject;
        if (!curFocus) {
          if (typeof ui.patch === "function") ui.patch({ focusProject: pName });
          else if (typeof ui.set === "function") ui.set("focusProject", pName);
          else ui.focusProject = pName;
        }
      }
      var targetProj = (ui && typeof ui.get === "function") ? ui.get("focusProject") : (ui ? ui.focusProject : null);
      if (!targetProj || targetProj === "all" || targetProj === "All") targetProj = pName;
      var proj = (vm && vm.projects || []).find(function (p) { return p.name === targetProj; }) || p0;

      var phaseItems = [];
      if (proj && proj.phases && cy) {
        (proj.phases || []).forEach(function (ph) {
          var idExp = "ph:" + proj.name + "|" + ph.name, idCol = "phc:" + proj.name + "|" + ph.name;
          var n = cy.getElementById(idExp), chosenId = idExp;
          if (!n || !n.length) { n = cy.getElementById(idCol); chosenId = idCol; }
          if (n && n.length && typeof n.boundingBox === "function") {
            var b = n.boundingBox(bbOpts);
            if (b && (b.w > 0 || (b.x2 !== undefined && b.x2 > b.x1))) {
              var actualId = (typeof n.id === "function" ? n.id() : chosenId) || chosenId;
              phaseItems.push({ id: actualId, box: b });
            }
          }
        });
      }
      if (!phaseItems.length && cy && typeof cy.nodes === "function") {
        cy.nodes(".phase-compound, .phase-collapsed").forEach(function (n) {
          var dProj = (typeof n.data === "function") ? n.data("project") : null;
          if (!proj || dProj === proj.name) {
            var b = n.boundingBox(bbOpts);
            if (b && (b.w > 0 || (b.x2 !== undefined && b.x2 > b.x1))) {
              var nid = (typeof n.id === "function") ? n.id() : "";
              phaseItems.push({ id: nid, box: b });
            }
          }
        });
      }

      if (phaseItems.length) {
        phaseItems.sort(function (a, b) { return a.box.x1 - b.box.x1; });
        var expIdx = -1;
        for (var pi = 0; pi < phaseItems.length; pi++) {
          if (phaseItems[pi].id && phaseItems[pi].id.indexOf("ph:") === 0) {
            expIdx = pi;
            break;
          }
        }
        if (expIdx < 0) expIdx = 0;
        var boxes = phaseItems.map(function (it) { return it.box; });
        var cyW = (cy && typeof cy.width === "function") ? cy.width() : 0;
        var availW = cyW / 0.8;
        box = anchorBoxFor(boxes, expIdx, availW);
      }

      if ((!box || !box.w) && pName && cy) {
        var pNodeFallback = cy.getElementById("p:" + pName);
        if (pNodeFallback && pNodeFallback.length) box = pNodeFallback.boundingBox(bbOpts);
      }
    } else {
      if (pName && cy) {
        var pNode = cy.getElementById("p:" + pName);
        if (pNode && pNode.length) box = pNode.boundingBox(bbOpts);
      }
    }

    if ((!box || !box.w) && cy && typeof cy.elements === "function") {
      var allEles = cy.elements();
      if (allEles && allEles.length) box = allEles.boundingBox(bbOpts);
    }

    var size = {
      w: (cy && typeof cy.width === "function") ? cy.width() : 0,
      h: (cy && typeof cy.height === "function") ? cy.height() : 0
    };
    var res = viewportFor(box, size, isPh);
    if (typeof onApply === "function") onApply(true);
    if (cy && typeof cy.zoom === "function") cy.zoom(res.zoom);
    if (cy && typeof cy.pan === "function") cy.pan(res.pan);
    if (typeof onApply === "function") onApply(false);
    installHook(cy);
    res.box = box;
    return res;
  }

  PlanUI.viewportFor = viewportFor; PlanUI.anchorBoxFor = anchorBoxFor; PlanUI.panZoomFor = panZoomFor;
  PlanUI.updateSemanticZoom = updateSemanticZoom; PlanUI.clusterLabel = clusterLabel;
  PlanUI.wrapLabel = wrapLabel; PlanUI.measurer = measurer; PlanUI.sheetInset = sheetInset;
  PlanUI.focusProjectFor = focusProjectFor;
  PlanUI.GraphViewport = {
    initial: initial, installHook: installHook, anchorBoxFor: anchorBoxFor,
    panZoomFor: panZoomFor, viewportFor: viewportFor, sheetInset: sheetInset,
    wrapLabel: wrapLabel, measurer: measurer
  };

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
