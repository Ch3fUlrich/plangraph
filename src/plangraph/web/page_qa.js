/* PlanUI.QAPage — the Decisions & Q&A page: two dense tables with their
 * connections to task, phase and project, hover previews, and in-page answering.
 *
 * The page is a static snapshot, so an answer typed here is a LOCAL answer: it is
 * kept in localStorage, shown as answered, and can be exported as JSON for the
 * keeper to apply. Nothing is sent anywhere. DOM via createElement/textContent.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var KEY = "plangraph.qa.answers.v1";
  var status = "all", open = {};                 // status filter; question id -> editor open
  var sort = { dec: { col: "id", dir: "asc" }, qa: { col: "id", dir: "asc" } };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function nat(a, b) {
    return PlanUI.naturalCmp ? PlanUI.naturalCmp(String(a), String(b)) : String(a).localeCompare(String(b), undefined, { numeric: true });
  }

  /* ---- local answers ------------------------------------------------------- */
  function loadAnswers() {
    try { var o = JSON.parse(global.localStorage.getItem(KEY) || "{}"); return o && typeof o === "object" ? o : {}; }
    catch (e) { return {}; }
  }
  function saveAnswers(o) { try { global.localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) { /* storage blocked: kept in memory only */ } }
  var memory = null;
  function answers() { if (!memory) memory = loadAnswers(); return memory; }
  function setAnswer(id, text) {
    var a = answers();
    if (text) a[id] = { text: text, at: new Date().toISOString() }; else delete a[id];
    saveAnswers(a);
  }

  /* ---- connections ----------------------------------------------------------- */
  function ctxOf(vm, key) {
    var it = key && vm.items.get(key);
    return it ? { project: it.project, phase: it.phase, key: key, title: it.title } : { project: "", phase: "", key: key || "", title: "" };
  }
  function taskChip(vm, key) {
    var rec = vm.items.get(key);
    var b = el("button", "tl-item", String(key).split(":").pop());
    b.type = "button"; b.title = rec ? rec.title : "not in this snapshot";
    if (rec) b.addEventListener("click", function () { global.PlanPage.selectNode(key); }); else b.disabled = true;
    return b;
  }
  function projDot(vm, name) {
    var s = el("span", "qa-proj");
    var c = "#888"; (vm.projects || []).forEach(function (p) { if (p.name === name) c = p.color; });
    var d = el("span", "tc-dot"); d.style.setProperty("--pc", c); s.appendChild(d); s.appendChild(document.createTextNode(name || "—"));
    return s;
  }
  function uniq(a) { var o = {}, r = []; a.forEach(function (x) { if (x && !o[x]) { o[x] = 1; r.push(x); } }); return r; }

  function passes(vm, ctx, text) {
    var s = global.PlanPage.state;
    if (ctx.project && s.projectsOff && s.projectsOff[ctx.project]) return false;
    if (ctx.phase && s.tracksOff && s.tracksOff[ctx.phase]) return false;
    var ui = global.PlanUI.app && global.PlanUI.app.ui, q = ui ? String(ui.get("query") || "").trim().toLowerCase() : "";
    return !q || String(text).toLowerCase().indexOf(q) >= 0;
  }

  /* ---- table helper ------------------------------------------------------------ */
  function mkTable(id, cols, rows, skey, empty, rerender) {
    var wrap = el("div", "tg-scroll"), t = el("table", "tt qa-t"); t.id = id;
    var st = sort[skey], hr = el("tr");
    cols.forEach(function (c) {
      var th = el("th"), on = st.col === c.id;
      th.setAttribute("aria-sort", on ? (st.dir === "asc" ? "ascending" : "descending") : "none");
      var b = el("button", "th-btn" + (on ? " on" : ""), c.label + (on ? (st.dir === "asc" ? " ▲" : " ▼") : ""));
      b.type = "button";
      b.addEventListener("click", function () {
        if (st.col === c.id) st.dir = st.dir === "asc" ? "desc" : "asc"; else { st.col = c.id; st.dir = "asc"; }
        rerender();
      });
      th.appendChild(b); hr.appendChild(th);
    });
    var hd = el("thead"); hd.appendChild(hr); t.appendChild(hd);
    var sc = null; cols.forEach(function (c) { if (c.id === st.col) sc = c; });
    if (sc) rows.sort(function (a, b) { var r = nat(a.v[sc.id], b.v[sc.id]); return st.dir === "asc" ? r : -r; });
    var tb = el("tbody");
    if (!rows.length) { var tr0 = el("tr"), td0 = el("td", null, empty); td0.colSpan = cols.length; tr0.appendChild(td0); tb.appendChild(tr0); }
    rows.forEach(function (r) { tb.appendChild(r.tr); });
    t.appendChild(tb); wrap.appendChild(t);
    return wrap;
  }

  /* ---- decisions ----------------------------------------------------------------- */
  function decisionTable(vm, rerender) {
    var cols = [{ id: "id", label: "ID" }, { id: "title", label: "Decision" }, { id: "date", label: "Date" },
                { id: "project", label: "Project" }, { id: "phase", label: "Phase" }, { id: "tasks", label: "Tasks" }];
    var rows = [];
    (vm.decisions || []).forEach(function (d) {
      var ctxs = (d.items || []).map(function (k) { return ctxOf(vm, k); });
      var projects = uniq(ctxs.map(function (c) { return c.project; }).concat([d.project]));
      var phases = uniq(ctxs.map(function (c) { return c.phase; }));
      if (!passes(vm, { project: projects[0], phase: phases.length === 1 ? phases[0] : "" }, [d.id, d.title, d.text, projects.join(" ")].join(" "))) return;
      var tr = el("tr"); tr.id = "qa-row-" + d.id;
      var idc = el("td"), idb = el("button", "tl-chip tl-decision", d.id); idb.type = "button";
      PlanUI.Preview.attach(idb, { kind: "decision", id: d.id, title: d.title, text: d.text, date: d.date, project: d.project });
      idc.appendChild(idb); tr.appendChild(idc);
      tr.appendChild(el("td", "qa-text", d.title || d.id));
      tr.appendChild(el("td", "tc-when", d.date || "—"));
      var tp = el("td"); projects.forEach(function (p) { tp.appendChild(projDot(vm, p)); }); tr.appendChild(tp);
      tr.appendChild(el("td", "tc-phase", phases.join(", ") || "—"));
      var tt = el("td", "tc-links"); (d.items || []).forEach(function (k) { tt.appendChild(taskChip(vm, k)); });
      if (!(d.items || []).length) tt.textContent = "—"; tr.appendChild(tt);
      rows.push({ tr: tr, v: { id: d.id, title: d.title || "", date: d.date || "", project: projects.join(","), phase: phases.join(","), tasks: (d.items || []).length } });
    });
    return { node: mkTable("decisions-table", cols, rows, "dec", "No decisions match the current filters.", rerender), n: rows.length };
  }

  /* ---- questions -------------------------------------------------------------------- */
  function effective(q) {
    var a = answers()[q.id];
    if (a) return { status: "answered", answer: a.text, local: true, at: a.at };
    return { status: q.status, answer: q.answer, local: false };
  }

  function editor(q, rerender) {
    var box = el("div", "qa-edit");
    var ta = el("textarea", "qa-ta"); ta.rows = 3; ta.placeholder = "Your answer…"; ta.setAttribute("aria-label", "Answer to " + q.id);
    var cur = answers()[q.id]; ta.value = cur ? cur.text : (q.answer || "");
    var save = el("button", "tt-btn", "Save answer"); save.type = "button";
    save.addEventListener("click", function () { var v = ta.value.trim(); setAnswer(q.id, v); delete open[q.id]; rerender(); });
    var cancel = el("button", "tt-btn", "Cancel"); cancel.type = "button";
    cancel.addEventListener("click", function () { delete open[q.id]; rerender(); });
    box.appendChild(ta); box.appendChild(save); box.appendChild(cancel);
    if (cur) {
      var clr = el("button", "tt-btn", "Remove local answer"); clr.type = "button";
      clr.addEventListener("click", function () { setAnswer(q.id, ""); delete open[q.id]; rerender(); });
      box.appendChild(clr);
    }
    return box;
  }

  function questionTable(vm, rerender) {
    var cols = [{ id: "id", label: "ID" }, { id: "text", label: "Question" }, { id: "status", label: "Status" },
                { id: "project", label: "Project" }, { id: "phase", label: "Phase" }, { id: "task", label: "Task" },
                { id: "by", label: "Asked by" }, { id: "act", label: "Answer" }];
    var rows = [];
    (vm.questions || []).forEach(function (q) {
      var ef = effective(q), c = ctxOf(vm, q.item);
      if (status !== "all" && ef.status !== status) return;
      if (!passes(vm, { project: q.project || c.project, phase: c.phase }, [q.id, q.text, q.project, ef.answer || ""].join(" "))) return;
      var tr = el("tr"); tr.id = "qa-row-" + q.id;
      var idc = el("td"), idb = el("button", "tl-chip tl-question", q.id); idb.type = "button";
      PlanUI.Preview.attach(idb, { kind: "question", id: q.id, text: q.text, status: ef.status, answer: ef.answer, project: q.project, phase: c.phase, item: q.item });
      idc.appendChild(idb); tr.appendChild(idc);
      var tx = el("td", "qa-text"); tx.appendChild(el("div", null, q.text));
      if (ef.answer) tx.appendChild(el("div", "qa-ans", (ef.local ? "Local answer: " : "Answer: ") + ef.answer));
      if (open[q.id]) tx.appendChild(editor(q, rerender));
      tr.appendChild(tx);
      var ts = el("td"); ts.appendChild(el("span", "tc-badge b-" + (ef.status === "answered" ? "done" : ef.status === "cancelled" ? "cancelled" : "open"), ef.status + (ef.local ? " (local)" : "")));
      tr.appendChild(ts);
      var tp = el("td"); tp.appendChild(projDot(vm, q.project || c.project)); tr.appendChild(tp);
      tr.appendChild(el("td", "tc-phase", c.phase || "—"));
      var tk = el("td", "tc-links"); if (q.item) tk.appendChild(taskChip(vm, q.item)); else tk.textContent = "—"; tr.appendChild(tk);
      tr.appendChild(el("td", "tc-agent", q.asked_by || "—"));
      var ta = el("td"), ab = el("button", "tt-btn", ef.local ? "Edit" : ef.status === "answered" ? "Re-answer" : "Answer"); ab.type = "button";
      ab.addEventListener("click", function () { open[q.id] = !open[q.id]; rerender(); });
      ta.appendChild(ab); tr.appendChild(ta);
      rows.push({ tr: tr, v: { id: q.id, text: q.text || "", status: ef.status, project: q.project || c.project, phase: c.phase, task: q.item || "", by: q.asked_by || "", act: "" } });
    });
    return { node: mkTable("qa-table", cols, rows, "qa", "No questions match the current filters.", rerender), n: rows.length };
  }

  function exportBar(vm) {
    var a = answers(), ids = Object.keys(a), bar = el("div", "tt-bar qa-export");
    bar.appendChild(el("span", "tt-count", ids.length + " local answer" + (ids.length === 1 ? "" : "s") + " — this page is read-only; export them for the keeper"));
    function payload() {
      return JSON.stringify(ids.map(function (id) { return { question: id, answer: a[id].text, answered_at: a[id].at }; }), null, 2);
    }
    var cp = el("button", "tt-btn", "Copy as JSON"); cp.type = "button"; cp.disabled = !ids.length;
    cp.addEventListener("click", function () {
      try { global.navigator.clipboard.writeText(payload()); cp.textContent = "Copied"; } catch (e) { cp.textContent = "Copy failed"; }
    });
    var dl = el("button", "tt-btn", "Download JSON"); dl.type = "button"; dl.disabled = !ids.length;
    dl.addEventListener("click", function () {
      var u = URL.createObjectURL(new Blob([payload()], { type: "application/json" }));
      var l = el("a"); l.href = u; l.download = "plangraph-answers.json"; document.body.appendChild(l); l.click(); l.remove();
      setTimeout(function () { URL.revokeObjectURL(u); }, 1000);
    });
    bar.appendChild(cp); bar.appendChild(dl);
    return bar;
  }

  function render(vm) {
    vm = vm || (global.PlanUI.app && global.PlanUI.app.vm);
    var box = document.getElementById("sb-panel-qa");
    if (!box || !vm) return;
    clear(box);
    var again = function () { render(vm); };
    var d = decisionTable(vm, again);
    box.appendChild(el("h2", "pg-h", "Decisions (" + d.n + " of " + (vm.decisions || []).length + ")"));
    box.appendChild(d.node);
    var q = questionTable(vm, again);
    box.appendChild(el("h2", "pg-h", "Questions (" + q.n + " of " + (vm.questions || []).length + ")"));
    var seg = el("div", "seg"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Question status");
    [["all", "All"], ["open", "Open"], ["answered", "Answered"]].forEach(function (o) {
      var b = el("button", null, o[1]); b.type = "button"; b.setAttribute("aria-pressed", String(status === o[0]));
      b.addEventListener("click", function () { status = o[0]; again(); });
      seg.appendChild(b);
    });
    box.appendChild(seg);
    box.appendChild(exportBar(vm));
    box.appendChild(q.node);
    var ans = document.getElementById("sb-panel-answers");
    if (ans) clear(ans);
  }

  PlanUI.QAPage = { render: render, answers: answers, setAnswer: setAnswer };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
