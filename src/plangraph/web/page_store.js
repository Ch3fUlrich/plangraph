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
