/* PlanUI store — the `ui` state of UI-PLAN §3 (store row of §2: `page_store.js`
 * → `Store (get/set/patch/subscribe)` → `ui` patches → `change(keys[])`).
 * Pure state container: no DOM, no I/O, ES5-safe, no modules; attaches to the
 * shared `PlanUI` namespace and exports it for node tests.
 *
 * Semantics (T1):
 *   - defaults are a FRESH object every call (new Sets/Map/objects each time,
 *     nothing shared between Store instances), overridden key-for-key by the
 *     constructor argument — partial state still renders with full defaults;
 *   - `patch` / `set` compare strictly (`!==`): an unchanged value is a no-op
 *     and emits nothing;
 *   - one `change(keys[])` event per patch, keys in patch-argument order.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  /* §3 `ui`: everything components may read besides `vm`. Persisting it
   * (localStorage / location.hash) is page_boot's job (T2), not the Store's. */
  function freshDefaults() {
    return {
      view: "graph",                     // "graph" | "table"
      sel: null,                         // selected key | null (hash param `sel`)
      query: "",
      collapsedPhases: new Set(),        // "proj|phase" ids
      collapsedProjects: new Set(),
      expanded: new Set(),               // task cards open in the graph
      filters: { projects: new Set(), phases: new Set(), states: new Set(),
                 owners: new Set(), operatorOnly: false, loopOnly: false,
                 edgeMode: "all" },      // "all" | "critical" | "blocking" | "cross"
      matchMode: "dim",                  // "dim" | "hide" (§7 graph effect)
      revealMatches: false,
      sort: { col: null, dir: "asc" },
      group: "project>phase",            // "project>phase" | "none"
      tableSel: new Set(),
      prioPending: new Map(),            // key -> int, T3 batch panel
      sidebar: { open: true, tab: "needs" },
      settings: { textSize: "M", theme: "auto", showCritical: true,
                  showCross: true, autoCollapseDone: false, animate: true },
      focusProject: null                 // phone: one project in view (A11)
    };
  }

  /* Fresh defaults with a caller override object merged on top (top level
   * only: `filters`/`settings` are replaced wholesale when overridden). */
  function defaultUI(overrides) {
    var ui = freshDefaults();
    Object.keys(overrides || {}).forEach(function (k) { ui[k] = overrides[k]; });
    return ui;
  }

  /* ---- hash codec (§3/R6: `#v=table&sel=…&q=…` + collapse/filter/focus) --
   * Compact: Sets are comma-joined percent-encoded ids; defaults omitted so
   * a fresh page keeps an empty hash. Decode never throws: each bad field
   * is ignored, the rest still applies. */
  function hEnc(v) {
    try { return encodeURIComponent(String(v)); } catch (e) { return ""; }
  }
  function hDec(v) {
    try { return decodeURIComponent(v); } catch (e) { return v; }
  }
  function hDecS(raw) {
    try { return decodeURIComponent(raw); } catch (e) { return null; }
  }
  function hEncList(set) {
    var out = [];
    try {
      if (set && typeof set.forEach === "function") {
        set.forEach(function (v) { if (v != null && String(v) !== "") out.push(hEnc(v)); });
      }
    } catch (e) { /* malformed set: encode what we have */ }
    return out.join(",");
  }
  function hDecList(raw) {
    var set = new Set();
    try {
      String(raw == null ? "" : raw).split(",").forEach(function (p) {
        if (!p) return;
        var d;
        try { d = decodeURIComponent(p); } catch (e) { return; }
        if (d) set.add(d);
      });
    } catch (e) { return null; }
    return set.size ? set : null;
  }
  /* state-like → "#…", defaults omitted */
  function hashEncode(st) {
    if (!st) return "";
    var p = [];
    try {
      if (st.view === "table") p.push("v=table");
      if (st.sel) p.push("sel=" + hEnc(st.sel));
      if (st.query) p.push("q=" + hEnc(st.query));
      var sb = st.sidebar || {};
      if (sb.tab && sb.tab !== "needs") p.push("sb=" + hEnc(sb.tab));
      if (sb.open === false) p.push("so=0");
      var cp = hEncList(st.collapsedPhases);
      if (cp) p.push("cph=" + cp);
      var cr = hEncList(st.collapsedProjects);
      if (cr) p.push("cpr=" + cr);
      var ex = hEncList(st.expanded);
      if (ex) p.push("ex=" + ex);
      if (st.focusProject) p.push("fp=" + hEnc(st.focusProject));
      var f = st.filters || {};
      var fpr = hEncList(f.projects);
      if (fpr) p.push("fpr=" + fpr);
      var fph = hEncList(f.phases);
      if (fph) p.push("fph=" + fph);
      if (f.edgeMode === "critical" || f.edgeMode === "blocking" || f.edgeMode === "cross") {
        p.push("em=" + hEnc(f.edgeMode));
      }
      if (st.matchMode === "hide") p.push("mm=hide");
    } catch (e) { /* never throws: return what we have */ }
    return p.length ? "#" + p.join("&") : "";
  }
  /* "#…" → partial patch object; malformed input never throws (R6) */
  function hashDecode(str) {
    var out = {};
    var s = String(str == null ? "" : str).replace(/^#/, "");
    if (!s) return out;
    var fPr = null, fPh = null, fEm = null;
    s.split("&").forEach(function (pair) {
      try {
        var i = pair.indexOf("=");
        if (i < 1) return;
        var k = pair.slice(0, i), raw = pair.slice(i + 1), v = hDec(raw);
        if (k === "v") out.view = v === "table" ? "table" : "graph";
        else if (k === "sel") { var s1 = hDecS(raw); if (s1) out.sel = s1; }
        else if (k === "q") { var q1 = hDecS(raw); if (q1) out.query = q1; }
        else if (k === "sb") { var t1 = hDecS(raw); if (t1) out.tab = t1; }
        else if (k === "so") out.open = v !== "0";
        else if (k === "cph") { var a = hDecList(raw); if (a) out.collapsedPhases = a; }
        else if (k === "cpr") { var b = hDecList(raw); if (b) out.collapsedProjects = b; }
        else if (k === "ex") { var c = hDecList(raw); if (c) out.expanded = c; }
        else if (k === "fp") { var f1 = hDecS(raw); if (f1) out.focusProject = f1; }
        else if (k === "fpr") { var d = hDecList(raw); if (d) fPr = d; }
        else if (k === "fph") { var e = hDecList(raw); if (e) fPh = e; }
        else if (k === "em") { if (v === "critical" || v === "blocking" || v === "cross") fEm = v; }
        else if (k === "mm") { if (v === "hide" || v === "dim") out.matchMode = v; }
      } catch (e) { /* one bad field never breaks the rest */ }
    });
    if (fPr || fPh || fEm) {
      out.filters = { projects: fPr || new Set(), phases: fPh || new Set(),
        states: new Set(), owners: new Set(), operatorOnly: false, loopOnly: false,
        edgeMode: fEm || "all" };
    }
    return out;
  }
  PlanUI.hashEncode = hashEncode;
  PlanUI.hashDecode = hashDecode;

  function Store(initial) {
    this._ui = defaultUI(initial);
    this._subs = [];
  }

  /* get() → snapshot copy of the whole ui; get(key) → one value. */
  Store.prototype.get = function (key) {
    if (key !== undefined) return this._ui[key];
    var out = {}, self = this;
    Object.keys(this._ui).forEach(function (k) { out[k] = self._ui[k]; });
    return out;
  };

  /* One `change(keys[])` event per call, keys in patch-key order, empty → none. */
  Store.prototype.patch = function (obj) {
    var changed = [], self = this;
    Object.keys(obj).forEach(function (k) {
      if (self._ui[k] !== obj[k]) {     // strict: unchanged value = no-op
        self._ui[k] = obj[k];
        changed.push(k);
      }
    });
    if (changed.length) this._emit(changed);
    return changed.length > 0;
  };

  Store.prototype.set = function (key, value) {
    var o = {};
    o[key] = value;
    return this.patch(o);
  };

  Store.prototype.subscribe = function (fn) {
    this._subs.push(fn);
    var self = this;
    return function unsubscribe() {     // idempotent
      var i = self._subs.indexOf(fn);
      if (i >= 0) self._subs.splice(i, 1);   // safe during an emit walk
    };
  };

  Store.prototype._emit = function (keys) {
    var payload = keys.slice();
    this._subs.slice().forEach(function (fn) { fn(payload); });
  };

  PlanUI.Store = Store;
  PlanUI.defaultUI = defaultUI;
  PlanUI.freshDefaults = freshDefaults;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
