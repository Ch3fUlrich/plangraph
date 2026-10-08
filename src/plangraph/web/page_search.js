/* PlanUI query grammar — UI-PLAN §7 (`Query.parse/match`, edge match), T1.
 * Pure predicates over the view-model's Item records (§3): no DOM, no I/O,
 * ES5-safe, no modules — attaches to the shared `PlanUI` namespace.
 *
 * Grammar (§7 line 81):
 *   free words   → title,key,id,owner,doneCriteria,phase — case-insensitive
 *                  substring, AND across words;
 *   facets       → state:ready|blocked|claimed|in_review|done|cancelled,
 *                  owner:, project:, phase: (substring), op:1|0, loop:1|0,
 *                  prio:<op><n>, dep:<key> (item-local: does the item depend
 *                  on <key> — Router decision, §7's "depend on / unblock"
 *                  direction is resolved to depend-on only);
 *   edge facets  → edge:critical|blocking|cross (see edgeMatch below).
 * Unknown tokens degrade to free words (typed text never silently no-ops).
 * Facets combine with AND; an empty query matches everything.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var WORD_FIELDS = ["title", "key", "id", "owner", "doneCriteria", "phase"];
  var FACETS = { state: 1, owner: 1, project: 1, phase: 1, op: 1, loop: 1,
                 prio: 1, dep: 1, edge: 1 };
  /* single-entry memo: the harness loops match() over every item per query */
  var cacheQ = null, cacheTok = null;

  function lo(v) { return v == null ? "" : String(v).toLowerCase(); }

  function has(item, field, needle) {          // substring, both lowered
    return lo(item[field]).indexOf(needle) >= 0;
  }

  function facetFn(name, arg) {
    var a = lo(arg);
    var fn;
    if (name === "state") fn = function (i) { return i.display === arg; };
    if (name === "owner" || name === "project" || name === "phase") {
      var f = name;
      fn = function (i) { return has(i, f, a); };
    }
    if (name === "op") fn = function (i) { return arg === "1" ? !!i.operator : !i.operator; };
    if (name === "loop") fn = function (i) { return arg === "1" ? !!i.loop : !i.loop; };
    if (name === "dep") fn = function (i) { return i.deps.indexOf(arg) >= 0; };
    if (name === "prio") {
      var m = /^([<>]=?|=)?(-?\d+)$/.exec(arg);
      if (!m) return tag(name, arg, function () { return false; });
      var op = m[1] || "=", n = Number(m[2]);
      var test = op === ">" ? function (v) { return v > n; }
        : op === "<" ? function (v) { return v < n; }
        : op === ">=" ? function (v) { return v >= n; }
        : op === "<=" ? function (v) { return v <= n; }
        : function (v) { return v === n; };
      fn = function (i) { return test(Number(i.priority) || 0); };
    }
    if (name === "edge") {                     // node view of an edge facet
      if (arg === "critical") fn = function (i) { return i.critical; };
      else if (arg === "blocking") fn = function (i) { return i.display !== "done"; };
      else if (arg === "cross") fn = function (i) { return i.crossDeps.length > 0; };
      else fn = function () { return false; };
    }
    if (!fn) return tag(name, arg, function () { return false; });
    return tag(name, arg, fn);
  }

  /* Facet predicates remember name+arg so edgeMatch can branch (§7 line 82);
   * free-word predicates stay untagged. */
  function tag(name, arg, fn) {
    fn.facet = name;
    fn.arg = arg;
    return fn;
  }

  /* String → predicate list. Words keep their own predicate so they AND
   * across words but search every field for each word (§7). */
  function parse(q) {
    if (q === cacheQ) return cacheTok;
    var toks = String(q == null ? "" : q).trim().split(/\s+/).filter(Boolean);
    var out = toks.map(function (t) {
      var ci = t.indexOf(":");
      var name = ci > 0 ? t.slice(0, ci).toLowerCase() : "";
      if (FACETS[name]) return facetFn(name, t.slice(ci + 1));
      var word = lo(t);                        // unknown facet → literal word
      return function (i) { return WORD_FIELDS.some(function (f) { return has(i, f, word); }); };
    });
    cacheQ = q; cacheTok = out;
    return out;
  }

  /* Node match: every token passes (the "" query matches everything). */
  function match(item, q) {
    var toks = parse(q);
    for (var i = 0; i < toks.length; i++) {
      if (!toks[i](item)) return false;
    }
    return true;
  }

  /* Edge match (§7 line 82): `edge:` facets use the dedicated edge rules —
   * critical = both endpoints critical, blocking = source not done,
   * cross = endpoints in different projects — while the remaining node
   * facets and free words form ONE node predicate that matches when either
   * endpoint satisfies the WHOLE group (an edge stays visible iff one side
   * matches). `edge` shape: {source, target} of item keys. */
  function edgeMatch(edge, q, items) {
    var toks = parse(q);
    var s = items.get(edge.source), t = items.get(edge.target);
    if (!s || !t) return false;
    var nodeFns = [];
    for (var i = 0; i < toks.length; i++) {
      var fn = toks[i];
      if (fn.facet !== "edge") { nodeFns.push(fn); continue; }
      var ok = fn.arg === "critical" ? (s.critical && t.critical)
        : fn.arg === "blocking" ? (s.display !== "done")
        : fn.arg === "cross" ? (s.project !== t.project)
        : false;
      if (!ok) return false;
    }
    if (!nodeFns.length) return true;
    var pass = function (n) {
      return nodeFns.every(function (f) { return f(n); });
    };
    return pass(s) || pass(t);
  }

  PlanUI.Query = { parse: parse, match: match, edgeMatch: edgeMatch };

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
