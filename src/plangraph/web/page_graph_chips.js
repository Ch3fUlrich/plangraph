/* PlanUI Graph Chips & Hull Pure Model — UI-PLAN GU-G2a.
 * Stable project colours, chip view model, and convex hull geometry.
 * No DOM, ES5-safe, no modules: attaches to PlanUI & exports.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var PROJECT_PALETTE = [
    "#2f6f4f", "#3b5bdb", "#b45309", "#7c3aed",
    "#0e7490", "#be123c", "#4d7c0f", "#c026d3"
  ];
  PlanUI.PROJECT_PALETTE = PROJECT_PALETTE;

  function fnv1a(str) {
    var hash = 2166136261;
    for (var i = 0; i < str.length; i++) {
      hash ^= str.charCodeAt(i);
      hash = Math.imul(hash, 16777619) >>> 0;
    }
    return hash >>> 0;
  }

  function resolveAllocations(names) {
    var out = Object.create(null), used = new Array(PROJECT_PALETTE.length);
    for (var u = 0; u < PROJECT_PALETTE.length; u++) used[u] = false;
    if (!names || !Array.isArray(names)) return { map: {}, used: used };
    var unique = [], seen = Object.create(null);
    for (var i = 0; i < names.length; i++) {
      var n = names[i];
      if (typeof n !== "string" || n.length === 0) continue;
      if (!seen[n]) { seen[n] = true; unique.push(n); }
    }
    unique.sort(function (a, b) { return a < b ? -1 : (a > b ? 1 : 0); });
    for (var j = 0; j < unique.length; j++) {
      var nm = unique[j], base = fnv1a(nm) % PROJECT_PALETTE.length, idx = base, probed = 0;
      while (used[idx] && probed < PROJECT_PALETTE.length) {
        idx = (idx + 1) % PROJECT_PALETTE.length;
        probed++;
      }
      if (probed >= PROJECT_PALETTE.length) idx = base;
      else used[idx] = true;
      out[nm] = PROJECT_PALETTE[idx];
    }
    var plain = {};
    for (var k in out) {
      Object.defineProperty(plain, k, {
        value: out[k],
        enumerable: true,
        writable: true,
        configurable: true
      });
    }
    return { map: plain, used: used };
  }

  function projectColors(names) {
    if (!names || !Array.isArray(names)) return {};
    return resolveAllocations(names).map;
  }
  PlanUI.projectColors = projectColors;

  function projectColor(name, names) {
    if (typeof name !== "string" || name.length === 0) return PROJECT_PALETTE[0];
    if (!names || !Array.isArray(names)) return PROJECT_PALETTE[fnv1a(name) % PROJECT_PALETTE.length];
    var resolved = resolveAllocations(names);
    if (Object.prototype.hasOwnProperty.call(resolved.map, name)) return resolved.map[name];
    var base = fnv1a(name) % PROJECT_PALETTE.length, idx = base, probed = 0;
    while (resolved.used[idx] && probed < PROJECT_PALETTE.length) {
      idx = (idx + 1) % PROJECT_PALETTE.length;
      probed++;
    }
    return PROJECT_PALETTE[probed >= PROJECT_PALETTE.length ? base : idx];
  }
  PlanUI.projectColor = projectColor;

  function colorTint(hex, alpha) {
    var a = (typeof alpha === "number" && isFinite(alpha)) ? Math.max(0, Math.min(1, alpha)) : 0.12;
    var m = typeof hex === "string" ? /^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/.exec(hex.trim()) : null;
    if (!m) return "rgba(120, 120, 120, " + a + ")";
    return "rgba(" + parseInt(m[1], 16) + ", " + parseInt(m[2], 16) + ", " + parseInt(m[3], 16) + ", " + a + ")";
  }
  PlanUI.colorTint = colorTint;

  function chipsModel(vm, ui) {
    var projects = (vm && vm.projects) ? vm.projects : [];
    var allNames = projects.map(function (p) { return p.name; });
    var filterProjects = (ui && ui.filters && ui.filters.projects) ? ui.filters.projects : null;
    var hasFilter = filterProjects && filterProjects.size > 0;
    var focusProj = (ui && ui.focusProject && ui.focusProject !== "all" && ui.focusProject !== "All") ? ui.focusProject : null;
    var anySelected = hasFilter || !!focusProj, totalTasks = 0, projectChips = [];
    for (var i = 0; i < projects.length; i++) {
      var p = projects[i], pCount = 0, pDone = 0;
      if (p.phases && Array.isArray(p.phases)) {
        for (var j = 0; j < p.phases.length; j++) {
          pCount += (p.phases[j].total || 0); pDone += (p.phases[j].done || 0);
        }
      } else {
        pCount = p.total || 0; pDone = p.done || 0;
      }
      totalTasks += pCount;
      var percent = pCount > 0 ? Math.round((pDone / pCount) * 100) : 0;
      var isActive = hasFilter ? filterProjects.has(p.name) : (focusProj ? (p.name === focusProj) : false);
      projectChips.push({
        id: p.name, label: p.label || p.name, count: pCount,
        done: pDone, percent: percent, color: projectColor(p.name, allNames), active: isActive
      });
    }
    return [{ id: "all", label: "All", count: totalTasks, color: null, active: !anySelected }].concat(projectChips);
  }
  PlanUI.chipsModel = chipsModel;

  function uniqueSortedStrings(arr) {
    var out = [], seen = {};
    for (var i = 0; i < arr.length; i++) {
      var item = arr[i];
      if (typeof item === "string" && !seen[item]) { seen[item] = true; out.push(item); }
    }
    return out.sort(function (a, b) { return a < b ? -1 : (a > b ? 1 : 0); });
  }

  function chipClick(selected, id, mods, known) {
    var sel = Array.isArray(selected) ? selected.slice() : [];
    if (!id || typeof id !== "string") return uniqueSortedStrings(sel);
    if (known) {
      var isKnown = Array.isArray(known) ? (known.indexOf(id) >= 0) : ((typeof known.has === "function") && known.has(id));
      if (!isKnown && id !== "all") return uniqueSortedStrings(sel);
    }
    if (id === "all") return [];
    var isMod = !!(mods && (mods.ctrl || mods.meta || mods.shift || mods.ctrlKey || mods.metaKey || mods.shiftKey));
    var cur = uniqueSortedStrings(sel);
    if (!isMod) return (cur.length === 1 && cur[0] === id) ? [] : [id];
    var idx = cur.indexOf(id);
    if (idx >= 0) {
      cur.splice(idx, 1);
      return cur.length === 0 ? [] : cur;
    }
    cur.push(id);
    return uniqueSortedStrings(cur);
  }
  PlanUI.chipClick = chipClick;

  function parseBox(b) {
    if (!b || typeof b !== "object") return null;
    var x1 = (typeof b.x1 === "number") ? b.x1 : ((typeof b.x === "number") ? b.x : NaN);
    var y1 = (typeof b.y1 === "number") ? b.y1 : ((typeof b.y === "number") ? b.y : NaN);
    var x2 = (typeof b.x2 === "number") ? b.x2 : ((typeof b.w === "number" && !isNaN(x1)) ? x1 + b.w : NaN);
    var y2 = (typeof b.y2 === "number") ? b.y2 : ((typeof b.h === "number" && !isNaN(y1)) ? y1 + b.h : NaN);
    if (!isFinite(x1) || !isFinite(y1) || !isFinite(x2) || !isFinite(y2)) return null;
    return { x1: Math.min(x1, x2), y1: Math.min(y1, y2), x2: Math.max(x1, x2), y2: Math.max(y1, y2) };
  }

  function crossProduct(a, b, c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
  }

  function andrewMonotoneChain(pts) {
    if (pts.length <= 2) return pts.slice();
    var sorted = pts.slice().sort(function (a, b) { return a.x === b.x ? a.y - b.y : a.x - b.x; });
    var unique = [];
    for (var i = 0; i < sorted.length; i++) {
      if (i === 0 || sorted[i].x !== sorted[i - 1].x || sorted[i].y !== sorted[i - 1].y) unique.push(sorted[i]);
    }
    if (unique.length <= 2) return unique;
    var lower = [];
    for (var j = 0; j < unique.length; j++) {
      while (lower.length >= 2 && crossProduct(lower[lower.length - 2], lower[lower.length - 1], unique[j]) <= 0) lower.pop();
      lower.push(unique[j]);
    }
    var upper = [];
    for (var k = unique.length - 1; k >= 0; k--) {
      while (upper.length >= 2 && crossProduct(upper[upper.length - 2], upper[upper.length - 1], unique[k]) <= 0) upper.pop();
      upper.push(unique[k]);
    }
    lower.pop(); upper.pop();
    return lower.concat(upper);
  }

  function hullFor(boxes, pad) {
    if (!boxes || !boxes.length) return null;
    var p = (typeof pad === "number" && isFinite(pad)) ? pad : 24, valid = [];
    for (var i = 0; i < boxes.length; i++) {
      var pb = parseBox(boxes[i]);
      if (pb) valid.push(pb);
    }
    if (!valid.length) return null;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, allCorners = [];
    for (var b = 0; b < valid.length; b++) {
      var bx = valid[b], px1 = bx.x1 - p, py1 = bx.y1 - p, px2 = bx.x2 + p, py2 = bx.y2 + p;
      if (px1 < minX) minX = px1; if (py1 < minY) minY = py1;
      if (px2 > maxX) maxX = px2; if (py2 > maxY) maxY = py2;
      allCorners.push({ x: px1, y: py1 }, { x: px2, y: py1 }, { x: px2, y: py2 }, { x: px1, y: py2 });
    }
    var bbox = { x1: minX, y1: minY, x2: maxX, y2: maxY };
    var labelAt = { x: (minX + maxX) / 2, y: minY + 18 };
    var points = (valid.length === 1)
      ? [{ x: valid[0].x1 - p, y: valid[0].y1 - p }, { x: valid[0].x2 + p, y: valid[0].y1 - p },
         { x: valid[0].x2 + p, y: valid[0].y2 + p }, { x: valid[0].x1 - p, y: valid[0].y2 + p }]
      : andrewMonotoneChain(allCorners);
    return { points: points, labelAt: labelAt, bbox: bbox };
  }
  PlanUI.hullFor = hullFor;

  function pointInPolygon(pt, points) {
    if (!pt || typeof pt.x !== "number" || typeof pt.y !== "number") return false;
    if (!points || !Array.isArray(points) || points.length < 3) return false;
    var x = pt.x, y = pt.y, n = points.length;
    for (var i = 0; i < n; i++) {
      var p1 = points[i], p2 = points[(i + 1) % n];
      var cross = (p2.x - p1.x) * (y - p1.y) - (p2.y - p1.y) * (x - p1.x);
      if (Math.abs(cross) < 1e-9) {
        var minX = Math.min(p1.x, p2.x) - 1e-9, maxX = Math.max(p1.x, p2.x) + 1e-9;
        var minY = Math.min(p1.y, p2.y) - 1e-9, maxY = Math.max(p1.y, p2.y) + 1e-9;
        if (x >= minX && x <= maxX && y >= minY && y <= maxY) return true;
      }
    }
    var inside = false;
    for (var a = 0, c = n - 1; a < n; c = a++) {
      var xi = points[a].x, yi = points[a].y, xj = points[c].x, yj = points[c].y;
      var intersect = ((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }
  PlanUI.pointInPolygon = pointInPolygon;

  function fitBoxFor(boxesByProject, selected) {
    if (!boxesByProject || typeof boxesByProject !== "object") return null;
    var targetKeys = [];
    if (selected) {
      if (Array.isArray(selected)) {
        if (selected.length > 0) targetKeys = selected;
      } else if (typeof selected.forEach === "function") {
        selected.forEach(function (k) { targetKeys.push(k); });
      }
    }
    if (targetKeys.length === 0) targetKeys = Object.keys(boxesByProject);
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity, count = 0;
    for (var i = 0; i < targetKeys.length; i++) {
      var raw = boxesByProject[targetKeys[i]];
      if (!raw) continue;
      var pb = parseBox(raw);
      if (!pb) continue;
      if (pb.x1 < minX) minX = pb.x1;
      if (pb.y1 < minY) minY = pb.y1;
      if (pb.x2 > maxX) maxX = pb.x2;
      if (pb.y2 > maxY) maxY = pb.y2;
      count++;
    }
    if (count === 0) return null;
    return { x1: minX, y1: minY, x2: maxX, y2: maxY };
  }
  PlanUI.fitBoxFor = fitBoxFor;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : (typeof global !== "undefined" ? global : this));

