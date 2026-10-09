/* PlanUI.KnowledgePage — the per-project Knowledge tab (PG-KG-UI, D-879): the knowledge nodes of one plan
 * project grouped by type, each with its status, a superseded marker and the reverse index (node -> items).
 * Data only from PlanUI.Knowledge (kg-export.json). DOM via createElement/textContent.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var cur = "";
  var ORDER = ["Decision", "Rule", "Component", "Convention", "Preference", "Project"];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function render() {
    var box = document.getElementById("sb-panel-knowledge");
    if (!box) return;
    clear(box);
    var K = PlanUI.Knowledge, d = K && K.get();
    if (!d) { box.appendChild(el("p", "sb-empty", "No knowledge export.")); return; }
    var names = Object.keys(d.projects).sort();
    if (!names.length) { box.appendChild(el("p", "sb-empty", "The export lists no project knowledge.")); return; }
    if (names.indexOf(cur) < 0) cur = names[0];

    var head = el("div", "kg-head");
    head.appendChild(el("h2", null, "Knowledge"));
    var sel = el("select", "kg-project");
    sel.setAttribute("aria-label", "Project");
    names.forEach(function (p) {
      var o = el("option", null, p);
      o.setAttribute("value", p);
      sel.appendChild(o);
    });
    sel.value = cur;
    sel.addEventListener("change", function () { cur = sel.value; render(); });
    head.appendChild(sel);
    box.appendChild(head);
    if (d.truncated) box.appendChild(el("p", "muted", "The export was truncated; not every node is listed."));

    var refs = d.projects[cur].filter(function (r) { return d.nodes[r]; });
    ORDER.forEach(function (type) {
      var group = refs.filter(function (r) { return d.nodes[r].type === type; });
      if (!group.length) return;
      group.sort(function (a, b) { return d.nodes[a].title.localeCompare(d.nodes[b].title); });
      box.appendChild(el("h3", null, type + "s (" + group.length + ")"));
      var ul = el("ul", "kg-list");
      group.forEach(function (r) { ul.appendChild(K.nodeRow(r, { items: true })); });
      box.appendChild(ul);
    });
  }

  PlanUI.KnowledgePage = { render: render };
})(typeof window !== "undefined" ? window : globalThis);
