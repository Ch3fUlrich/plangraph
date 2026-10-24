/* PlanUI.ProgressPage — the Progress header page (PROGRESS-PANEL, router D-547).
 * A standing view, rebuilt by the 5-minute render: reads snapshot.progress
 * (computed by the keeper, views_progress.progress). Per project one row with
 * a count bar, hours, state counts, operator waits and the last update; below,
 * per project, the phases and what is running now (owner + writer model,
 * "unknown" until runs are recorded through the keeper). Text via textContent.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  function str(v) { return v == null ? "" : String(v); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = str(text);
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function jump(key) {
    var PP = global.PlanPage;
    if (PP && PP.selectNode) PP.selectNode(key);
    else if (PlanUI.app && PlanUI.app.jump) PlanUI.app.jump(key);
  }
  function num(v) { return typeof v === "number" && isFinite(v) ? v : 0; }
  function bar(pct) {
    var b = el("span", "prg-bar"), f = el("span", "prg-fill");
    f.style.width = Math.max(0, Math.min(100, num(pct))) + "%";
    b.appendChild(f);
    b.setAttribute("role", "img");
    b.setAttribute("aria-label", num(pct) + " percent done");
    return b;
  }

  /* Rows for the summary table; pure, so tests can call it without a DOM. */
  function rows(progress) {
    return ((progress && progress.projects) || []).map(function (p) {
      var st = p.states || {};
      return {
        project: str(p.project), pct: num(p.pct_count), pctHours: num(p.pct_hours),
        done: num(p.done), total: num(p.total), hoursLeft: num(p.hours_left),
        open: num(st.open), active: num(st.claimed) + num(st.in_review),
        operator: num(p.operator_open), updated: str(p.last_update || "-"),
        phases: p.phases || [], running: p.running || []
      };
    });
  }

  var HEAD = ["Project", "Done", "Count", "Hours", "Left (h)", "Open", "Active", "Operator", "Updated"];

  function render() {
    if (typeof document === "undefined") return;
    var box = document.getElementById("sb-panel-progress");
    if (!box) return;
    var PP = global.PlanPage;
    var snap = (PP && PP.state && PP.state.snapshot) || {};
    clear(box);
    var list = rows(snap.progress);
    if (!list.length) { box.appendChild(el("p", "act-empty", "No progress data in this snapshot.")); return; }
    var t = snap.progress.total || {};
    box.appendChild(el("p", "prg-total", "All projects: " + num(t.pct_count) + "% done (" + num(t.done) + "/" +
      num(t.total) + "), " + num(t.hours_left) + " h left. As of " + str(snap.generated || "-") + "."));
    var wrap = el("div", "prg-scroll"), table = el("table", "act-counts prg-table"), tr = el("tr");
    HEAD.forEach(function (h) { tr.appendChild(el("th", null, h)); });
    var thead = el("thead"); thead.appendChild(tr); table.appendChild(thead);
    var tbody = el("tbody");
    list.forEach(function (r) {
      var row = el("tr");
      row.appendChild(el("td", "prg-name", r.project));
      var cell = el("td", "prg-cell"); cell.appendChild(bar(r.pct)); cell.appendChild(el("span", "prg-pct", r.pct + "%"));
      row.appendChild(cell);
      [r.done + "/" + r.total, r.pctHours + "%", r.hoursLeft, r.open, r.active, r.operator, r.updated].forEach(function (v) {
        row.appendChild(el("td", null, v));
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody); wrap.appendChild(table); box.appendChild(wrap);
    list.forEach(function (r) {
      var det = el("details", "act-group");
      det.id = "prg-" + r.project;
      det.appendChild(el("summary", null, r.project + ": " + r.phases.length + " phases, " + r.running.length + " running"));
      r.phases.forEach(function (ph) {
        var row = el("div", "act-row");
        row.appendChild(bar(ph.pct_count));
        row.appendChild(el("span", "act-title", str(ph.phase) + " " + num(ph.pct_count) + "% (" + num(ph.done) + "/" + num(ph.total) + ")"));
        row.appendChild(el("span", "act-time", num(ph.hours_left) + " h left"));
        det.appendChild(row);
      });
      r.running.forEach(function (run) {
        var row = el("div", "act-row prg-run"), link = el("button", "keylink", run.key);
        link.type = "button";
        link.setAttribute("aria-label", "Select " + str(run.key));
        link.addEventListener("click", function () { jump(run.key); });
        row.appendChild(link);
        row.appendChild(el("span", "act-title", str(run.title)));
        row.appendChild(el("span", "act-time", str(run.owner || "-") + " · " + str(run.state) + " · model " + str(run.model || "unknown")));
        det.appendChild(row);
      });
      box.appendChild(det);
    });
  }

  PlanUI.ProgressPage = { render: render, rows: rows };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
