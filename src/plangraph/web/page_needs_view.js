/* Needs-you DOM view; pure helpers live in page_needs.js. */
(function (global) {
  "use strict";
  if (typeof document === "undefined") return;

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  var NP = (PlanUI.NeedsPage = PlanUI.NeedsPage || {});
  var LABELS = { type: "Type", project: "Project", priority: "Prio", state: "State" };
  var FACETS = ["type", "project", "priority", "state"];
  var COLS = [["type", "Type"], ["id", "ID"], ["title", "Title"],
    ["project", "Project"], ["priority", "Prio"], ["state", "State"], ["age", "Age"]];
  var DEFF = { type: [], project: [], priority: [], state: [], age: "" };
  var CAP = 300;
  var f = clone(DEFF), sort = { col: "priority", dir: "desc" };
  var openF = {}, openA = "", showAll = false, init = false, rowId = "", flashed = false, vm = null;

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function td(tr, cls, label, node) {
    var d = el("td", cls);
    d.setAttribute("data-label", label);
    d.appendChild(node); tr.appendChild(d);
    return d;
  }

  function facetItems(all, key) {
    var map = new Map();
    all.forEach(function (r) {
      var v = r[key];
      if (v == null && key === "priority") return;
      v = v == null ? "" : String(v);
      map.set(v, (map.get(v) || 0) + 1);
    });
    return Array.from(map.keys()).sort(function (a, b) {
      return key === "priority" ? (Number(b) - Number(a) || 0) :
        PlanUI.naturalCmp ? PlanUI.naturalCmp(a, b) : a < b ? -1 : a > b ? 1 : 0;
    }).map(function (v) { return {v:v, n:map.get(v)}; });
  }
  function cb(text, checked, aria, apply) {
    var lb = el("label", "tt-fl"), b = el("input");
    b.type = "checkbox"; b.checked = !!checked;
    b.setAttribute("aria-label", aria);
    lb.appendChild(b); lb.appendChild(el("span", null, text));
    b.addEventListener("change", function () { apply(b.checked); refresh(); });
    return lb;
  }
  function facet(all, key) {
    var d = el("details", "tt-fd"), list = el("div", "tt-fdl"), sel = f[key];
    d.appendChild(el("summary", null, LABELS[key] + " (" + sel.length + ")"));
    facetItems(all, key).forEach(function (it) {
      var val = key === "priority" ? Number(it.v) : it.v;
      list.appendChild(cb(it.v + " (" + it.n + ")", sel.indexOf(val) >= 0,
        "Filter " + LABELS[key] + " " + it.v, function (on) {
          var i = sel.indexOf(val);
          if (on && i < 0) sel.push(val);
          if (!on && i >= 0) sel.splice(i, 1);
        }));
    });
    d.appendChild(list); d.open = !!openF[key];
    d.addEventListener("toggle", function () { openF[key] = d.open; });
    return d;
  }
  function bar(all) {
    var d = el("div", "tt-filt"), lab = el("label", "tt-fl"), sel = el("select", "tt-age"), cl;
    FACETS.forEach(function (key) { d.appendChild(facet(all, key)); });
    [["", "Any age"], ["1", "1 day or less"], ["7", "7 days or less"],
      ["30", "30 days or less"], ["30+", "Older than 30 days"]].forEach(function (o) {
      var op = el("option", null, o[1]);
      op.value = o[0]; sel.appendChild(op);
    });
    sel.value = f.age; sel.setAttribute("aria-label", "Filter by age");
    sel.addEventListener("change", function () { f.age = sel.value; refresh(); });
    lab.appendChild(el("span", null, "Age"));
    lab.appendChild(sel); d.appendChild(lab);
    cl = el("button", "tt-btn tt-clear", "Clear filters");
    cl.type = "button";
    cl.setAttribute("aria-label", "Clear all needs-you filters");
    cl.addEventListener("click", function () { f = clone(DEFF); refresh(); });
    d.appendChild(cl); return d;
  }

  function refresh() { showAll = false; commit(); }
  function commit() {
    var q = NP.encode ? NP.encode(f, sort, rowId) : "";
    try {
      var parts = [q].concat(String(global.location.search || "").slice(1).split("&")
        .filter(function (p) { return p && p.indexOf("n_") !== 0; })).filter(Boolean);
      var url = global.location.pathname + (parts.length ? "?" + parts.join("&") : "") +
        (global.location.hash || "");
      global.history.replaceState(null, "", url);
    } catch (e) { /* file:// may throw */ }
    render(vm);
  }

  function head() {
    var tr = el("tr"), th;
    COLS.forEach(function (c) {
      th = el("th", "tc-" + (c[0] === "id" ? "key" : c[0]));
      th.setAttribute("data-label", c[1]);
      th.setAttribute("aria-sort", sort.col === c[0] ?
        (sort.dir === "asc" ? "ascending" : "descending") : "none");
      var b = el("button", "th-btn", c[1] + (sort.col === c[0] ? (sort.dir === "asc" ? " \u25B2" : " \u25BC") : ""));
      b.type = "button";
      b.setAttribute("aria-label", "Sort by " + c[1]);
      b.addEventListener("click", function () {
        sort = sort.col === c[0] ? { col: c[0], dir: sort.dir === "asc" ? "desc" : "asc" }
          : { col: c[0], dir: (c[0] === "priority" || c[0] === "age") ? "desc" : "asc" };
        refresh();
      });
      th.appendChild(b); tr.appendChild(th);
    });
    th = el("th", null, "Open");
    th.setAttribute("data-label", "Open"); tr.appendChild(th);
    return tr;
  }
  function row(r) {
    var tr = el("tr");
    tr.id = "ny-row-" + r.id;
    var proj = (vm.projects || []).find(function (p) { return p.name === r.project; });
    if (proj) tr.style.setProperty("--pc", proj.color || "#888");

    td(tr, "tc-type", "Type", el("span", null,
      r.type.charAt(0).toUpperCase() + r.type.slice(1)));

    var a = el("a", "ny-id", r.id);
    a.href = (global.location ? global.location.pathname : "") + "?" +
      (NP.encode ? NP.encode(f, sort, r.id) : "") + "#pg=needs";
    if (r.type !== "task" && PlanUI.Preview && PlanUI.Preview.attach) {
      try {
        PlanUI.Preview.attach(a, r.type === "decision"
          ? { kind: "decision", id: r.id, title: r.title, text: r.ref.text, date: r.ref.date,
              project: r.project, items: r.ref.items }
          : { kind: "question", id: r.id, title: r.ref.text, text: r.ref.text,
              project: r.project, status: r.state, answer: r.ref.answer, item: r.ref.item });
      } catch (e) { /* preview optional */ }
    }
    td(tr, "tc-key", "ID", a);

    var tc = td(tr, "tc-title", "Title", el("span", "ny-tt", r.title || ""));
    if (r.ref && r.ref.kind === "operator_wait_unlinked") tc.appendChild(el("span", "tc-badge", "Operator wait unlinked"));
    var wk = r.type === "question" ? "answer" : r.type === "task" && r.ref ? r.ref.wait : "";
    if (wk) {
      var bd = el("span", "tc-badge", "Needs your " + (wk === "answer" ? "answer" : "task"));
      bd.style.background = "var(--wait-" + wk + ")";
      bd.style.color = "#fff";
      tc.appendChild(bd);
    }
    if (r.type === "question") {
      var ab = el("button", "tt-btn ny-ans", openA === r.id ? "Close" : "Answer");
      ab.type = "button";
      ab.addEventListener("click", function () { openA = openA === r.id ? "" : r.id; render(vm); });
      tc.appendChild(ab);
      if (openA === r.id && r.ref && PlanUI.QAPage && PlanUI.QAPage.editor) {
        var ed = PlanUI.QAPage.editor(r.ref, function () { openA = ""; render(vm); });
        if (ed) {
          var box = el("div", "ny-ed");
          box.appendChild(ed); tc.appendChild(box);
        }
      }
    }

    [["project", "Project", r.project || "-"],
     ["priority", "Prio", r.priority == null ? "-" : r.priority],
     ["state", "State", r.state || "-"],
     ["age", "Age", r.ageDays == null ? "-" : r.ageDays + "d"]].forEach(function (c) {
      td(tr, "tc-" + c[0], c[1], el("span", null, c[2]));
    });

    var key = r.key || "";
    var has = !!(key && vm.items && typeof vm.items.get === "function" && vm.items.get(key));
    var jb = el("button", "tt-btn ny-jump", "Jump");
    jb.type = "button"; jb.disabled = !has;
    jb.setAttribute("aria-label", "Jump to " + r.id);
    jb.addEventListener("click", function () {
      if (!has) return;
      if (PlanUI.app && PlanUI.app.jump) PlanUI.app.jump(key);
      else if (typeof PlanPage !== "undefined" && PlanPage.focusNode) PlanPage.focusNode(key);
    });
    td(tr, "tc-open", "Open", jb); return tr;
  }

  function flash() {
    if (flashed || !rowId) return;
    var tr = document.getElementById("ny-row-" + rowId);
    if (!tr) return;
    flashed = true;
    if (tr.scrollIntoView) tr.scrollIntoView({ block: "center" });
    tr.className += " flash";
    setTimeout(function () { tr.classList.remove("flash"); }, 1600);
  }

  function render(m) {
    vm = m || vm;
    var host = document.getElementById("sb-panel-needs");
    if (!host || !vm) return;
    if (!init) {
      init = true;
      var d = NP.decode ? NP.decode(global.location ? global.location.search : "") : null;
      if (d && d.f) f = d.f;
      if (d && d.sort) sort = d.sort;
      rowId = (d && d.row) || "";
    }
    var now = Date.now(), seen = new Map(), all = [];
    NP.rows(vm, now).forEach(function (r) {
      var key = r.key || r.id, ref = r.ref || {}, i = seen.get(key);
      if (r.type === "task") {
        r = Object.assign({}, r, {state: r.state || ref.state});
        if (key && i != null) {
          if (all[i].ref && all[i].ref.kind === "operator_wait_unlinked" && ref.kind !== "operator_wait_unlinked") all[i] = r;
          return;
        }
        if (key) seen.set(key, all.length);
      }
      all.push(r);
    });
    var list = NP.sort(NP.filter(all, f, now), sort);
    while (host.firstChild) host.removeChild(host.firstChild);
    host.appendChild(el("h2", "ny-h", "Needs you (" + list.length + " of " + all.length + ")"));
    var eb = PlanUI.QAPage && PlanUI.QAPage.exportBar && PlanUI.QAPage.exportBar(vm);
    if (eb) host.appendChild(eb);
    host.appendChild(bar(all));
    if (!list.length) {
      host.appendChild(el("p", "ny-empty", all.length ? "No rows match the current filters." : "Nothing needs you right now."));
      return;
    }
    var t = el("table", "tt ny-t"), thead = el("thead"), tb = el("tbody");
    thead.appendChild(head()); t.appendChild(thead);
    list.slice(0, showAll ? list.length : CAP).forEach(function (r) { tb.appendChild(row(r)); });
    t.appendChild(tb); host.appendChild(t);
    if (!showAll && list.length > CAP) {
      var more = el("button", "tt-btn ny-all", "Show all " + list.length + " rows");
      more.type = "button";
      more.onclick = function () { showAll = true; render(vm); };
      host.appendChild(more);
    }
    flash();
  }

  NP.render = render;
})(typeof window !== "undefined" ? window : globalThis);
