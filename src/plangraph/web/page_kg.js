/* PlanUI.Knowledge — data + item-detail block for the Omnigraph knowledge export (PG-KG-UI, D-879).
 *
 * Data comes ONLY from the static sibling file `kg-export.json` (written by the render job). A missing,
 * disabled or malformed export keeps everything hidden and raises no error. Every string from the export is
 * length-capped and put in the DOM with createElement/textContent — never as HTML.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var REF = /^[a-z0-9][a-z0-9-]{0,62}:(Decision|Rule|Convention|Component|Preference|Project):[a-z0-9][a-z0-9._-]{0,127}$/;
  var MAX_NODES = 8000, MAX_ITEM_REFS = 40, MAX_REV = 200, FILE = "kg-export.json";
  var data = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function str(v, max) { return typeof v === "string" ? v.slice(0, max) : ""; }
  function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }
  function ref(v) { return typeof v === "string" && REF.test(v) ? v : ""; }
  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function list(v, cap, keep) {
    var out = [];
    if (Array.isArray(v)) for (var i = 0; i < v.length && out.length < cap; i++) { var x = keep(v[i]); if (x) out.push(x); }
    return out;
  }

  /* raw JSON -> a frozen-shape view; null unless enabled with a nodes object. */
  function normalize(raw) {
    if (!isObj(raw) || raw.enabled !== true || !isObj(raw.nodes)) return null;
    var d = { nodes: Object.create(null), items: Object.create(null), reverse: Object.create(null),
              projects: Object.create(null), graphs: Object.create(null), unresolved: Object.create(null),
              truncated: raw.truncated === true };
    Object.keys(raw.nodes).slice(0, MAX_NODES).forEach(function (k) {
      var n = raw.nodes[k];
      if (!ref(k) || !isObj(n)) return;
      d.nodes[k] = { ref: k, type: k.split(":")[1], title: str(n.title, 300) || k.split(":")[2],
        status: str(n.status, 20), severity: str(n.severity, 10), kind: str(n.kind, 60), excerpt: str(n.excerpt, 300),
        project: str(n.project, 100), superseded_by: ref(n.superseded_by), current: ref(n.current) || k };
    });
    if (isObj(raw.items)) Object.keys(raw.items).forEach(function (k) {
      var refs = list(raw.items[k], MAX_ITEM_REFS, ref);
      if (k.length <= 200 && refs.length) d.items[k] = refs;
    });
    if (isObj(raw.reverse)) Object.keys(raw.reverse).forEach(function (k) {
      if (ref(k)) d.reverse[k] = list(raw.reverse[k], MAX_REV, function (s) { return typeof s === "string" && s.length <= 200 ? s : ""; });
    });
    if (isObj(raw.projects)) Object.keys(raw.projects).forEach(function (k) {
      var p = raw.projects[k];
      if (k.length <= 100 && isObj(p)) d.projects[k] = list(p.nodes, MAX_NODES, ref);
    });
    list(raw.graphs, 50, function (g) { return typeof g === "string" ? g : ""; }).forEach(function (g) { d.graphs[g] = true; });
    list(raw.unresolved_graphs, 50, function (g) { return typeof g === "string" ? g : ""; }).forEach(function (g) { d.unresolved[g] = true; });
    return Object.keys(d.nodes).length || Object.keys(d.items).length ? d : null;   // nothing to show: hidden
  }

  function label(n) { return n.type === "Rule" ? n.severity : n.type === "Component" ? n.kind : n.status; }
  function isSuperseded(n) { return !!n.superseded_by || n.status === "superseded"; }

  /* One node as an <li>. opts.items: add the reverse index (node -> items) as item links. */
  function nodeRow(r, opts) {
    var n = data && data.nodes[r], li = el("li", "kg-node");
    if (!n) {
      var g = r.split(":")[0];
      var gone = !data.graphs[g] || data.unresolved[g];
      li.className = "kg-node " + (gone ? "kg-unavailable" : "kg-dangling");
      li.textContent = r + (gone ? " — unavailable (graph not readable)" : " — no such node");
      return li;
    }
    li.appendChild(el("span", "kg-type", n.type));
    li.appendChild(el("strong", "kg-title", n.title));
    if (label(n)) li.appendChild(el("span", "kg-status", label(n)));
    if (isSuperseded(n)) {
      var cur = n.current !== n.ref ? data.nodes[n.current] : null;
      li.appendChild(el("div", "kg-superseded", cur
        ? "superseded → " + cur.title + (label(cur) ? " (" + label(cur) + ")" : "")
        : "superseded (no successor in the export)"));
    }
    if (n.excerpt) li.appendChild(el("p", "kg-excerpt", n.excerpt));
    if (opts && opts.items) {
      var keys = data.reverse[r] || [];
      if (keys.length) {
        var row = el("div", "kg-items");
        row.appendChild(el("span", "muted", "used by"));
        keys.forEach(function (k) {
          var b = el("button", "keylink", k);
          b.type = "button";
          b.addEventListener("click", function () { if (global.PlanPage && global.PlanPage.focusNode) global.PlanPage.focusNode(k); });
          row.appendChild(b);
        });
        li.appendChild(row);
      }
    }
    return li;
  }

  /* The "Knowledge" block of one item, or null when there is nothing to show. */
  function block(key) {
    var refs = data && own(data.items, key) ? data.items[key] : null;
    if (!refs || !refs.length) return null;
    var box = el("section", "kg-block");
    box.appendChild(el("h3", null, "Knowledge"));
    var ul = el("ul", "kg-list");
    refs.forEach(function (r) { ul.appendChild(nodeRow(r)); });
    box.appendChild(ul);
    return box;
  }

  function enabled() { return !!data; }

  /* Apply new data: tab visibility, an open details panel, the open tab page. */
  function setData(raw) {
    try { data = normalize(raw); } catch (e) { data = null; }
    var tab = document.getElementById("tab-knowledge");
    if (tab) tab.hidden = !data;
    var P = PlanUI.Pages, PP = global.PlanPage;
    if (!data && P && P.current && P.current() === "knowledge") P.show("graph");
    if (PP && PP.renderDetails && PP.state && PP.state.selected) { try { PP.renderDetails(PP.state.selected); } catch (e) { /* panel not mounted */ } }
    if (data && P && P.current && P.current() === "knowledge" && PlanUI.KnowledgePage) PlanUI.KnowledgePage.render();
  }

  /* GET the sibling file with XMLHttpRequest (the page keeps its "no fetch(" invariant); any failure -> hidden. */
  function load() {
    if (typeof global.XMLHttpRequest !== "function") return Promise.resolve();
    return new Promise(function (resolve) {
      function done(raw) { setData(raw); resolve(); }
      try {
        var x = new global.XMLHttpRequest();
        x.open("GET", FILE);
        x.timeout = 10000;
        x.onload = function () {
          var j = null;
          if (x.status === 200) { try { j = JSON.parse(x.responseText); } catch (e) { j = null; } }
          done(j);
        };
        x.onerror = x.ontimeout = x.onabort = function () { done(null); };
        x.send();
      } catch (e) { done(null); }
    });
  }

  var tab0 = document.getElementById("tab-knowledge");
  if (tab0) tab0.hidden = true;                                  // until an export says otherwise

  PlanUI.Knowledge = { setData: setData, load: load, block: block, nodeRow: nodeRow, enabled: enabled,
    get: function () { return data; } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { load(); });
  else load();
})(typeof window !== "undefined" ? window : globalThis);
