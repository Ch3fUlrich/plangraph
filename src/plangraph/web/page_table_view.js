/* PlanUI table page — dense, groupable tables (one table per group).
 *
 * Columns: project, phase, task, state, agent, created/modified, previous,
 * next, decisions & questions. Any column can be the grouping key; every group
 * is its own table with a progress bar. Reads only `vm` (PlanUI.app.vm) and the
 * filter switches in PlanPage.state; builds DOM with createElement/textContent.
 * Also exports PlanUI.Preview, the hover/focus popup shared with the Q&A page.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var ROLE = { planning: "◇", implementation: "▭", review: "⬡" };
  var ROLE_LABEL = { planning: "Planning", implementation: "Implementation", review: "Review" };
  var MAX_ROWS = 300;                                   // per group, "show more" beyond
  var view = { group: "project", sort: { col: "task", dir: "asc" }, collapsed: {}, more: {} };
  var urlLoaded = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function PP() { return global.PlanPage; }
  function VM() { return global.PlanUI.app && global.PlanUI.app.vm; }
  function when(s) { return s ? String(s).replace("T", " ").slice(0, 16) : ""; }
  function day(s) { return s ? String(s).slice(0, 10) : "(none)"; }

  /* ---- preview popup (decisions / questions) -------------------------------- */
  var pop = null, popFor = null, shownAt = 0;
  function popup() {
    if (pop) return pop;
    pop = el("div", "pg-pop");
    pop.id = "pg-pop"; pop.setAttribute("role", "tooltip"); pop.hidden = true;
    document.body.appendChild(pop);
    return pop;
  }
  function hidePreview() { if (pop) { pop.hidden = true; popFor = null; } }
  function fill(p, rec) {
    while (p.firstChild) p.removeChild(p.firstChild);
    var isQ = rec.kind === "question";
    var h = el("div", "pg-pop-h");
    h.appendChild(el("span", "pg-pop-id", rec.id));
    h.appendChild(el("span", "pg-pop-kind", isQ ? "Question · " + rec.status : "Decision · " + (rec.date || "")));
    p.appendChild(h);
    if (rec.title) p.appendChild(el("div", "pg-pop-t", rec.title));
    var txt = String(rec.text || "").trim();
    p.appendChild(el("div", "pg-pop-x", txt ? (txt.length > 320 ? txt.slice(0, 320) + "…" : txt) : "(no text)"));
    if (isQ && rec.answer) p.appendChild(el("div", "pg-pop-a", "Answer: " + rec.answer));
    p.appendChild(el("div", "pg-pop-m", [rec.project, rec.phase, rec.item].filter(Boolean).join(" › ")));
  }
  function showPreview(anchor, rec) {
    var p = popup();
    fill(p, rec);
    p.hidden = false; popFor = anchor; shownAt = Date.now();
    var r = anchor.getBoundingClientRect(), w = p.offsetWidth, h = p.offsetHeight;
    var x = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    var y = r.bottom + 6; if (y + h > window.innerHeight - 8) y = Math.max(8, r.top - h - 6);
    p.style.left = x + "px"; p.style.top = y + "px";
  }
  function attachPreview(node, rec) {
    node.addEventListener("mouseenter", function () { showPreview(node, rec); });
    node.addEventListener("focus", function () { showPreview(node, rec); });
    node.addEventListener("mouseleave", hidePreview);
    node.addEventListener("blur", hidePreview);
  }
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hidePreview(); });
  window.addEventListener("scroll", function () { if (Date.now() - shownAt > 400) hidePreview(); }, true);

  /* decisions / questions per item key -> [{kind,id,...}] */
  function linkIndex(vm) {
    var idx = {};
    function add(k, r) { if (k) (idx[k] = idx[k] || []).push(r); }
    (vm.decisions || []).forEach(function (d) {
      var r = { kind: "decision", id: d.id, title: d.title, text: d.text, date: d.date, project: d.project };
      (d.items || []).forEach(function (k) { add(k, r); });
    });
    (vm.questions || []).forEach(function (q) {
      var it = vm.items.get(q.item);
      add(q.item, { kind: "question", id: q.id, text: q.text, status: q.status, answer: q.answer, project: q.project, phase: it ? it.phase : "", item: q.item });
    });
    return idx;
  }

  function goQA(rec) {
    if (PlanUI.Pages) PlanUI.Pages.show("qa");
    var n = document.getElementById("qa-row-" + rec.id);
    if (n) { n.scrollIntoView({ block: "center" }); n.classList.add("flash"); setTimeout(function () { n.classList.remove("flash"); }, 1600); }
  }
  function linkChip(rec) {
    var b = el("button", "tl-chip tl-" + rec.kind, rec.id);
    b.type = "button";
    b.setAttribute("aria-label", rec.kind + " " + rec.id);
    attachPreview(b, rec);
    b.addEventListener("click", function () { hidePreview(); goQA(rec); });
    return b;
  }

  /* ---- columns ------------------------------------------------------------ */
  function agentOf(r) { return r.claimedBy || r.workerRun || r.owner || ""; }
  function pick(r, which) { return which === "prev" ? (r.deps || []) : (r.unblocks || []); }

  var COLS = [
    { id: "project", label: "Project", val: function (r) { return r.project; } },
    { id: "phase", label: "Phase", val: function (r) { return r.phase; } },
    { id: "task", label: "Task", val: function (r) { return r.title; }, group: function (r) { return ROLE_LABEL[r.role] || r.role; } },
    { id: "state", label: "State", val: function (r) { return r.display || r.state; } },
    { id: "agent", label: "Agent", val: function (r) { return agentOf(r); }, group: function (r) { return agentOf(r) || "(unassigned)"; } },
    { id: "when", label: "Created / modified", val: function (r) { return r.updatedAt; }, group: function (r) { return day(r.updatedAt); } },
    { id: "prev", label: "Previous", val: function (r) { return pick(r, "prev").length; }, num: true, group: function (r) { var n = pick(r, "prev").length; return n ? "has previous" : "no previous"; } },
    { id: "next", label: "Next", val: function (r) { return pick(r, "next").length; }, num: true, group: function (r) { var n = pick(r, "next").length; return n ? "has next" : "no next"; } },
    { id: "links", label: "Decisions & questions", val: function (r) { return (r._links || []).length; }, num: true, group: function (r) { var n = (r._links || []).length; return n ? "has decisions/questions" : "none"; } },
    { id: "prio", label: "Prio", val: function (r) { return Number(r.priority) || 0; }, num: true },
    { id: "kind", label: "Kind", val: function (r) { return r.kind || ""; } },
    { id: "op", label: "Op", val: function (r) { return global.PlanUI.TableCtl && global.PlanUI.TableCtl.operatorNeeded(r) ? 1 : 0; }, num: true }
  ];
  var COL = {}; COLS.forEach(function (c) { COL[c.id] = c; });
  PlanUI.TABLE_PAGE_COLUMNS = COLS;

  function groupKey(col, r) {
    var c = COL[col];
    if (!c) return "";
    var v = c.group ? c.group(r) : c.val(r);
    return v === "" || v == null ? "(none)" : String(v);
  }

  function cmp(a, b) {
    var cmpN = PlanUI.naturalCmp || function (x, y) { return String(x).localeCompare(String(y), undefined, { numeric: true }); };
    var c = COL[view.sort.col] || COL.task, va = c.val(a), vb = c.val(b);
    var d = c.num ? (va - vb) : cmpN(String(va), String(vb));
    return view.sort.dir === "desc" ? -d : d;
  }

  function setSort(col, dir) {
    view.sort = { col: col, dir: dir };
    var t = global.PlanUI.TableCtl;
    if (t) { t.state.sort = { col: col, dir: dir }; t.save(); }
    render();
  }

  /* ---- cells ---------------------------------------------------------------- */
  function itemChip(key, vm) {
    var rec = vm.items.get(key);
    var b = el("button", "tl-item", key.split(":").pop());
    b.type = "button";
    b.title = rec ? rec.title + " (" + (rec.display || rec.state) + ")" : key;
    b.addEventListener("click", function () { if (rec) PP().selectNode(key); });
    return b;
  }
  function chipList(keys, vm) {
    var td = el("td", "tc-links");
    keys.slice(0, 2).forEach(function (k) { td.appendChild(itemChip(k, vm)); });
    if (keys.length > 2) {
      var more = el("span", "tl-more", "+" + (keys.length - 2));
      more.title = keys.slice(2).join(", ");
      td.appendChild(more);
    }
    return td;
  }

  function rowFor(r, vm, color) {
    var tr = el("tr", "tr state-" + (r.display || r.state));
    tr.style.setProperty("--pc", color || "#888");
    tr.setAttribute("data-key", r.key);
    tr.tabIndex = 0;
    tr.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target === tr) PP().selectNode(r.key); });
    if (PP().state.selected === r.key) tr.classList.add("sel");
    var tdP = el("td", "tc-proj"); tdP.appendChild(el("span", "tc-dot")); tdP.appendChild(el("span", null, r.project)); tr.appendChild(tdP);
    var tdPh = el("td", "tc-phase", r.phase); tdPh.title = String(r.phase || ""); tr.appendChild(tdPh);
    var tdT = el("td", "tc-task");
    var open = el("button", "tc-open");
    open.type = "button";
    open.title = r.key + " · " + (ROLE_LABEL[r.role] || r.role);
    open.appendChild(el("span", "tc-sym role-" + r.role, ROLE[r.role] || ROLE.implementation));
    open.appendChild(el("span", "tc-title", r.title));
    open.addEventListener("click", function () { PP().selectNode(r.key); });
    tdT.appendChild(open);
    var go = el("button", "tc-go", "⌖"); go.type = "button"; go.title = "Show in graph"; go.setAttribute("aria-label", "Show " + r.key + " in graph");
    go.addEventListener("click", function () { var a = global.PlanUI.app; if (a && a.jump) a.jump(r.key); });
    tdT.appendChild(go);
    tr.appendChild(tdT);
    var tdS = el("td", "tc-state"); tdS.appendChild(el("span", "tc-badge b-" + (r.display || r.state), (r.display || r.state).replace("_", " "))); tr.appendChild(tdS);
    var agT = agentOf(r) || "—", tdAg = el("td", "tc-agent", agT); tdAg.title = agT; tr.appendChild(tdAg);
    var tdW = el("td", "tc-when");
    var w1 = r.createdAt ? "c " + when(r.createdAt) : "c —", w2 = r.updatedAt ? "m " + when(r.updatedAt) : "m —";
    tdW.appendChild(el("div", null, w1));
    tdW.appendChild(el("div", null, w2));
    tdW.title = w1 + " / " + w2;
    tr.appendChild(tdW);
    tr.appendChild(chipList(pick(r, "prev"), vm));
    tr.appendChild(chipList(pick(r, "next"), vm));
    var tdL = el("td", "tc-links");
    (r._links || []).forEach(function (l) { tdL.appendChild(linkChip(l)); });
    tr.appendChild(tdL);
    tr.appendChild(el("td", null, r.priority == null ? "" : String(r.priority)));
    tr.appendChild(el("td", null, r.kind || "—"));
    tr.appendChild(el("td", null, global.PlanUI.TableCtl && global.PlanUI.TableCtl.operatorNeeded(r) ? "●" : ""));
    /* UI-TABLE-PHONE: below 600 px each row is a card; the cell label comes from data-label. */
    for (var ci = 0; ci < COLS.length && ci < tr.childNodes.length; ci++) {
      tr.childNodes[ci].setAttribute("data-label", COLS[ci].label);
      tr.childNodes[ci].setAttribute("data-col", COLS[ci].id);
    }
    return tr;
  }

  function tableFor(rows, vm, colorOf, gkey) {
    var wrap = el("div", "tg-scroll");
    var ctl = global.PlanUI.TableCtl, hc = "";
    if (ctl) Object.keys(ctl.state.hide).forEach(function (id) { if (ctl.state.hide[id]) hc += " hc-" + id; });
    var t = el("table", "tt" + hc);
    t.setAttribute("aria-label", gkey + " tasks");
    var hr = el("tr");
    COLS.forEach(function (c) {
      var th = el("th", "th-" + c.id);
      th.setAttribute("aria-sort", view.sort.col === c.id ? (view.sort.dir === "desc" ? "descending" : "ascending") : "none");
      var b = el("button", "th-btn" + (view.sort.col === c.id ? " on" : ""), c.label + (view.sort.col === c.id ? (view.sort.dir === "asc" ? " ▲" : " ▼") : ""));
      b.type = "button";
      b.setAttribute("aria-label", "Sort by " + c.label);
      b.addEventListener("click", function () {
        if (view.sort.col === c.id) setSort(c.id, view.sort.dir === "asc" ? "desc" : "asc");
        else setSort(c.id, "asc");
      });
      th.appendChild(b);
      hr.appendChild(th);
    });
    var hd = el("thead"); hd.appendChild(hr); t.appendChild(hd);
    var tb = el("tbody");
    var limit = view.more[gkey] ? rows.length : MAX_ROWS;
    rows.slice(0, limit).forEach(function (r) { tb.appendChild(rowFor(r, vm, colorOf(r))); });
    t.appendChild(tb);
    wrap.appendChild(t);
    if (rows.length > limit) {
      var m = el("button", "tg-more", "Show all " + rows.length + " rows"); m.type = "button";
      m.addEventListener("click", function () { view.more[gkey] = true; render(); });
      wrap.appendChild(m);
    }
    return wrap;
  }

  function progress(rows) {
    var done = 0, live = 0;
    rows.forEach(function (r) { if (r.state === "cancelled") return; live++; if (r.state === "done") done++; });
    return { done: done, total: live, pct: live ? Math.round(100 * done / live) : 0 };
  }

  /* ---- render ----------------------------------------------------------------- */
  function pass(r) {
    var s = PP().state;
    if (s.projectsOff && s.projectsOff[r.project]) return false;
    if (s.tracksOff && s.tracksOff[r.phase]) return false;
    if (global.PlanUI.TableCtl && !global.PlanUI.TableCtl.pass(r)) return false;
    var q = global.PlanUI.app && global.PlanUI.app.ui ? global.PlanUI.app.ui.get("query") : "";
    if (q && PlanUI.Query && PlanUI.Query.match) { try { return PlanUI.Query.match(r, q); } catch (e) { return true; } }
    return true;
  }

  function render() {
    var box = document.getElementById("list-content");
    var vm = VM();
    if (!box || !vm) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    var idx = linkIndex(vm), colors = {};
    (vm.projects || []).forEach(function (p) { colors[p.name] = p.color; });
    var all = [];
    vm.items.forEach(function (r) { r._links = idx[r.key] || []; all.push(r); });
    if (!urlLoaded && global.PlanUI.TableCtl) { urlLoaded = true; global.PlanUI.TableCtl.load(location.search); }
    if (global.PlanUI.TableCtl && global.PlanUI.TableCtl.state.sort && COL[global.PlanUI.TableCtl.state.sort.col]) {
      var ss = global.PlanUI.TableCtl.state.sort;
      view.sort = { col: ss.col, dir: ss.dir === "desc" ? "desc" : "asc" };
    }
    var rows = all.filter(pass);

    var bar = el("div", "tt-bar");
    var lab = el("label", "tt-grp", "Group by ");
    var sel = el("select", "tt-sel"); sel.setAttribute("aria-label", "Group table by column");
    var none = el("option", null, "No grouping"); none.value = ""; sel.appendChild(none);
    COLS.forEach(function (c) { var o = el("option", null, c.label); o.value = c.id; if (view.group === c.id) o.selected = true; sel.appendChild(o); });
    if (!view.group) none.selected = true;
    sel.addEventListener("change", function () { view.group = sel.value; render(); });
    lab.appendChild(sel); bar.appendChild(lab);
    /* Phone cards hide the header sort buttons, so sorting needs its own control (CSS shows it < 601 px). */
    var slab = el("label", "tt-sortlab", "Sort by "), ssel = el("select", "tt-sel tt-sort");
    ssel.setAttribute("aria-label", "Sort table by column");
    COLS.forEach(function (c) {
      [["asc", " ▲"], ["desc", " ▼"]].forEach(function (d) {
        var o = el("option", null, c.label + d[1]); o.value = c.id + ":" + d[0];
        if (view.sort.col === c.id && view.sort.dir === d[0]) o.selected = true;
        ssel.appendChild(o);
      });
    });
    ssel.addEventListener("change", function () { var v = ssel.value.split(":"); setSort(v[0], v[1] === "desc" ? "desc" : "asc"); });
    slab.appendChild(ssel); bar.appendChild(slab);
    var cnt = el("span", "tt-count", rows.length + " of " + all.length + " tasks");
    cnt.setAttribute("role", "status"); cnt.setAttribute("aria-live", "polite");
    bar.appendChild(cnt);
    var ca = el("button", "tt-btn", "Collapse all"); ca.type = "button";
    var ea = el("button", "tt-btn", "Expand all"); ea.type = "button";
    box.appendChild(bar);
    if (global.PlanUI.TableCtl) box.appendChild(global.PlanUI.TableCtl.bar(all, render));

    var groups = [], map = {};
    rows.sort(cmp);
    rows.forEach(function (r) {
      var k = view.group ? groupKey(view.group, r) : "All tasks";
      if (!map[k]) { map[k] = { key: k, rows: [] }; groups.push(map[k]); }
      map[k].rows.push(r);
    });
    var cn = PlanUI.naturalCmp || function (a, b) { return String(a).localeCompare(String(b), undefined, { numeric: true }); };
    if (view.group) groups.sort(function (a, b) { return cn(a.key, b.key); });
    ca.addEventListener("click", function () { groups.forEach(function (g) { view.collapsed[g.key] = true; }); render(); });
    ea.addEventListener("click", function () { view.collapsed = {}; render(); });
    if (view.group) { bar.appendChild(ca); bar.appendChild(ea); }

    if (!rows.length) { box.appendChild(el("p", "muted", all.length ? "No tasks match the current filters." : "This snapshot has no items.")); return; }
    groups.forEach(function (g) {
      var sec = el("section", "tg"), pr = progress(g.rows);
      var gc = view.group === "project" ? colors[g.key] : null;
      if (gc) sec.style.setProperty("--pc", gc);
      var hd = el("header", "tg-head");
      var tgl = el("button", "tg-toggle", view.collapsed[g.key] ? "▸" : "▾"); tgl.type = "button";
      tgl.setAttribute("aria-expanded", String(!view.collapsed[g.key]));
      tgl.setAttribute("aria-label", (view.collapsed[g.key] ? "Expand " : "Collapse ") + g.key);
      tgl.addEventListener("click", function () { view.collapsed[g.key] = !view.collapsed[g.key]; render(); });
      hd.appendChild(tgl);
      hd.appendChild(el("span", "tg-name", g.key));
      hd.appendChild(el("span", "tg-count", g.rows.length + " tasks"));
      var bar2 = el("span", "tg-bar"); bar2.setAttribute("role", "progressbar");
      bar2.setAttribute("aria-valuenow", String(pr.pct)); bar2.setAttribute("aria-valuemin", "0"); bar2.setAttribute("aria-valuemax", "100");
      bar2.setAttribute("aria-label", g.key + " progress");
      var fillb = el("i"); fillb.style.width = pr.pct + "%"; bar2.appendChild(fillb);
      hd.appendChild(bar2);
      hd.appendChild(el("span", "tg-pct", pr.done + "/" + pr.total + " · " + pr.pct + "%"));
      sec.appendChild(hd);
      if (!view.collapsed[g.key]) sec.appendChild(tableFor(g.rows, vm, function (r) { return colors[r.project]; }, g.key));
      box.appendChild(sec);
    });
  }

  PlanUI.Preview = { attach: attachPreview, show: showPreview, hide: hidePreview, linkIndex: linkIndex, goQA: goQA };
  PlanUI.TablePage = { render: render, view: view, columns: COLS, groupKey: groupKey, progress: progress };
  if (global.PlanPage) global.PlanPage.renderTable = render;
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
