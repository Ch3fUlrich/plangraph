/* PlanUI table page — facet filters, column chooser, URL persistence (G1-G3).
 * Pure state + save/load; bar() is the only part that needs a document.
 * ES5, createElement/textContent only. Loads in node without a DOM.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var FACETS = ["state", "owner", "priority", "kind"];
  var LABELS = { state: "State", owner: "Owner", priority: "Prio", kind: "Kind" };
  var STATE_ORDER = ["ready", "blocked", "claimed", "in_review", "open", "done", "cancelled"];

  var state = { filters: { state: [], owner: [], priority: [], kind: [], op: false }, hide: {} };
  var base = "", openSet = {};

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function disp(r) { return String(r.display || r.state || ""); }
  function operatorNeeded(r) {
    var d = disp(r);
    return !!(r.operator && d !== "done" && d !== "cancelled");
  }
  function pass(r) {
    var f = state.filters;
    if (f.state.length && f.state.indexOf(disp(r)) < 0) return false;
    if (f.owner.length && f.owner.indexOf(String(r.owner || "")) < 0) return false;
    if (f.priority.length && f.priority.indexOf(Number(r.priority) || 0) < 0) return false;
    if (f.kind.length && f.kind.indexOf(String(r.kind || "")) < 0) return false;
    if (f.op && !operatorNeeded(r)) return false;
    return true;
  }
  function colOn(id) { return id === "task" ? true : !state.hide[id]; }

  function reset() {
    state.filters = { state: [], owner: [], priority: [], kind: [], op: false };
    state.hide = {};
    return state;
  }

  function hiddenIds() {
    var out = [];
    for (var id in state.hide) if (state.hide[id]) out.push(id);
    return out;
  }

  function save() {
    var f = state.filters;
    var st = {
      filters: {
        state: f.state.slice(), owner: f.owner.slice(), priority: f.priority.slice(),
        kind: f.kind.slice(), op: !!f.op, project: [], phase: []
      },
      cols: PlanUI.tableDefaults ? PlanUI.tableDefaults().cols : [], sort: [{ col: "priority", dir: "desc" }],
      group: "project>phase", collapsed: [], search: ""
    };
    var q = PlanUI.tableUrlMerge ? (PlanUI.tableUrlMerge(base, st) || "") : "";
    var hid = hiddenIds();
    if (hid.length) {
      q = (q ? q + "&" : "?") + "t_hide=" + hid.map(encodeURIComponent).join(",");
    }
    try {
      if (typeof document !== "undefined" && global.history && global.location) {
        global.history.replaceState(null, "", global.location.pathname + q + global.location.hash);
      }
    } catch (e) { /* file:// may throw */ }
    return q;
  }

  function load(search) {
    var raw = search == null ? "" : String(search);
    var trimmed = raw;
    if (trimmed.charAt(0) === "?" || trimmed.charAt(0) === "#") trimmed = trimmed.slice(1);
    base = trimmed.split("&").filter(function (p) { return p && p.indexOf("t_") !== 0; }).join("&");
    state.filters = { state: [], owner: [], priority: [], kind: [], op: false };
    state.hide = {};
    if (PlanUI.tableUrlDecode) {
      var st = PlanUI.tableUrlDecode(raw);
      if (st && st.filters) {
        FACETS.forEach(function (k) {
          if (st.filters[k]) state.filters[k] = st.filters[k].slice();
        });
        state.filters.op = !!st.filters.op;
      }
    }
    var pairs = trimmed.split("&"), i, eq, key, val, ids, j;
    for (i = 0; i < pairs.length; i++) {
      if (!pairs[i]) continue;
      eq = pairs[i].indexOf("=");
      key = eq >= 0 ? pairs[i].slice(0, eq) : pairs[i];
      if (key !== "t_hide") continue;
      val = eq >= 0 ? pairs[i].slice(eq + 1) : "";
      ids = val.split(",");
      for (j = 0; j < ids.length; j++) {
        try { ids[j] = decodeURIComponent(ids[j]); } catch (e) { ids[j] = ids[j]; }
        if (ids[j]) state.hide[ids[j]] = true;
      }
    }
    return state;
  }

  /* ---- bar ---------------------------------------------------------------- */
  function facetItems(rows, key) {
    var map = {}, keys = [], out = [], i, r, v, k;
    for (i = 0; i < rows.length; i++) {
      r = rows[i];
      if (key === "state") v = disp(r);
      else if (key === "owner") v = String(r.owner || "");
      else if (key === "kind") v = String(r.kind || "");
      else v = Number(r.priority) || 0;
      k = String(v);
      if (!map[k]) { map[k] = { v: v, n: 0 }; keys.push(k); }
      map[k].n++;
    }
    if (key === "state") {
      var extra = keys.filter(function (x) { return STATE_ORDER.indexOf(x) < 0; }).sort();
      STATE_ORDER.forEach(function (s) { if (map[s]) out.push(map[s]); });
      extra.forEach(function (x) { out.push(map[x]); });
    } else if (key === "priority") {
      keys.sort(function (a, b) { return Number(b) - Number(a); });
      keys.forEach(function (x) { out.push(map[x]); });
    } else {
      keys.sort();
      keys.forEach(function (x) { out.push(map[x]); });
    }
    return out;
  }

  function checkbox(text, checked, disabled, aria, onChange, apply) {
    var lb = el("label", "tt-fl");
    var cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!checked;
    if (disabled) cb.disabled = true;
    cb.setAttribute("aria-label", aria);
    lb.appendChild(cb);
    lb.appendChild(el("span", null, text));
    cb.addEventListener("change", function () { apply(cb.checked); save(); onChange(); });
    return lb;
  }

  /* the bar is rebuilt on every change; keep a popover open across that */
  function keepOpen(det, id) {
    det.open = !!openSet[id];
    det.addEventListener("toggle", function () { openSet[id] = det.open; });
    return det;
  }

  function addFacet(d, key, items, onChange) {
    var det = keepOpen(el("details", "tt-fd"), key);
    det.appendChild(el("summary", null, LABELS[key] + " (" + state.filters[key].length + ")"));
    var list = el("div", "tt-fdl");
    items.forEach(function (it) {
      var sel = state.filters[key];
      list.appendChild(checkbox(it.v + " (" + it.n + ")", sel.indexOf(it.v) >= 0, false,
        "Filter " + LABELS[key] + " " + it.v, onChange, function (on) {
          var i = sel.indexOf(it.v);
          if (on && i < 0) sel.push(it.v);
          if (!on && i >= 0) sel.splice(i, 1);
        }));
    });
    det.appendChild(list);
    d.appendChild(det);
  }

  function colPicker(onChange) {
    var det = keepOpen(el("details", "tt-fd"), "cols");
    det.appendChild(el("summary", null, "Columns (" + hiddenIds().length + ")"));
    var list = el("div", "tt-fdl");
    var cols = (PlanUI.TablePage && PlanUI.TablePage.columns) || PlanUI.TABLE_PAGE_COLUMNS || [];
    cols.forEach(function (c) {
      var locked = c.id === "task";
      list.appendChild(checkbox(c.label, locked ? true : !state.hide[c.id], locked,
        (locked ? "Column " : "Show column ") + c.label, onChange, function (on) {
          if (on) delete state.hide[c.id]; else state.hide[c.id] = true;
        }));
    });
    det.appendChild(list);
    return det;
  }

  function bar(allRows, onChange) {
    var d = el("div", "tt-filt");
    var rows = allRows || [];
    FACETS.forEach(function (key) { addFacet(d, key, facetItems(rows, key), onChange); });
    d.appendChild(checkbox("Operator needed", state.filters.op, false,
      "Filter operator-needed rows", onChange, function (on) { state.filters.op = !!on; }));
    d.appendChild(colPicker(onChange));
    var cl = el("button", "tt-btn tt-clear", "Clear filters");
    cl.type = "button";
    cl.setAttribute("aria-label", "Clear all table filters");
    cl.addEventListener("click", function () { reset(); save(); onChange(); });
    d.appendChild(cl);
    return d;
  }

  PlanUI.TableCtl = {
    state: state, pass: pass, colOn: colOn, operatorNeeded: operatorNeeded,
    load: load, save: save, bar: bar, reset: reset
  };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
