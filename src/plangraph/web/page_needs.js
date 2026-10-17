/* PlanUI needs-you page — unified rows, filters, sort, URL state (PG-NEEDS-YOU).
 * Pure only: no DOM here; render() is added by page_needs_view.js.
 * ES5, loads in node without a document.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var FACETS = ["type", "project", "priority", "state"];
  var AGES = ["", "1", "7", "30", "30+"];
  var COLS = ["type", "id", "title", "project", "priority", "state", "age"];
  var DEFAULT_SORT = { col: "priority", dir: "desc" };

  function effStatus(q) {
    return (PlanUI.QAPage && PlanUI.QAPage.effectiveStatus) ?
      PlanUI.QAPage.effectiveStatus(q) : String(q.status || "");
  }
  function ageOf(x, now) {
    var t = x ? Date.parse(x) : NaN;
    if (isNaN(t)) return null;
    return Math.max(0, Math.floor((now - t) / 86400000));
  }
  function num(v) { var n = Number(v); return v === "" || v === null || v === undefined || isNaN(n) ? null : n; }
  function str(v) { return v === null || v === undefined ? "" : String(v); }

  function rows(vm, nowMs) {
    var now = typeof nowMs === "number" ? nowMs : Date.now();
    var out = [], list = (vm && vm.needsYou) || [], i, e;
    for (i = 0; i < list.length; i++) {
      e = list[i];
      if (e.text !== undefined) continue;
      out.push({ type: "task", id: e.key, title: str(e.title), project: str(e.project),
        priority: num(e.priority), state: str(e.display),
        ageDays: ageOf(e.updatedAt || e.createdAt, now), key: str(e.key), ref: e });
    }
    list = (vm && vm.questions) || [];
    for (i = 0; i < list.length; i++) {
      e = list[i];
      out.push({ type: "question", id: e.id, title: str(e.text), project: str(e.project),
        priority: null, state: effStatus(e), ageDays: null, key: str(e.item), ref: e });
    }
    list = (vm && vm.decisions) || [];
    for (i = 0; i < list.length; i++) {
      e = list[i];
      out.push({ type: "decision", id: e.id, title: str(e.title), project: str(e.project),
        priority: null, state: "decided", ageDays: ageOf(e.date, now),
        key: (e.items && e.items[0]) || "", ref: e });
    }
    return out;
  }

  function inNum(list, v) {
    for (var i = 0; i < list.length; i++) if (Number(list[i]) === v) return true;
    return false;
  }

  function filter(rs, f) {
    f = f || {};
    var out = [], i, r, a, ok, list;
    for (i = 0; i < rs.length; i++) {
      r = rs[i];
      list = f.type || [];
      if (list.length && list.indexOf(str(r.type)) < 0) continue;
      list = f.project || [];
      if (list.length && list.indexOf(str(r.project)) < 0) continue;
      list = f.state || [];
      if (list.length && list.indexOf(str(r.state)) < 0) continue;
      list = f.priority || [];
      if (list.length && (r.priority === null || !inNum(list, r.priority))) continue;
      ok = true;
      a = f.age || "";
      if (a) {
        if (r.ageDays === null) ok = false;
        else if (a === "30+") ok = r.ageDays > 30;
        else ok = r.ageDays <= Number(a);
      }
      if (!ok) continue;
      out.push(r);
    }
    return out;
  }

  function cmp(a, b) {
    if (PlanUI.naturalCmp) return PlanUI.naturalCmp(a, b);
    return String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0;
  }
  function val(r, col) {
    if (col === "priority") return r.priority;
    if (col === "age") return r.ageDays;
    return str(r[col]);
  }
  function sort(rs, spec) {
    rs = rs || [];
    var col = (spec && COLS.indexOf(spec.col) >= 0) ? spec.col : DEFAULT_SORT.col;
    var dir = (spec && spec.dir === "asc") ? 1 : -1;
    var idx = [], i, out = [];
    for (i = 0; i < rs.length; i++) idx.push(i);
    idx.sort(function (a, b) {
      var va = val(rs[a], col), vb = val(rs[b], col);
      var na = va === null || va === undefined || va === "";
      var nb = vb === null || vb === undefined || vb === "";
      if (na || nb) { if (na && nb) return a - b; return na ? 1 : -1; }
      var c = (typeof va === "number" && typeof vb === "number") ? va - vb : cmp(va, vb);
      return c === 0 ? a - b : c * dir;
    });
    for (i = 0; i < idx.length; i++) out.push(rs[idx[i]]);
    return out;
  }

  function encList(v) {
    var p = [], i;
    for (i = 0; i < v.length; i++) p.push(encodeURIComponent(str(v[i])));
    return p.join(",");
  }
  function encode(f, spec, rowId) {
    f = f || {};
    spec = spec || DEFAULT_SORT;
    var p = [], i, key, col, dir;
    for (i = 0; i < FACETS.length; i++) {
      key = FACETS[i];
      if (f[key] && f[key].length) p.push("n_" + key + "=" + encList(f[key]));
    }
    if (f.age && AGES.indexOf(String(f.age)) >= 0) p.push("n_age=" + encodeURIComponent(f.age));
    col = (spec && COLS.indexOf(spec.col) >= 0) ? spec.col : DEFAULT_SORT.col;
    dir = (spec && spec.dir === "asc") ? "asc" : "desc";
    if (col !== DEFAULT_SORT.col || dir !== DEFAULT_SORT.dir) p.push("n_sort=" + col + ":" + dir);
    if (rowId !== undefined && rowId !== null && rowId !== "") p.push("n_row=" + encodeURIComponent(str(rowId)));
    return p.join("&");
  }

  function decode(search) {
    var f = { type: [], project: [], priority: [], state: [], age: "" };
    var spec = { col: DEFAULT_SORT.col, dir: DEFAULT_SORT.dir };
    var row = "";
    var raw = search == null ? "" : String(search);
    if (raw.charAt(0) === "?" || raw.charAt(0) === "#") raw = raw.slice(1);
    var pairs = raw.split("&"), i, eq, k, v, j, parts, n;
    for (i = 0; i < pairs.length; i++) {
      if (!pairs[i]) continue;
      eq = pairs[i].indexOf("=");
      k = eq >= 0 ? pairs[i].slice(0, eq) : pairs[i];
      v = eq >= 0 ? pairs[i].slice(eq + 1) : "";
      try { v = decodeURIComponent(v); } catch (e) { v = v; }
      if (k === "n_type" || k === "n_project" || k === "n_state") {
        parts = v.split(",");
        for (j = 0; j < parts.length; j++) if (parts[j]) f[k.slice(2)].push(parts[j]);
      } else if (k === "n_priority") {
        parts = v.split(",");
        for (j = 0; j < parts.length; j++) {
          n = Number(parts[j]);
          if (parts[j] !== "" && !isNaN(n)) f.priority.push(n);
        }
      } else if (k === "n_age") {
        if (AGES.indexOf(v) >= 0) f.age = v;
      } else if (k === "n_sort") {
        parts = v.split(":");
        if (COLS.indexOf(parts[0]) >= 0) {
          spec.col = parts[0];
          spec.dir = parts[1] === "asc" ? "asc" : "desc";
        }
      } else if (k === "n_row") {
        row = v;
      }
    }
    return { f: f, sort: spec, row: row };
  }

  PlanUI.NeedsPage = { rows: rows, filter: filter, sort: sort, encode: encode, decode: decode,
    FACETS: FACETS, AGES: AGES, COLS: COLS, DEFAULT_SORT: DEFAULT_SORT };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
