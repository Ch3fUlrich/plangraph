/* plangraph page logic — pure, DOM-free, no I/O.
 *
 * Everything the page needs to derive from the snapshot lives here and is exposed
 * on `PlanGraph` (browser) and `module.exports` (node). `page_ui.js` draws it.
 * The rules mirror docs/SPEC.md:
 *   - ready   = state `open` and every dependency `done`
 *   - blocked = state `open` or `claimed` with at least one unmet dependency
 *   - critical path = longest remaining path by estimate_h over non-terminal items
 * A dependency is met only when the target item exists and is `done`; `cancelled`
 * never satisfies a dependency.
 */
(function (global) {
  "use strict";

  var TERMINAL = { done: true, cancelled: true };

  function normItems(items) {
    return Array.isArray(items) ? items.filter(Boolean) : [];
  }

  function indexByKey(items) {
    var m = {};
    normItems(items).forEach(function (it) {
      if (it.key) m[it.key] = it;
    });
    return m;
  }

  function depMet(depKey, idx) {
    var d = idx[depKey];
    return !!(d && d.state === "done");
  }

  function unmetDeps(item, idx) {
    return normItems(item && item.depends_on).filter(function (k) {
      return !depMet(k, idx);
    });
  }

  function displayState(item, idx) {
    var s = item.state;
    if (s === "done" || s === "cancelled" || s === "in_review") return s;
    if (unmetDeps(item, idx).length) return "blocked";
    if (s === "claimed") return "claimed";
    return "ready";
  }

  function computeDisplayState(items) {
    var idx = indexByKey(items);
    var out = {};
    normItems(items).forEach(function (it) {
      out[it.key] = displayState(it, idx);
    });
    return out;
  }

  function unblocksMap(items) {
    var m = {};
    normItems(items).forEach(function (it) {
      m[it.key] = 0;
    });
    normItems(items).forEach(function (it) {
      if (isTerminal(it)) return; // SPEC: count non-terminal waiters only
      normItems(it.depends_on).forEach(function (d) {
        if (m[d] !== undefined) m[d] += 1;
      });
    });
    return m;
  }

  function isTerminal(item) {
    return !!(item && TERMINAL[item.state]);
  }

  function estimate(item) {
    var v = Number(item && item.estimate_h);
    return isFinite(v) ? v : 0;
  }

  function byKeyAsc(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
  }

  /* Longest remaining path by estimate_h over non-terminal items, ending at a
   * goal when one is reachable, otherwise ending at the single longest chain.
   * Kahn's algorithm keeps it iterative and cycle-safe: nodes in a cycle are
   * simply left out rather than causing an infinite loop. */
  function criticalPath(items) {
    items = normItems(items);
    var nodes = items.filter(function (it) {
      return !isTerminal(it);
    });
    var nset = {};
    nodes.forEach(function (n) {
      nset[n.key] = n;
    });

    var indeg = {};
    var best = {};
    var prev = {};
    nodes.forEach(function (n) {
      indeg[n.key] = 0;
    });
    nodes.forEach(function (n) {
      normItems(n.depends_on).forEach(function (d) {
        if (nset[d]) indeg[n.key] += 1;
      });
    });

    var queue = [];
    nodes.forEach(function (n) {
      if (indeg[n.key] === 0) queue.push(n.key);
    });
    queue.sort(byKeyAsc);

    var order = [];
    while (queue.length) {
      var k = queue.shift();
      order.push(k);
      nodes.forEach(function (n) {
        if (nset[n.key] && normItems(n.depends_on).indexOf(k) >= 0) {
          indeg[n.key] -= 1;
          if (indeg[n.key] === 0) queue.push(n.key);
        }
      });
      queue.sort(byKeyAsc);
    }

    order.forEach(function (k) {
      var n = nset[k];
      var est = estimate(n);
      var total = est;
      var from = null;
      normItems(n.depends_on).forEach(function (d) {
        if (best[d] !== undefined) {
          var cand = best[d] + est;
          if (cand > total) {
            total = cand;
            from = d;
          }
        }
      });
      best[k] = total;
      prev[k] = from;
    });

    var target = null;
    var goalH = -Infinity;
    var anyH = -Infinity;
    var anyK = null;
    order.forEach(function (k) {
      if (best[k] > anyH) {
        anyH = best[k];
        anyK = k;
      }
      if (nset[k].kind === "goal" && best[k] > goalH) {
        goalH = best[k];
        target = k;
      }
    });
    if (target === null) target = anyK;

    var hours = target ? best[target] : 0;
    var keys = [];
    var guard = 0;
    while (target && guard <= order.length) {
      keys.unshift(target);
      target = prev[target];
      guard += 1;
    }
    return { keys: keys, hours: hours };
  }

  /* Percent done by estimate (default) or by item count. Cancelled items are
   * excluded from the denominator: they are not work left to do. */
  function percentDone(items, by) {
    by = by || "estimate";
    var total = 0;
    var done = 0;
    normItems(items).forEach(function (it) {
      if (it.state === "cancelled") return;
      var v = by === "count" ? 1 : estimate(it);
      total += v;
      if (it.state === "done") done += v;
    });
    return total > 0 ? done / total : 0;
  }

  function readyList(items) {
    var idx = indexByKey(items);
    var ub = unblocksMap(items);
    return normItems(items)
      .filter(function (it) {
        return it.state === "open" && unmetDeps(it, idx).length === 0;
      })
      .sort(function (a, b) {
        return (
          (b.priority || 0) - (a.priority || 0) ||
          (ub[b.key] || 0) - (ub[a.key] || 0) ||
          estimate(a) - estimate(b) ||
          byKeyAsc(a.key, b.key)
        );
      });
  }

  function blockedBy(items) {
    var idx = indexByKey(items);
    var out = {};
    normItems(items).forEach(function (it) {
      if (it.state === "open" || it.state === "claimed") {
        var u = unmetDeps(it, idx);
        if (u.length) out[it.key] = u;
      }
    });
    return out;
  }

  function inProgress(items) {
    return normItems(items).filter(function (it) {
      return it.state === "claimed" || it.state === "in_review";
    });
  }

  var PlanGraph = {
    indexByKey: indexByKey,
    unmetDeps: unmetDeps,
    displayState: displayState,
    computeDisplayState: computeDisplayState,
    unblocksMap: unblocksMap,
    isTerminal: isTerminal,
    estimate: estimate,
    criticalPath: criticalPath,
    percentDone: percentDone,
    readyList: readyList,
    blockedBy: blockedBy,
    inProgress: inProgress
  };

  global.PlanGraph = PlanGraph;
  if (typeof module !== "undefined" && module.exports) module.exports = PlanGraph;
})(typeof window !== "undefined" ? window : globalThis);
