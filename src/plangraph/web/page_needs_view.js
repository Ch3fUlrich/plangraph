/* plangraph — "Needs you" view (item A2): render() only.
 * Pure rows/filter/sort/encode/decode/FACETS come from page_needs.js.
 * ES5, createElement/textContent only; no-op without a document.
 */
(function (global) {
  "use strict";
  if (typeof document === "undefined") return;

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  var NP = (PlanUI.NeedsPage = PlanUI.NeedsPage || {});
  var LABELS = { type: "Type", project: "Project", priority: "Prio", state: "State" };
  var FACETS = ["type", "project", "priority", "state"];
  var COLS = [["type", "Type", "tc-type"], ["id", "ID", "tc-key"], ["title", "Title", "tc-title"],
    ["project", "Project", "tc-project"], ["priority", "Prio", "tc-priority"],
    ["state", "State", "tc-state"], ["age", "Age", "tc-age"]];
  var DEFF = { type: [], project: [], priority: [], state: [], age: "" };
  var CAP = 300;
  var f = clone(DEFF), sort = { col: "priority", dir: "desc" };
  var openF = {}, openA = "", showAll = false, init = false, rowId = "", flashed = false, vm = null;

  function clone(o) {
    var c = {}, k;
    for (k in o) if (o.hasOwnProperty(k)) c[k] = Array.isArray(o[k]) ? o[k].slice() : o[k];
    return c;
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function td(tr, cls, label, node) {
    var d = el("td", cls);
    d.setAttribute("data-label", label);
    d.appendChild(node);
    tr.appendChild(d);
    return d;
  }

  /* ---- filter bar ------------------------------------------------------- */
  function facetItems(all, key) {
    var map = {}, keys = [], out = [], i, r, v;
    for (i = 0; i < all.length; i++) {
      r = all[i];
      if (key === "priority") {
        if (r.priority === null || r.priority === undefined) continue;
        v = String(r.priority);
      } else v = String(r[key] === null || r[key] === undefined ? "" : r[key]);
      if (!map[v]) { map[v] = { v: v, n: 0 }; keys.push(v); }
      map[v].n++;
    }
    keys.sort(function (a, b) {
      if (key === "priority") return (Number(b) || 0) - (Number(a) || 0);
      if (PlanUI.naturalCmp) return PlanUI.naturalCmp(a, b);
      return a < b ? -1 : a > b ? 1 : 0;
    });
    for (i = 0; i < keys.length; i++) out.push(map[keys[i]]);
    return out;
  }
  function cb(text, checked, aria, apply) {
    var lb = el("label", "tt-fl"), b = el("input");
    b.type = "checkbox";
    b.checked = !!checked;
    b.setAttribute("aria-label", aria);
    lb.appendChild(b);
    lb.appendChild(el("span", null, text));
    b.addEventListener("change", function () {
      apply(b.checked);
      showAll = false;
      commit();
    });
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
    d.appendChild(list);
    d.open = !!openF[key];
    d.addEventListener("toggle", function () { openF[key] = d.open; });
    return d;
  }
  function bar(all) {
    var d = el("div", "tt-filt"), lab = el("label", "tt-fl"), sel = el("select", "tt-age"), cl;
    FACETS.forEach(function (key) { d.appendChild(facet(all, key)); });
    [["", "Any age"], ["1", "1 day or less"], ["7", "7 days or less"],
      ["30", "30 days or less"], ["30+", "Older than 30 days"]].forEach(function (o) {
      var op = el("option", null, o[1]);
      op.value = o[0];
      sel.appendChild(op);
    });
    sel.value = f.age;
    sel.setAttribute("aria-label", "Filter by age");
    sel.addEventListener("change", function () {
      f.age = sel.value;
      showAll = false;
      commit();
    });
    lab.appendChild(el("span", null, "Age"));
    lab.appendChild(sel);
    d.appendChild(lab);
    cl = el("button", "tt-btn tt-clear", "Clear filters");
    cl.type = "button";
    cl.setAttribute("aria-label", "Clear all needs-you filters");
    cl.addEventListener("click", function () {
      f = clone(DEFF);
      showAll = false;
      commit();
    });
    d.appendChild(cl);
    return d;
  }

  /* ---- URL -------------------------------------------------------------- */
  function commit() {
    var q = NP.encode ? NP.encode(f, sort, rowId) : "", keep = [], s, i, k;
    try {
      s = String(global.location.search || "").slice(1).split("&");
      for (i = 0; i < s.length; i++) {
        k = s[i] ? s[i].split("=")[0] : "";
        if (s[i] && k.indexOf("n_") !== 0) keep.push(s[i]);
      }
      var parts = [q].concat(keep).filter(function (p) { return !!p; });
      var url = global.location.pathname + (parts.length ? "?" + parts.join("&") : "") +
        (global.location.hash || "");
      global.history.replaceState(null, "", url);
    } catch (e) { /* file:// may throw */ }
    render(vm);
  }

  /* ---- table ------------------------------------------------------------ */
  function head() {
    var tr = el("tr"), th;
    COLS.forEach(function (c) {
      th = el("th");
      th.setAttribute("data-label", c[1]);
      th.setAttribute("aria-sort", sort.col === c[0] ?
        (sort.dir === "asc" ? "ascending" : "descending") : "none");
      var b = el("button", "th-btn", c[1] + (sort.col === c[0] ? (sort.dir === "asc" ? " \u25B2" : " \u25BC") : ""));
      b.type = "button";
      b.setAttribute("aria-label", "Sort by " + c[1]);
      b.addEventListener("click", function () {
        sort = sort.col === c[0] ? { col: c[0], dir: sort.dir === "asc" ? "desc" : "asc" }
          : { col: c[0], dir: (c[0] === "priority" || c[0] === "age") ? "desc" : "asc" };
        showAll = false;
        commit();
      });
      th.appendChild(b);
      tr.appendChild(th);
    });
    th = el("th");
    th.setAttribute("data-label", "Open");
    th.textContent = "Open";
    tr.appendChild(th);
    return tr;
  }
  function row(r) {
    var tr = el("tr"), i, proj = null;
    tr.id = "ny-row-" + r.id;
    if (vm && vm.projects) for (i = 0; i < vm.projects.length; i++)
      if (vm.projects[i].name === r.project) proj = vm.projects[i];
    if (proj) { try { tr.style.setProperty("--pc", proj.color || "#888"); } catch (e) { /* ignore */ } }

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
    var wk = r.type === "question" ? "answer" : (r.type === "task" ? r.ref && r.ref.wait : "");
    if (wk) {   // D-677
      var bd = el("span", "tc-badge", wk === "answer" ? "Needs your answer" : "Needs your task");
      bd.style.background = "var(--wait-" + wk + ")";
      bd.style.color = "#fff";
      tc.appendChild(bd);
    }
    if (r.type === "question") {
      var ab = el("button", "tt-btn ny-ans", openA === r.id ? "Close" : "Answer");
      ab.type = "button";
      ab.addEventListener("click", function () {
        openA = (openA === r.id) ? "" : r.id;
        render(vm);
      });
      tc.appendChild(ab);
      if (openA === r.id && PlanUI.QAPage && PlanUI.QAPage.editor) {
        var ed = PlanUI.QAPage.editor(r.ref, function () { openA = ""; render(vm); });
        if (ed) {
          var box = el("div", "ny-ed");
          box.appendChild(ed);
          tc.appendChild(box);
        }
      }
    }

    td(tr, "tc-project", "Project", el("span", null, r.project || "-"));
    td(tr, "tc-priority", "Prio", el("span", null,
      r.priority === null || r.priority === undefined ? "-" : r.priority));
    td(tr, "tc-state", "State", el("span", null, r.state || "-"));
    td(tr, "tc-age", "Age", el("span", null,
      r.ageDays === null || r.ageDays === undefined ? "-" : r.ageDays + "d"));

    var key = r.key || "";
    var has = !!(key && vm && vm.items && vm.items.get && vm.items.get(key));
    var jb = el("button", "tt-btn ny-jump", "Jump");
    jb.type = "button";
    jb.disabled = !has;
    jb.setAttribute("aria-label", "Jump to " + (key || r.id));
    jb.addEventListener("click", function () {
      if (!has) return;
      if (PlanUI.app && PlanUI.app.jump) PlanUI.app.jump(key);
      else if (typeof PlanPage !== "undefined" && PlanPage.focusNode) PlanPage.focusNode(key);
    });
    td(tr, "tc-open", "Open", jb);
    return tr;
  }

  function flash() {
    if (flashed || !rowId) return;
    var tr = document.getElementById("ny-row-" + rowId);
    if (!tr) return;
    flashed = true;
    try { tr.scrollIntoView({ block: "center" }); } catch (e) { if (tr.scrollIntoView) tr.scrollIntoView(); }
    tr.className += " flash";
    setTimeout(function () {
      var n = document.getElementById("ny-row-" + rowId);
      if (n) n.className = n.className.replace(/\s*\bflash\b/g, "");
    }, 1600);
  }

  /* ---- render ----------------------------------------------------------- */
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
    var now = Date.now();
    var all = NP.rows(vm, now);
    var list = NP.sort(NP.filter(all, f, now), sort);
    while (host.firstChild) host.removeChild(host.firstChild);
    host.appendChild(el("h2", "ny-h", "Needs you (" + list.length + " of " + all.length + ")"));
    if (PlanUI.QAPage && PlanUI.QAPage.exportBar) {
      var eb = PlanUI.QAPage.exportBar(vm);
      if (eb) host.appendChild(eb);
    }
    host.appendChild(bar(all));
    if (!all.length) { host.appendChild(el("p", "ny-empty", "Nothing needs you right now.")); return; }
    if (!list.length) { host.appendChild(el("p", "ny-empty", "No rows match the current filters.")); return; }
    var t = el("table", "tt ny-t"), thead = el("thead"), tb = el("tbody");
    thead.appendChild(head());
    t.appendChild(thead);
    list.slice(0, showAll ? list.length : CAP).forEach(function (r) { tb.appendChild(row(r)); });
    t.appendChild(tb);
    host.appendChild(t);
    if (!showAll && list.length > CAP) {
      var more = el("button", "tt-btn ny-all", "Show all " + list.length + " rows");
      more.type = "button";
      more.addEventListener("click", function () { showAll = true; render(vm); });
      host.appendChild(more);
    }
    flash();
  }

  NP.render = render;
})(typeof window !== "undefined" ? window : globalThis);
