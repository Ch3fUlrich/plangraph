/* PlanUI.Sidebar — UI-PLAN §9 (T2): Needs You, Decisions & Q&A, Answers,
 * Settings, mounted into page_template.html's shell markup. Contract (§2): reads
 * ONLY `vm` (PlanUI.buildViewModel) and `ui` (PlanUI.Store) — never the raw
 * snapshot. `ui.query` filters every table through PlanUI.Query.match on
 * Item-shaped views (§7); sort state is ephemeral and local; all DOM is built
 * with createElement/textContent (no innerHTML with data). A9: `vm.needsYou`
 * (= operator-open + open-question) / questions / decisions / answers are the
 * counts; a snapshot without them says "No data in this snapshot" (§9 tail). */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var UI = null, VM = null;
  var qaStatus = "all";                      // "all" | "open" | "answered"
  var sortState = {                          // ephemeral, per table (§9)
    decisions: { col: "id", dir: "asc" },
    qa: { col: "id", dir: "asc" }
  };
  var checks = {};

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function join(arr, sep) { return (arr || []).join(sep || ", "); }

  /* Item-shaped view of a question so PlanUI.Query.match can run on it (§7). */
  function shapeQ(q) {
    var answered = q.status === "answered";
    var term = answered ? "done" : q.status === "cancelled" ? "cancelled" : "open";
    return {
      key: q.item || q.id, id: q.id, title: q.text || "", project: q.project || "",
      phase: "", owner: q.asked_by || "", kind: "question",
      state: term, display: term, operator: false, priority: 0, estimate_h: 0,
      deps: [], unblocks: [], critical: false, slack: 0, loop: null,
      doneCriteria: q.answer || null, evidence: [], reviewTier: "standard",
      updatedAt: "", crossDeps: []
    };
  }

  /* Item-shaped view of a decision (§9: id · title · project · date · items). */
  function shapeD(d) {                       // §9: id · title · project · date · items
    var s = shapeQ({ id: d.id, text: d.title, project: d.project });
    s.kind = "decision"; s.key = d.id;
    s.title = d.title || d.id; s.deps = d.items || [];
    return s;
  }

  /* needsYou mixes Item records with question objects (page_model). */
  function shaped(entry) { return entry.text !== undefined ? shapeQ(entry) : entry; }

  /* Current query, applied through the shared grammar (never a raw snapshot). */
  function pass(entry) {
    var q = UI ? UI.get("query") : "";
    return !q || PlanUI.Query.match(shaped(entry), q);
  }

  function natural(a, b) {
    if (typeof PlanUI.naturalCmp === "function") return PlanUI.naturalCmp(a, b);
    return String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0;
  }

  /* "Decisions (18)" keeps the TOTAL readable even when a search filters rows
   * (A9: sidebar shows 12 questions / 18 decisions / the needs-you count). */
  function count(label, shown, total) {
    if (shown === total) return label + " (" + total + ")";
    return label + " (" + shown + " of " + total + ")";
  }

  function projectColor(name) {
    var ps = VM ? VM.projects : [];
    for (var i = 0; i < ps.length; i++) if (ps[i].name === name) return ps[i].color;
    return "var(--muted)";
  }

  /* ---- tiny sortable table ------------------------------------------------ */
  function table(cols, rows, tkey, emptyMsg) {
    var t = el("table", "sb-table"), s = sortState[tkey]; t.id = tkey + "-table";
    var tr = el("tr");
    cols.forEach(function (c) {
      var th = el("th", c.cls), b = el("button", null, c.label);
      th.setAttribute("aria-sort", s.col === c.col ? (s.dir === "asc" ? "ascending" : "descending") : "none");
      b.type = "button";
      b.addEventListener("click", function () {
        if (s.col === c.col) s.dir = s.dir === "asc" ? "desc" : "asc";
        else { s.col = c.col; s.dir = "asc"; }
        renderPanel(UI.get("sidebar").tab);
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
      var r = natural(a.v[idx], b.v[idx]);
      if (!r && a.v[0] !== b.v[0]) r = natural(a.v[0], b.v[0]);
      return s.dir === "asc" ? r : -r;
    });
    rows.forEach(function (r) {
      var rr = el("tr");
      r.cells.forEach(function (c) {
        var td = el("td");
        if (c && c.nodeType) td.appendChild(c); else td.textContent = c == null ? "" : String(c);
        rr.appendChild(td);
      });
      body.appendChild(rr);
    });
    t.appendChild(body);
    return t;
  }

  function keylink(key) {
    var ok = !!(VM && VM.items.has(key));
    var b = el("button", "keylink", key);
    b.type = "button"; b.title = ok ? VM.humanize(key) : "not in this snapshot";
    if (ok) b.addEventListener("click", function () { global.PlanPage.focusNode(key); });
    else { b.disabled = true; b.className += " unknown"; }
    return b;
  }

  /* ---- Needs You ---------------------------------------------------------- */
  function renderNeeds() {
    var box = $("sb-panel-needs");
    if (!box) return;
    clear(box);
    var rows = (VM.needsYou || []).filter(pass);
    box.appendChild(el("h3", "sb-h", "Needs You (" + rows.length + ")"));
    if (!rows.length) {
      box.appendChild(el("p", "sb-empty", (VM.needsYou || []).length ? "No rows match the current search." : "Nothing needs you right now."));
      return;
    }
    rows.forEach(function (e) {
      var rec = shaped(e), isQ = e.text !== undefined;
      var row = el("div", "ny-row"), dot = el("span", "ny-dot");
      dot.style.background = projectColor(rec.project);
      row.appendChild(dot);
      var main = el("div", "ny-main");
      main.appendChild(el("span", "ny-title", isQ ? "Q: " + (e.text || "") : (rec.title || rec.key)));
      var meta = isQ
        ? join([e.id, e.project, "asked by " + (e.asked_by || "—"), e.item || ""])
        : join([rec.key, rec.display, "prio " + rec.priority,
                 rec.unblocks && rec.unblocks.length ? "unblocks " + rec.unblocks.length : ""], " · ");
      main.appendChild(el("span", "ny-meta", meta));
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
    var box = $("sb-panel-qa");
    if (!box) return;
    clear(box);
    var dTotal = (VM.decisions || []).length;
    var ds = dTotal ? (VM.decisions || []).filter(pass) : [];
    box.appendChild(el("h3", "sb-h", count("Decisions", ds.length, dTotal)));
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
                 cells: [d.id, d.title, d.project, d.date, links.length ? join(links) : "—"] };
      }), "decisions", "No rows match the current search."));
    }

    var qs = (VM.questions || []).filter(function (q) {
      return pass(q) && (qaStatus === "all" || q.status === qaStatus);
    });
    var qTotal = (VM.questions || []).length;
    box.appendChild(el("h3", "sb-h", count("Q&A", qs.length, qTotal)));
    var seg = el("div", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Question status");
    [["all", "All"], ["open", "Open"], ["answered", "Answered"]].forEach(function (opt) {
      var b = el("button", null, opt[1]);
      b.type = "button";
      b.setAttribute("aria-pressed", String(qaStatus === opt[0]));
      b.addEventListener("click", function () { qaStatus = opt[0]; renderPanel("qa"); });
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
        var st = el("span", "badge badge-" + shapeQ(q).display, q.status);
        return { v: [q.id, q.text, q.project, q.status, q.item || ""],
                 cells: [q.id, q.text, q.project, st, q.item ? keylink(q.item) : "—"] };
      }), "qa", "No rows match the current search."));
    }
  }

  /* ---- Answers ------------------------------------------------------------ */
  function renderAnswers() {
    var box = $("sb-panel-answers");
    if (!box) return;
    clear(box);
    if (!(VM.questions || []).length) {
      box.appendChild(el("p", "sb-empty", "No data in this snapshot"));
      return;
    }
    var rows = (VM.answers || []).filter(pass);
    box.appendChild(el("h3", "sb-h", "Answers (" + rows.length + ")"));
    if (!rows.length) {
      box.appendChild(el("p", "sb-empty", (VM.answers || []).length ? "No rows match the current search." : "No answered questions yet."));
      return;
    }
    rows.forEach(function (q) {
      var row = el("div", "ans-row");
      row.appendChild(el("span", "ans-q", join([q.id, q.project], " · ") + " — " + (q.text || "")));
      row.appendChild(el("span", "ans-a", q.answer || "(no answer text)"));
      if (q.item) row.appendChild(el("span", "ans-meta", "Item: " + q.item));
      box.appendChild(row);
    });
  }

  /* ---- Settings ----------------------------------------------------------- */
  function seg(opts, current, onPick, label) {
    var s = el("div", "seg");
    s.setAttribute("role", "group");
    s.setAttribute("aria-label", label);
    opts.forEach(function (o) {
      var b = el("button", null, o[1]);
      b.type = "button";
      b.id = "set-" + label.replace(/\W+/g, "-").toLowerCase() + "-" + o[0];
      b.setAttribute("aria-pressed", String(current === o[0]));
      b.addEventListener("click", function () { onPick(o[0]); });
      s.appendChild(b);
    });
    return s;
  }

  function block(title, node) {
    var b = el("div", "set-block");
    b.appendChild(el("h4", "sb-h", title));
    b.appendChild(node);
    return b;
  }

  function renderSettings() {
    var box = $("sb-panel-settings");
    if (!box) return;
    clear(box);
    var s = UI.get("settings");
    box.appendChild(el("h3", "sb-h", "Settings"));
    box.appendChild(block("Text size (root px)", seg([["S", "S"], ["M", "M"], ["L", "L"]],
      s.textSize, function (v) { pick("textSize", v); }, "Text size")));
    box.appendChild(block("Theme", seg([["auto", "Auto"], ["light", "Light"], ["dark", "Dark"]],
      s.theme, function (v) { pick("theme", v); }, "Theme")));
    [["showCritical", "Show critical path"],
     ["showCross", "Show cross-project stubs"],
     ["autoCollapseDone", "Auto-collapse done phases"],
     ["animate", "Animate layout"],
     ["showAllEdges", "Show all edges"]].forEach(function (pair) {
      var row = el("div", "set-row");
      var id = "set-" + pair[0];
      var label = el("label", null, pair[1]);
      label.htmlFor = id;
      var cb = el("input");
      cb.type = "checkbox";
      cb.id = id;
      cb.checked = !!s[pair[0]];
      cb.addEventListener("change", function () { pick(pair[0], cb.checked); });
      row.appendChild(label);
      row.appendChild(cb);
      box.appendChild(row);
      checks[pair[0]] = cb;
    });
  }

  /* One store write per setting (page_boot persists + applies it). */
  function pick(key, value) {
    var cur = UI.get("settings");
    if (cur[key] === value) return;
    var next = {};
    Object.keys(cur).forEach(function (k) { next[k] = cur[k]; });
    next[key] = value;
    UI.set("settings", next);
  }

  function syncSettings() {
    var s = UI.get("settings");
    var boxes = $("sb-panel-settings");
    if (!boxes) return;
    Array.prototype.forEach.call(boxes.querySelectorAll(".seg[aria-label='Text size'] button"), function (b, i) {
      b.setAttribute("aria-pressed", String(s.textSize === ["S", "M", "L"][i]));
    });
    Array.prototype.forEach.call(boxes.querySelectorAll(".seg[aria-label='Theme'] button"), function (b, i) {
      b.setAttribute("aria-pressed", String(s.theme === ["auto", "light", "dark"][i]));
    });
    Object.keys(checks).forEach(function (k) { checks[k].checked = !!s[k]; });
  }

  function setAttributes() {
    var sb = UI.get("sidebar");
    Array.prototype.forEach.call(document.querySelectorAll(".sb-tab"), function (t) {
      var on = t.getAttribute("data-tab") === sb.tab;
      t.setAttribute("aria-selected", String(on));
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sb-panel"), function (p) {
      p.hidden = p.getAttribute("data-panel") !== sb.tab;
    });
  }

  function renderCounts() {
    function set(id, n) {
      var node = $(id);
      if (!node) return;
      node.textContent = String(n);
      node.hidden = !n;
    }
    var nQ = (VM.questions || []).length;
    var nD = (VM.decisions || []).length;
    var nN = (VM.needsYou || []).length;
    set("sb-count-needs", nN);                     // A9: operator-open + open-question
    set("sb-count-qa", nQ);                        // A9: 12 questions (fixture)
    set("sb-count-answers", (VM.answers || []).length);
    set("inbox-badge", nN);                        // phone Inbox tab (§10)
    /* Both A9 numbers are readable without opening a panel. */
    Array.prototype.forEach.call(document.querySelectorAll(".sb-tab"), function (t) {
      if (t.getAttribute("data-tab") === "qa") {
        t.setAttribute("title", "Decisions & Q&A: " + nD + " decisions, " + nQ + " questions");
        t.setAttribute("aria-label", "Decisions & Q&A, " + nD + " decisions and " + nQ + " questions");
      }
    });
  }

  function renderPanel(tab) {
    if (tab === "qa") renderQA();
    else if (tab === "answers") renderAnswers();
    else if (tab === "settings") renderSettings();
    else renderNeeds();
  }

  function renderAll() {
    renderCounts();
    renderNeeds();
    renderQA();
    renderAnswers();
    renderSettings();
    syncSettings();
    setAttributes();
  }

  /* mount(ui, vm): draw the chrome and follow the store from here on. */
  function mount(ui, vm) {
    UI = ui;
    VM = vm;
    renderAll();
    Array.prototype.forEach.call(document.querySelectorAll(".sb-tab"), function (t) {
      t.addEventListener("click", function () {
        var tab = t.getAttribute("data-tab");
        UI.set("sidebar", { open: true, tab: tab });
        renderPanel(tab);
        setAttributes();
      });
    });
    UI.subscribe(function (keys) {
      if (keys.indexOf("query") >= 0) renderAll();
      if (keys.indexOf("settings") >= 0) syncSettings();
      if (keys.indexOf("sidebar") >= 0) {
        setAttributes();
        renderPanel(UI.get("sidebar").tab);
      }
    });
  }

  PlanUI.Sidebar = { mount: mount, sync: syncSettings, render: renderAll };

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
