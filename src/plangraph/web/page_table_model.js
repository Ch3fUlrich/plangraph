/* PlanUI table model — pure table derivation (GU-TB1).
 * Columns, filters, search, multi-column stable sort, grouping, row ranges.
 * ES5/ES2015 without modules: attaches to PlanUI and exports for node.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var TABLE_STATES = ["ready", "blocked", "claimed", "in_review", "open", "done", "cancelled"];
  function col(id, label, w, num, dir, on) { return { id: id, label: label, w: w, num: num, dir: dir, on: on }; }
  var TABLE_COLUMNS = [
    col("state", "State", 112, false, "asc", true), col("project", "Project", 120, false, "asc", true),
    col("title", "Title", 420, false, "asc", true), col("phase", "Phase", 150, false, "asc", true),
    col("owner", "Owner", 130, false, "asc", true), col("priority", "Prio", 64, true, "desc", true),
    col("est", "Est h", 64, true, "desc", true), col("kind", "Kind", 80, false, "asc", false),
    col("op", "Op", 56, false, "desc", true), col("unblocks", "Unblocks", 88, true, "desc", true)
  ];
  var TABLE_METRICS = { rowH: 36, groupH: 32, detailH: 128, overscan: 6 };
  PlanUI.TABLE_STATES = TABLE_STATES;
  PlanUI.TABLE_COLUMNS = TABLE_COLUMNS;
  PlanUI.TABLE_METRICS = TABLE_METRICS;

  var prevNaturalCmp = PlanUI.naturalCmp;
  function naturalCmp(a, b) {
    return prevNaturalCmp ? prevNaturalCmp(a, b) : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  }
  PlanUI.naturalCmp = prevNaturalCmp || naturalCmp;
  function operatorNeeded(row) { return !!(row && row.operator && row.display !== "done" && row.display !== "cancelled"); }
  function plainAsc(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  function tableContext(vm) {
    var rows = [];
    if (vm && vm.items) {
      if (typeof vm.items.forEach === "function") vm.items.forEach(function (v) { if (v) rows.push(v); });
      else if (Array.isArray(vm.items)) rows = vm.items.filter(function (v) { return v; });
    }
    var projects = [], projIdx = {};
    if (vm && Array.isArray(vm.projects)) {
      vm.projects.forEach(function (p, idx) {
        projects.push({ name: p.name, label: p.label, color: p.color });
        projIdx[p.name] = idx;
      });
    }
    var byKey = {}, rowIdx = new Map(), oSet = {}, phSet = {}, kSet = {}, prSet = {}, missing = false;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!r) continue;
      rowIdx.set(r, i);
      if (r.key) byKey[r.key] = r;
      if (r.owner) oSet[r.owner] = true; if (r.phase) phSet[r.phase] = true;
      if (r.kind) kSet[r.kind] = true; if (r.priority != null) prSet[Number(r.priority) || 0] = true;
      if (projIdx[r.project == null ? "" : r.project] === undefined) missing = true;
    }
    if (missing && projIdx[""] === undefined) {
      projIdx[""] = projects.length;
      projects.push({ name: "", label: "(none)", color: "#2f6f4f" });
    }
    return {
      rows: rows, projects: projects, projIdx: projIdx, rowIdx: rowIdx,
      owners: Object.keys(oSet).sort(plainAsc), phases: Object.keys(phSet).sort(naturalCmp),
      kinds: Object.keys(kSet).sort(plainAsc),
      priorities: Object.keys(prSet).map(Number).sort(function (a, b) { return a - b; }),
      byKey: byKey, total: rows.length
    };
  }

  function tableDefaults() {
    return {
      sort: [{ col: "priority", dir: "desc" }],
      filters: { project: [], phase: [], state: [], owner: [], priority: [], kind: [], op: false },
      search: "", cols: ["state", "project", "title", "phase", "owner", "priority", "est", "op", "unblocks"],
      group: "project>phase", collapsed: []
    };
  }
  var VALID_COL_MAP = {}, COL_CANONICAL = TABLE_COLUMNS.map(function (c) {
    VALID_COL_MAP[c.id] = c; return c.id;
  });

  function normList(arr, isNum) {
    if (!Array.isArray(arr)) return [];
    var seen = {}, out = [];
    for (var i = 0; i < arr.length; i++) {
      var v = isNum ? (Number(arr[i]) || 0) : String(arr[i]);
      if (!seen[v]) { seen[v] = true; out.push(v); }
    }
    return out.sort(isNum ? function (a, b) { return a - b; } : plainAsc);
  }

  function tableNormalize(st) {
    var def = tableDefaults();
    if (!st || typeof st !== "object") return def;
    var sort = [], seen = {};
    if (Array.isArray(st.sort)) {
      for (var i = 0; i < st.sort.length; i++) {
        var s = st.sort[i];
        if (s && VALID_COL_MAP[s.col] && !seen[s.col]) {
          seen[s.col] = true;
          sort.push({ col: s.col, dir: s.dir === "desc" ? "desc" : "asc" });
          if (sort.length === 4) break;
        }
      }
    }
    if (!sort.length) sort = def.sort.map(function (x) { return { col: x.col, dir: x.dir }; });
    var sf = (st.filters && typeof st.filters === "object") ? st.filters : {};
    var reqCols = Array.isArray(st.cols) ? st.cols : def.cols, cSet = { title: true };
    reqCols.forEach(function (id) { cSet[id] = true; });
    var cols = COL_CANONICAL.filter(function (id) { return cSet[id]; });
    return {
      sort: sort,
      filters: {
        project: normList(sf.project, false), phase: normList(sf.phase, false),
        state: normList(sf.state, false), owner: normList(sf.owner, false),
        priority: normList(sf.priority, true), kind: normList(sf.kind, false), op: Boolean(sf.op)
      },
      search: st.search == null ? "" : String(st.search),
      cols: cols.length ? cols : ["title"],
      group: st.group === "none" ? "none" : "project>phase",
      collapsed: normList(st.collapsed, false)
    };
  }

  function makeSet(arr) {
    if (!arr || !arr.length) return null;
    var s = {}; for (var i = 0; i < arr.length; i++) s[arr[i]] = true; return s;
  }

  function tableFilter(ctx, st) {
    if (!ctx || !Array.isArray(ctx.rows)) return [];
    var rows = ctx.rows, f = (st && st.filters) || {};
    var pSet = makeSet(f.project), phSet = makeSet(f.phase), sSet = makeSet(f.state);
    var oSet = makeSet(f.owner), prSet = makeSet(f.priority), kSet = makeSet(f.kind);
    var op = !!f.op, search = st ? (st.search == null ? "" : String(st.search).trim()) : "";

    return rows.filter(function (r) {
      if (!r) return false; if (pSet && !pSet[r.project == null ? "" : r.project]) return false;
      if (phSet && !phSet[r.phase == null ? "" : r.phase]) return false;
      if (sSet && !sSet[r.display]) return false;
      if (oSet && !oSet[r.owner]) return false;
      if (prSet && !prSet[Number(r.priority) || 0]) return false;
      if (kSet && !kSet[r.kind]) return false;
      if (op && !operatorNeeded(r)) return false;
      if (search && PlanUI.Query && PlanUI.Query.match && !PlanUI.Query.match(r, search)) return false;
      return true;
    });
  }

  var STATE_IDX = {};
  TABLE_STATES.forEach(function (s, idx) { STATE_IDX[s] = idx; });

  function compareValues(col, dir, a, b, ctx) {
    var d = 0, p1, p2;
    if (col === "state") {
      d = (STATE_IDX[a.display] === undefined ? 99 : STATE_IDX[a.display]) - (STATE_IDX[b.display] === undefined ? 99 : STATE_IDX[b.display]);
    } else if (col === "project") {
      p1 = ctx.projIdx[a.project || ""]; p2 = ctx.projIdx[b.project || ""];
      d = (p1 === undefined ? 9999 : p1) - (p2 === undefined ? 9999 : p2);
    } else if (col === "phase") {
      p1 = ctx.projIdx[a.project || ""]; p2 = ctx.projIdx[b.project || ""];
      d = naturalCmp(a.phase || "", b.phase || "") || ((p1 === undefined ? 9999 : p1) - (p2 === undefined ? 9999 : p2));
    } else if (col === "title" || col === "owner" || col === "kind") {
      d = plainAsc(String(a[col] || "").toLowerCase(), String(b[col] || "").toLowerCase());
    } else if (col === "priority") d = (Number(a.priority) || 0) - (Number(b.priority) || 0);
    else if (col === "est") d = (Number(a.estimate_h) || 0) - (Number(b.estimate_h) || 0);
    else if (col === "unblocks") d = (a.unblocks ? a.unblocks.length : 0) - (b.unblocks ? b.unblocks.length : 0);
    else if (col === "op") d = (operatorNeeded(a) ? 1 : 0) - (operatorNeeded(b) ? 1 : 0);
    return dir === "desc" ? -d : d;
  }

  function tableSort(rows, st, ctx) {
    if (!rows || !rows.length) return [];
    var sortKeys = (st && Array.isArray(st.sort)) ? st.sort : [{ col: "priority", dir: "desc" }];
    var safeCtx = ctx || { projIdx: {} };
    var decorated = rows.map(function (r, i) {
      var orig = (ctx && ctx.rowIdx && ctx.rowIdx.has(r)) ? ctx.rowIdx.get(r) : ((ctx && ctx.rows) ? ctx.rows.indexOf(r) : i);
      return { r: r, orig: orig };
    });
    decorated.sort(function (itemA, itemB) {
      for (var k = 0; k < sortKeys.length; k++) {
        var cmp = compareValues(sortKeys[k].col, sortKeys[k].dir, itemA.r, itemB.r, safeCtx);
        if (cmp !== 0) return cmp;
      }
      return itemA.orig - itemB.orig;
    });
    return decorated.map(function (d) { return d.r; });
  }

  function stateCounts(list) {
    var d = 0, r = 0, b = 0;
    for (var i = 0; i < list.length; i++) {
      var s = list[i].display;
      if (s === "done") d++; else if (s === "ready") r++; else if (s === "blocked") b++;
    }
    return { d: d, r: r, b: b };
  }
  function emitRow(entries, rows, r, color, exp) {
    rows.push(r); entries.push({ type: "row", row: r, color: color });
    if (exp[r.key]) entries.push({ type: "detail", row: r, color: color });
  }
  function emitGroup(entries, headers, level, id, proj, ph, count, all, counts, col) {
    headers.push(entries.length);
    entries.push({
      type: "group", level: level, id: id, project: proj.name, phase: ph,
      label: ph == null ? proj.label : ph, color: proj.color, count: count, all: all,
      done: counts.d, ready: counts.r, blocked: counts.b, collapsed: col
    });
  }

  function tableApply(ctx, st, opts) {
    st = tableNormalize(st);
    opts = opts || {};
    var m = opts.metrics || {}, rowH = m.rowH != null ? m.rowH : TABLE_METRICS.rowH;
    var groupH = m.groupH != null ? m.groupH : TABLE_METRICS.groupH, detailH = m.detailH != null ? m.detailH : TABLE_METRICS.detailH;
    var exp = {};
    if (Array.isArray(opts.expanded)) opts.expanded.forEach(function (k) { exp[k] = true; });
    var filtered = tableFilter(ctx, st);
    var fSort = st.group === "project>phase" ? st.sort.filter(function (s) { return s.col !== "project" && s.col !== "phase"; }) : st.sort;
    var sorted = tableSort(filtered, { sort: fSort }, ctx);
    var entries = [], headers = [], rows = [];
    if (st.group === "none") {
      sorted.forEach(function (r) {
        var pIdx = ctx.projIdx[r.project == null ? "" : r.project];
        emitRow(entries, rows, r, (ctx.projects[pIdx] && ctx.projects[pIdx].color) || "#2f6f4f", exp);
      });
    } else {
      var colSet = {};
      st.collapsed.forEach(function (id) { colSet[id] = true; });
      function projOf(n) { n = n || ""; return ctx.projIdx[n] !== undefined ? n : ""; } /* unknown projects fold into the synthetic "" (none) project (C4/C8) */
      var allP = {}, allPh = {};
      ctx.rows.forEach(function (cr) {
        var cP = projOf(cr && cr.project), cPh = (cr && cr.phase) || "";
        allP[cP] = (allP[cP] || 0) + 1;
        allPh[cP + "|" + cPh] = (allPh[cP + "|" + cPh] || 0) + 1;
      });
      var byProj = {};
      sorted.forEach(function (sr) {
        var sp = projOf(sr && sr.project), sph = (sr && sr.phase) || "";
        var pObj = byProj[sp] = byProj[sp] || { list: [], phases: {}, order: [] };
        pObj.list.push(sr);
        if (!pObj.phases[sph]) { pObj.phases[sph] = []; pObj.order.push(sph); }
        pObj.phases[sph].push(sr);
      });
      ctx.projects.forEach(function (pr) {
        var pData = byProj[pr.name];
        if (!pData || !pData.list.length) return;
        var pId = "p:" + pr.name, pCol = !!colSet[pId];
        emitGroup(entries, headers, "project", pId, pr, null, pData.list.length, allP[pr.name] || 0, stateCounts(pData.list), pCol);
        if (pCol) return;
        pData.order.sort(function (a, b) {
          var rA = pData.phases[a][0], rB = pData.phases[b][0];
          return ((rA ? rA.phaseIdx : 0) - (rB ? rB.phaseIdx : 0)) || naturalCmp(a, b);
        });
        pData.order.forEach(function (ph) {
          var phRows = pData.phases[ph], phId = "ph:" + pr.name + "|" + ph, phCol = !!colSet[phId];
          emitGroup(entries, headers, "phase", phId, pr, ph, phRows.length, allPh[pr.name + "|" + ph] || 0, stateCounts(phRows), phCol);
          if (!phCol) phRows.forEach(function (rk) { emitRow(entries, rows, rk, pr.color, exp); });
        });
      });
    }
    var tops = new Array(entries.length + 1), accH = 0;
    tops[0] = 0;
    for (var ti = 0; ti < entries.length; ti++) {
      var t = entries[ti].type;
      accH += t === "group" ? groupH : t === "row" ? rowH : detailH;
      tops[ti + 1] = accH;
    }
    return {
      total: ctx.total, matched: filtered.length,
      rows: st.group === "none" ? rows : sorted,
      entries: entries, tops: tops, height: accH, headers: headers
    };
  }

  function tableWindow(res, scrollTop, viewportH, overscan) {
    if (overscan === undefined || overscan === null) overscan = TABLE_METRICS.overscan;
    var n = res && res.entries ? res.entries.length : 0, h = (res && res.height) || 0, tops = (res && res.tops) || [0];
    if (n === 0) return { start: 0, end: 0, padTop: 0, padBottom: 0, count: 0 };
    var st = Math.min(Math.max(0, Number(scrollTop) || 0), h), vh = Number(viewportH);
    var low = 0, high = n - 1, rawStart = n;
    while (low <= high) {
      var mid = (low + high) >> 1;
      if (tops[mid + 1] > st) { rawStart = mid; high = mid - 1; } else { low = mid + 1; }
    }
    var rawEnd = rawStart;
    if (vh > 0) {
      low = 0; high = n;
      while (low <= high) {
        var m2 = (low + high) >> 1;
        if (tops[m2] >= st + vh) { rawEnd = m2; high = m2 - 1; } else { low = m2 + 1; }
      }
      if (tops[n] < st + vh) rawEnd = n;   // viewport bottom beyond content: render all remaining
    }
    var start = Math.max(0, rawStart - overscan), end = Math.min(n, (vh <= 0 ? rawStart : rawEnd) + overscan);
    return { start: start, end: end, padTop: tops[start], padBottom: h - tops[end], count: Math.max(0, end - start) };
  }

  function tableStickyGroups(res, index) {
    var entries = res && res.entries;
    if (!entries || !entries.length || index == null || index < 0) return { project: null, phase: null };
    var foundProj = null, foundPh = null;
    for (var i = Math.min(index, entries.length - 1); i >= 0; i--) {
      var ent = entries[i];
      if (ent.type === "group") {
        if (ent.level === "phase" && !foundPh && !foundProj) foundPh = ent;
        else if (ent.level === "project" && !foundProj) { foundProj = ent; break; }
      }
    }
    if (foundPh && (!foundProj || foundPh.project !== foundProj.project)) foundPh = null;
    return { project: foundProj || null, phase: foundPh || null };
  }

  function tableFacets(ctx, st) {
    st = tableNormalize(st);
    var list = ["project", "phase", "state", "owner", "priority", "kind"], res = {}, pLbl = {};
    for (var pi = 0; pi < ctx.projects.length; pi++) pLbl[ctx.projects[pi].name] = ctx.projects[pi].label;
    for (var fi = 0; fi < list.length; fi++) {
      var fn = list[fi], subF = Object.assign({}, st.filters);
      delete subF[fn];
      var rF = tableFilter(ctx, Object.assign({}, st, { filters: subF })), cMap = {};
      for (var ri = 0; ri < rF.length; ri++) {
        var r = rF[ri], v = fn === "priority" ? (Number(r.priority) || 0) : fn === "state" ? r.display : (r[fn] || "");
        cMap[v] = (cMap[v] || 0) + 1;
      }
      var sel = st.filters[fn] || [];
      for (var si = 0; si < sel.length; si++) if (cMap[sel[si]] === undefined) cMap[sel[si]] = 0;
      var items = [];
      for (var vk in cMap) {
        var rv = fn === "priority" ? Number(vk) : vk;
        items.push({ value: rv, label: fn === "project" ? (pLbl[rv] || String(rv)) : String(rv), count: cMap[vk] });
      }
      items.sort(fn === "project" ? function (a, b) {
        var ia = ctx.projIdx[a.value], ib = ctx.projIdx[b.value];
        return ((ia === undefined ? 9999 : ia) - (ib === undefined ? 9999 : ib)) || plainAsc(a.label, b.label);
      } : fn === "phase" ? function (a, b) { return naturalCmp(a.value, b.value); }
        : fn === "state" ? function (a, b) { return ((STATE_IDX[a.value] === undefined ? 99 : STATE_IDX[a.value]) - (STATE_IDX[b.value] === undefined ? 99 : STATE_IDX[b.value])); }
        : fn === "owner" ? function (a, b) { return (b.count - a.count) || plainAsc(a.value, b.value); }
        : fn === "priority" ? function (a, b) { return b.value - a.value; }
        : function (a, b) { return plainAsc(a.value, b.value); });
      res[fn] = items;
    }
    var opSt = Object.assign({}, st, { filters: Object.assign({}, st.filters) });
    delete opSt.filters.op;
    var opRows = tableFilter(ctx, opSt), on = 0, off = 0;
    opRows.forEach(function (r) { if (operatorNeeded(r)) on++; else off++; });
    res.op = { on: on, off: off }; return res;
  }
  function tableToggleSort(st, col, additive) {
    st = tableNormalize(st);
    var colDef = VALID_COL_MAP[col];
    if (!colDef) return tableNormalize(st);
    var sort = st.sort.slice(), idx = -1;
    for (var i = 0; i < sort.length; i++) if (sort[i].col === col) { idx = i; break; }
    if (!additive) {
      sort = (sort.length === 1 && idx === 0) ? [{ col: col, dir: sort[0].dir === "asc" ? "desc" : "asc" }] : [{ col: col, dir: colDef.dir }];
    } else if (idx >= 0) {
      sort[idx] = { col: col, dir: sort[idx].dir === "asc" ? "desc" : "asc" };
    } else {
      var entry = { col: col, dir: colDef.dir };
      if (sort.length >= 4) sort[3] = entry; else sort.push(entry);
    }
    return tableNormalize(Object.assign({}, st, { sort: sort }));
  }
  function tableToggleCollapse(st, id) {
    var c = tableNormalize(st).collapsed.slice(), i = c.indexOf(id);
    if (i >= 0) c.splice(i, 1); else c.push(id);
    return tableNormalize(Object.assign({}, st, { collapsed: c }));
  }
  function tableCollapseAll(ctx, st, on) {
    if (!on) return tableNormalize(Object.assign({}, st, { collapsed: [] }));
    var ids = {};
    if (ctx && ctx.projects) ctx.projects.forEach(function (p) { ids["p:" + p.name] = true; });
    if (ctx && ctx.rows) ctx.rows.forEach(function (r) { ids["ph:" + (r.project || "") + "|" + (r.phase || "")] = true; });
    return tableNormalize(Object.assign({}, st, { collapsed: Object.keys(ids).sort(plainAsc) }));
  }
  function tableSetColumn(st, id, on) {
    st = tableNormalize(st);
    if (!VALID_COL_MAP[id]) return tableNormalize(st);
    var cols = st.cols.slice();
    if (on && cols.indexOf(id) < 0) cols.push(id);
    else if (!on && id !== "title" && cols.length > 1) {
      var i = cols.indexOf(id); if (i >= 0) cols.splice(i, 1);
    }
    return tableNormalize(Object.assign({}, st, { cols: cols }));
  }
  function tableSetGroup(st, group) {
    return tableNormalize(Object.assign({}, tableNormalize(st), { group: group === "none" ? "none" : "project>phase" }));
  }

  Object.assign(PlanUI, {
    tableContext: tableContext, tableDefaults: tableDefaults, tableNormalize: tableNormalize,
    tableFilter: tableFilter, tableSort: tableSort, tableApply: tableApply,
    tableWindow: tableWindow, tableStickyGroups: tableStickyGroups, tableFacets: tableFacets,
    tableToggleSort: tableToggleSort, tableToggleCollapse: tableToggleCollapse,
    tableCollapseAll: tableCollapseAll, tableSetColumn: tableSetColumn, tableSetGroup: tableSetGroup
  });

  if (typeof module !== "undefined" && module.exports) {
    module.exports = PlanUI;
  }
})(typeof window !== "undefined" ? window : globalThis);
