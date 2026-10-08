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
  var MAX_ROWS = 300;  // per table, "+N more" beyond (matches page_table_view.js)
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
    var ok = !!(VM && VM.items.has(key));
    var b = el("button", "keylink", key);
    b.type = "button"; b.title = ok ? VM.humanize(key) : "not in this snapshot";
    if (ok) b.addEventListener("click", function () { global.PlanPage.focusNode(key); });
    else { b.disabled = true; b.className += " unknown"; }
    return b;
  }

  /* ---- Projects: project > phase > task tree (filter + navigation) -------- */
  var ROLE = {                                   // same glyph set everywhere
    planning: { sym: "\u25C7", label: "Planning" },
    implementation: { sym: "\u25AD", label: "Implementation" },
    review: { sym: "\u2B21", label: "Review" }
  };
  var expanded = {};                             // "p" or "p|phase" -> true

  function PP() { return global.PlanPage; }

  function eyeBtn(kind, name, label) {
    var on = PP().isFilterOn ? PP().isFilterOn(kind, name) : true;
    var b = el("button", "pt-eye" + (on ? " on" : ""), on ? "\u2713" : "");
    b.type = "button";
    b.title = (on ? "Hide " : "Show ") + label + " in the graph";
    b.setAttribute("aria-label", b.title);
    b.setAttribute("aria-pressed", String(on));
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      if (PP().toggleFilter) PP().toggleFilter(kind, name);
    });
    return b;
  }

  function rowHead(open, hasKids) {
    var c = el("span", "pt-chev", hasKids ? (open ? "\u25BE" : "\u25B8") : "");
    c.setAttribute("aria-hidden", "true");
    return c;
  }

  function taskNode(rec, color) {
    var li = el("li", "pt-node pt-task");
    li.setAttribute("role", "none");
    var b = el("button", "pt-row pt-task-row role-" + rec.role + " kind-" + rec.kind + " state-" + (rec.display || rec.state));
    b.type = "button";
    b.setAttribute("role", "treeitem");
    var r = ROLE[rec.role] || ROLE.implementation;
    b.title = rec.key + " \u00B7 " + r.label + " \u00B7 " + (rec.display || rec.state);
    b.setAttribute("aria-label", r.label + ": " + rec.title + ", " + (rec.display || rec.state));
    b.style.setProperty("--pc", color);
    b.appendChild(el("span", "pt-sym", r.sym));
    b.appendChild(el("span", "pt-title", rec.title));
    if (rec.critical) b.appendChild(el("span", "pt-crit", "\u2605"));
    b.addEventListener("click", function () { PP().focusNode(rec.key); });
    li.appendChild(b);
    return li;
  }

  function phaseNode(p, ph) {
    var id = p.name + "|" + ph.name, open = !!expanded[id];
    var li = el("li", "pt-node pt-phase");
    li.setAttribute("role", "none");
    var row = el("div", "pt-line");
    var b = el("button", "pt-row pt-phase-row" + (PP().isFilterOn && !PP().isFilterOn("phase", ph.name) ? " off" : ""));
    b.type = "button";
    b.setAttribute("role", "treeitem");
    b.setAttribute("aria-expanded", String(open));
    b.style.setProperty("--pc", p.color);
    var pct = ph.total ? Math.round(100 * ph.done / ph.total) : 0;
    b.style.setProperty("--pct", pct + "%");
    b.appendChild(rowHead(open, true));
    b.appendChild(el("span", "pt-title", ph.name));
    b.appendChild(el("span", "pt-count", ph.done + "/" + ph.total));
    b.addEventListener("click", function () { expanded[id] = !expanded[id]; renderProjects(); });
    row.appendChild(b);
    row.appendChild(eyeBtn("phase", ph.name, "phase " + ph.name));
    li.appendChild(row);
    if (open) {
      var ul = el("ul", "pt-list"); ul.setAttribute("role", "group");
      ph.keys.forEach(function (k) {
        var rec = VM.items.get(k);
        if (rec) ul.appendChild(taskNode(rec, p.color));
      });
      li.appendChild(ul);
    }
    return li;
  }

  function projectNode(p) {
    var open = !!expanded[p.name];
    var li = el("li", "pt-node pt-project");
    li.setAttribute("role", "none");
    var row = el("div", "pt-line");
    var b = el("button", "pt-row pt-project-row" + (PP().isFilterOn && !PP().isFilterOn("project", p.name) ? " off" : ""));
    b.type = "button";
    b.setAttribute("role", "treeitem");
    b.setAttribute("aria-expanded", String(open));
    b.style.setProperty("--pc", p.color);
    var tot = 0, done = 0;
    p.phases.forEach(function (ph) { tot += ph.total; done += ph.done; });
    b.appendChild(rowHead(open, true));
    b.appendChild(el("span", "pt-title", p.name));
    b.appendChild(el("span", "pt-count", done + "/" + tot));
    b.addEventListener("click", function () { expanded[p.name] = !expanded[p.name]; renderProjects(); });
    row.appendChild(b);
    row.appendChild(eyeBtn("project", p.name, "project " + p.name));
    li.appendChild(row);
    if (open) {
      var ul = el("ul", "pt-list"); ul.setAttribute("role", "group");
      p.phases.forEach(function (ph) { ul.appendChild(phaseNode(p, ph)); });
      li.appendChild(ul);
    }
    return li;
  }

  function renderProjects() {
    var box = $("project-tree");
    if (!box || !VM) return;
    clear(box);
    var legend = el("p", "pt-legend");
    ["planning", "implementation", "review"].forEach(function (k) {
      legend.appendChild(el("span", "pt-leg", ROLE[k].sym + " " + ROLE[k].label));
    });
    box.appendChild(legend);
    var ul = el("ul", "pt-list pt-root"); ul.setAttribute("role", "group");
    (VM.projects || []).forEach(function (p) { ul.appendChild(projectNode(p)); });
    box.appendChild(ul);
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
    if (global.PlanUI.QAPage && global.PlanUI.Preview) { global.PlanUI.QAPage.render(VM); return; }
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
    if (global.PlanUI.QAPage && global.PlanUI.Preview) return;      // answers live in the Q&A page
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

  function setAttributes() { /* the sidebar is the project tree only; pages live in the header */ }

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
    if (tab === "projects") renderProjects();
    else if (tab === "qa") renderQA();
    else if (tab === "answers") renderAnswers();
    else if (tab === "settings") renderSettings();
    else renderNeeds();
  }

  function renderAll() {
    renderCounts();
    renderProjects();
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
    UI.subscribe(function (keys) {
      if (keys.indexOf("query") >= 0) renderAll();
      if (keys.indexOf("settings") >= 0) syncSettings();
      if (keys.indexOf("sidebar") >= 0) {
        setAttributes();
        renderPanel(UI.get("sidebar").tab);
      }
    });
  }

  PlanUI.Sidebar = { mount: mount, sync: syncSettings, render: renderAll, renderProjects: renderProjects };

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
