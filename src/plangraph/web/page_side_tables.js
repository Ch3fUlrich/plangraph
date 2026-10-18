/* PlanUI.SideTables — sidebar tables + Needs/Q&A/Answers panels (W8 split).
 *
 * Moved verbatim from page_side.js (sortable table, keylink, renderNeeds,
 * renderQA, renderAnswers); shared state/helpers come from PlanUI._sideBase,
 * which page_side.js publishes (ES5/ES2015, no modules). Loaded right after
 * page_side.js; everything here runs only at call time (page_boot.js last).
 * DOM contracts are unchanged: decisions-table / qa-table ids, MAX_ROWS cap,
 * "+N more" overflow row, "No data in this snapshot" empty text. */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  /* Live base published by page_side.js (UI/VM set on mount, sortState/qaStatus mutate). */
  function B() { return PlanUI._sideBase; }

  /* ---- tiny sortable table ------------------------------------------------ */
  var MAX_ROWS = 300;  // per table, "+N more" beyond (matches page_table_view.js)
  function table(cols, rows, tkey, emptyMsg) {
    var base = B(), el = base.el, s = base.sortState[tkey];
    var t = el("table", "sb-table"); t.id = tkey + "-table";
    var tr = el("tr");
    cols.forEach(function (c) {
      var th = el("th", c.cls), b = el("button", null, c.label);
      th.setAttribute("aria-sort", s.col === c.col ? (s.dir === "asc" ? "ascending" : "descending") : "none");
      b.type = "button";
      b.addEventListener("click", function () {
        if (s.col === c.col) s.dir = s.dir === "asc" ? "desc" : "asc";
        else { s.col = c.col; s.dir = "asc"; }
        base.renderPanel(base.ui().get("sidebar").tab);
      });
      th.appendChild(b); tr.appendChild(th);
    });
    var thead = el("thead"); thead.appendChild(tr); t.appendChild(thead);
    var body = el("tbody"), idx = -1;
    cols.forEach(function (c, i) { if (c.col && c.col === s.col) idx = i; });
    if (!rows.length) {
      var td = el("td", null, emptyMsg); td.colSpan = cols.length;
      var er = el("tr"); er.appendChild(td); body.appendChild(er);
    }
    if (idx >= 0) rows.sort(function (a, b) {
      var r = base.natural(a.v[idx], b.v[idx]);
      if (!r && a.v[0] !== b.v[0]) r = base.natural(a.v[0], b.v[0]);
      return s.dir === "asc" ? r : -r;
    });
    var limit = Math.min(rows.length, MAX_ROWS);
    for (var i = 0; i < limit; i++) {
      var r = rows[i];
      var rr = el("tr");
      r.cells.forEach(function (c) {
        var td = el("td");
        if (c && c.nodeType) td.appendChild(c); else td.textContent = c == null ? "" : String(c);
        rr.appendChild(td);
      });
      body.appendChild(rr);
    }
    if (rows.length > MAX_ROWS) {
      var moreTr = el("tr");
      var moreTd = el("td", null, "+" + (rows.length - MAX_ROWS) + " more");
      moreTd.colSpan = cols.length;
      moreTd.style.textAlign = "center";
      moreTd.style.color = "var(--muted)";
      moreTr.appendChild(moreTd);
      body.appendChild(moreTr);
    }
    t.appendChild(body);
    return t;
  }

  function keylink(key) {
    var base = B(), el = base.el, VM = base.vm();
    var ok = !!(VM && VM.items.has(key));
    var b = el("button", "keylink", key);
    b.type = "button"; b.title = ok ? VM.humanize(key) : "not in this snapshot";
    if (ok) b.addEventListener("click", function () { global.PlanPage.focusNode(key); });
    else { b.disabled = true; b.className += " unknown"; }
    return b;
  }

  /* ---- Needs You ---------------------------------------------------------- */
  function renderNeeds() {
    if (global.PlanUI && global.PlanUI.NeedsPage) { global.PlanUI.NeedsPage.render(B().vm()); return; }
    var base = B(), el = base.el, VM = base.vm();
    var box = base.$("sb-panel-needs");
    if (!box) return;
    base.clear(box);
    var rows = (VM.needsYou || []).filter(base.pass);
    box.appendChild(el("h3", "sb-h", "Needs You (" + rows.length + ")"));
    if (!rows.length) {
      box.appendChild(el("p", "sb-empty", (VM.needsYou || []).length ? "No rows match the current search." : "Nothing needs you right now."));
      return;
    }
    rows.forEach(function (e) {
      var rec = base.shaped(e), isQ = e.text !== undefined;
      var row = el("div", "ny-row"), dot = el("span", "ny-dot");
      dot.style.background = base.projectColor(rec.project);
      row.appendChild(dot);
      var main = el("div", "ny-main");
      main.appendChild(el("span", "ny-title", isQ ? "Q: " + (e.text || "") : (rec.title || rec.key)));
      var meta = isQ
        ? base.join([e.id, e.project, "asked by " + (e.asked_by || "—"), e.item || ""])
        : base.join([rec.key, rec.display, "prio " + rec.priority,
                 rec.unblocks && rec.unblocks.length ? "unblocks " + rec.unblocks.length : ""], " · ");
      main.appendChild(el("span", "ny-meta", meta));
      var kind = isQ ? "answer" : e.wait;
      if (kind) {   // D-677
        var badge = el("span", "tc-badge", kind === "answer" ? "Needs your answer" : "Needs your task");
        badge.style.background = "var(--wait-" + kind + ")";
        badge.style.color = "#fff";
        main.appendChild(badge);
      }
      row.appendChild(main);
      var jump = el("button", "jump", "Jump");
      jump.type = "button";
      jump.setAttribute("aria-label", "Jump to " + (rec.key || ""));
      jump.addEventListener("click", function () { var a = global.PlanUI.app; (a && a.jump ? a.jump : global.PlanPage.focusNode)(rec.key); });
      row.appendChild(jump);
      box.appendChild(row);
    });
  }

  /* ---- Decisions & Q&A ---------------------------------------------------- */
  function renderQA() {
    var base = B(), el = base.el, VM = base.vm();
    if (global.PlanUI.QAPage && global.PlanUI.Preview) { global.PlanUI.QAPage.render(VM); return; }
    var box = base.$("sb-panel-qa");
    if (!box) return;
    base.clear(box);
    var dTotal = (VM.decisions || []).length;
    var ds = dTotal ? (VM.decisions || []).filter(base.pass) : [];
    box.appendChild(el("h3", "sb-h", base.count("Decisions", ds.length, dTotal)));
    if (!(VM.decisions || []).length) {
      box.appendChild(el("p", "sb-empty", "No data in this snapshot"));
    } else {
      var dcols = [{ label: "ID", col: "id", cls: "col-id" },
                   { label: "Title", col: "title" },
                   { label: "Project", col: "project", cls: "col-proj" },
                   { label: "Date", col: "date", cls: "col-date" },
                   { label: "Links", col: "links", cls: "col-links" }];
      box.appendChild(table(dcols, ds.map(function (d) {
        var links = (d.items || []).map(function (k) { return String(k).split(":").pop(); });
        return { v: [d.id, d.title, d.project, d.date, links.length],
                 cells: [d.id, d.title, d.project, d.date, links.length ? base.join(links) : "—"] };
      }), "decisions", "No rows match the current search."));
    }

    var qaStatus = base.qa();
    var qs = (VM.questions || []).filter(function (q) {
      return base.pass(q) && (qaStatus === "all" || base.qEff(q) === qaStatus);
    });
    var qTotal = (VM.questions || []).length;
    box.appendChild(el("h3", "sb-h", base.count("Q&A", qs.length, qTotal)));
    var seg = el("div", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Question status");
    [["all", "All"], ["open", "Open"], ["answered", "Answered"]].forEach(function (opt) {
      var b = el("button", null, opt[1]);
      b.type = "button";
      b.setAttribute("aria-pressed", String(qaStatus === opt[0]));
      b.addEventListener("click", function () { base.setQa(opt[0]); base.renderPanel("qa"); });
      seg.appendChild(b);
    });
    box.appendChild(seg);
    if (!(VM.questions || []).length) {
      box.appendChild(el("p", "sb-empty", "No data in this snapshot"));
    } else {
      var qcols = [{ label: "ID", col: "id", cls: "col-id" },
                   { label: "Question", col: "text" },
                   { label: "Project", col: "project", cls: "col-proj" },
                   { label: "Status", col: "status", cls: "col-status" },
                   { label: "Item", col: "item", cls: "col-links" }];
      box.appendChild(table(qcols, qs.map(function (q) {
        var es = base.qEff(q);
        var st = el("span", "badge badge-" + base.shapeQ(q).display, es);
        return { v: [q.id, q.text, q.project, es, q.item || ""],
                 cells: [q.id, q.text, q.project, st, q.item ? keylink(q.item) : "—"] };
      }), "qa", "No rows match the current search."));
    }
  }

  /* ---- Answers ------------------------------------------------------------ */
  function renderAnswers() {
    if (global.PlanUI.QAPage && global.PlanUI.Preview) return;      // answers live in the Q&A page
    var base = B(), el = base.el, VM = base.vm();
    var box = base.$("sb-panel-answers");
    if (!box) return;
    base.clear(box);
    if (!(VM.questions || []).length) {
      box.appendChild(el("p", "sb-empty", "No data in this snapshot"));
      return;
    }
    var rows = (VM.answers || []).filter(base.pass);
    box.appendChild(el("h3", "sb-h", "Answers (" + rows.length + ")"));
    if (!rows.length) {
      box.appendChild(el("p", "sb-empty", (VM.answers || []).length ? "No rows match the current search." : "No answered questions yet."));
      return;
    }
    rows.forEach(function (q) {
      var row = el("div", "ans-row");
      row.appendChild(el("span", "ans-q", base.join([q.id, q.project], " · ") + " — " + (q.text || "")));
      row.appendChild(el("span", "ans-a", q.answer || "(no answer text)"));
      if (q.item) row.appendChild(el("span", "ans-meta", "Item: " + q.item));
      box.appendChild(row);
    });
  }

  PlanUI.SideTables = { renderNeeds: renderNeeds, renderQA: renderQA, renderAnswers: renderAnswers };

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
