/* PlanUI.ActivityPage — the Activity header page (D-696): what happened in a
 * time range. Reads snapshot.activity (oldest first, last 31 days) plus the
 * current snapshot.items. Range presets 24 h (default) / 7 d / 30 d end at
 * the latest activity timestamp (so ranges are stable for old snapshots);
 * custom uses two datetime-local inputs. Per project a counts table, below
 * expandable lists whose key link selects the item. Text via textContent.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  var DAY = 86400000;
  var PRESETS = { "24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY };
  var GROUPS = [
    ["created", "Created"], ["done", "Done"], ["cancelled", "Cancelled"],
    ["progress", "In progress"], ["qasked", "Questions asked"],
    ["qanswered", "Questions answered"], ["unblocked", "Unblocked"],
    ["blocked", "Blocked"]
  ];
  var COLS = ["created", "done", "cancelled", "progress", "qasked", "qanswered", "unblocked", "blocked"];
  var mode = "24h", customStart = "", customEnd = "";

  function parseTs(v) { var m = Date.parse(v); return isFinite(m) ? m : NaN; }
  function isDone(e) {
    return e.type === "closed_on_decision" || e.type === "review_passed" || e.type === "answered" ||
      (e.type === "legacy_state_set" && e.state === "done");
  }
  function isCancelled(e) {
    return e.type === "cancelled" ||
      (e.type === "legacy_state_set" && e.state === "cancelled");
  }
  function isProgress(e) { return e.type === "claimed" || e.type === "submitted"; }
  function str(v) { return v == null ? "" : String(v); }
  function idOf(key, byKey) {
    var it = byKey[key];
    if (it && it.id != null) return str(it.id);
    var i = str(key).indexOf(":");
    return i >= 0 ? str(key).slice(i + 1) : str(key);
  }
  function projOf(key, byKey) {
    var it = byKey[key];
    if (it && it.project != null && it.project !== "") return str(it.project);
    var i = str(key).indexOf(":");
    return i > 0 ? str(key).slice(0, i) : "(unknown)";
  }
  function depsOf(key, byKey) {
    var it = byKey[key];
    var d = it && it.depends_on;
    return Array.isArray(d) ? d : [];
  }
  function stateOf(key, byKey) {
    var it = byKey[key];
    return it ? str(it.state) : "";
  }
  function isQuestion(snap, key, byKey) {
    var id = idOf(key, byKey);
    if (id.indexOf("OS-") === 0 || id.indexOf("Q-") === 0) return true;
    var it = byKey[key];
    if (it && (it.track === "QUESTIONS" || it.phase === "QUESTIONS")) return true;
    var qs = snap.questions;
    if (Array.isArray(qs)) {
      for (var i = 0; i < qs.length; i++) {
        if (qs[i] && (qs[i].id === id || qs[i].item === key)) return true;
      }
    }
    return false;
  }
  /* Times come from snapshot.activity events (keeper events table), not from
   * item.createdAt, which is empty for most items. */
  /* Ranges end at the render time (snapshot.generated); without it, at the
   * latest activity event; last resort the wall clock. */
  function nowMs(snap) {
    var gen = parseTs(snap.generated);
    if (isFinite(gen)) return gen;
    var best = NaN, act = snap.activity;
    if (Array.isArray(act)) {
      act.forEach(function (e) {
        var m = e ? parseTs(e.ts) : NaN;
        if (isFinite(m) && (!isFinite(best) || m > best)) best = m;
      });
    }
    return isFinite(best) ? best : Date.now();
  }
  function inRange(m, start, end) { return isFinite(m) && m >= start && m <= end; }

  function compute(snap, start, end) {
    snap = snap || {};
    var items = Array.isArray(snap.items) ? snap.items : [];
    var act = Array.isArray(snap.activity) ? snap.activity : [];
    var byKey = {};
    items.forEach(function (it) { if (it && it.key) byKey[it.key] = it; });
    var groups = {};
    GROUPS.forEach(function (g) { groups[g[0]] = []; });
    var counts = {};
    function bump(proj, col) {
      if (!counts[proj]) {
        counts[proj] = {};
        COLS.forEach(function (c) { counts[proj][c] = 0; });
      }
      counts[proj][col] += 1;
    }
    function push(col, key, ts) {
      groups[col].push({ key: key, title: str((byKey[key] || {}).title), ts: ts });
      bump(projOf(key, byKey), col);
    }
    var doneAt = {};
    act.forEach(function (e) {
      if (!e || !isDone(e)) return;
      var m = parseTs(e.ts);
      if (isFinite(m) && (!isFinite(doneAt[e.key]) || m > doneAt[e.key])) doneAt[e.key] = m;
    });
    var createdAt = {};
    act.forEach(function (e) {
      if (!e || e.type !== "item_created") return;
      var m = parseTs(e.ts);
      if (!inRange(m, start, end)) return;
      if (!isFinite(createdAt[e.key]) || m < createdAt[e.key]) createdAt[e.key] = m;
      push("created", e.key, e.ts);
      if (isQuestion(snap, e.key, byKey)) push("qasked", e.key, e.ts);
    });
    act.forEach(function (e) {
      if (!e) return;
      var m = parseTs(e.ts);
      if (!inRange(m, start, end)) return;
      if (isDone(e)) {
        push("done", e.key, e.ts);
        if (isQuestion(snap, e.key, byKey)) push("qanswered", e.key, e.ts);
      } else if (isCancelled(e)) {
        push("cancelled", e.key, e.ts);
      } else if (isProgress(e)) {
        push("progress", e.key, e.ts);
      }
    });
    Object.keys(byKey).forEach(function (key) {
      var st = stateOf(key, byKey), deps = depsOf(key, byKey);
      if (!deps.length || st === "done" || st === "cancelled") return;
      var latest = NaN, open = false;
      deps.forEach(function (d) {
        if (!(byKey[d] && stateOf(d, byKey) === "done")) { open = true; return; }
        if (isFinite(doneAt[d]) && (!isFinite(latest) || doneAt[d] > latest)) latest = doneAt[d];
      });
      if (!open && inRange(latest, start, end)) {
        push("unblocked", key, new Date(latest).toISOString());
      }
      if (isFinite(createdAt[key]) && open) {
        push("blocked", key, new Date(createdAt[key]).toISOString());
      }
    });
    return { projects: Object.keys(counts).sort(), counts: counts, groups: groups };
  }

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
  function rangeOf(snap) {
    var now = nowMs(snap), start, end;
    if (mode === "custom") {
      end = parseTs(customEnd);
      if (!isFinite(end)) end = now;
      start = parseTs(customStart);
      if (!isFinite(start)) start = end - PRESETS["24h"];
    } else {
      end = now;
      start = now - PRESETS[mode];
    }
    return { start: start, end: end };
  }
  function localInput(v) {
    try {
      var s = new Date(v).toISOString();
      return s.slice(0, 16);
    } catch (e) { return ""; }
  }

  function render() {
    if (typeof document === "undefined") return;
    var box = document.getElementById("sb-panel-activity");
    if (!box) return;
    var PP = global.PlanPage;
    var snap = (PP && PP.state && PP.state.snapshot) || {};
    clear(box);
    var rg = rangeOf(snap), out = compute(snap, rg.start, rg.end);
    var bar = el("div", "act-range");
    Object.keys(PRESETS).forEach(function (name) {
      var b = el("button", null, name);
      b.type = "button";
      b.setAttribute("aria-pressed", str(mode === name));
      b.addEventListener("click", function () { mode = name; render(); });
      bar.appendChild(b);
    });
    var from = el("input", "act-custom");
    from.setAttribute("type", "datetime-local");
    from.setAttribute("aria-label", "Range start");
    from.value = localInput(customStart || rg.start);
    var to = el("input", "act-custom");
    to.setAttribute("type", "datetime-local");
    to.setAttribute("aria-label", "Range end");
    to.value = localInput(customEnd || rg.end);
    var apply = el("button", null, "Show");
    apply.type = "button";
    apply.addEventListener("click", function () {
      mode = "custom"; customStart = from.value; customEnd = to.value; render();
    });
    bar.appendChild(from); bar.appendChild(to); bar.appendChild(apply);
    box.appendChild(bar);
    var total = 0;
    GROUPS.forEach(function (g) { total += out.groups[g[0]].length; });
    if (!total) {
      box.appendChild(el("p", "act-empty", "Nothing happened"));
      return;
    }
    var t = el("table", "act-counts");
    t.id = "act-counts";
    var hr = el("tr");
    ["Project"].concat(GROUPS.map(function (g) { return g[1]; })).forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    var head = el("thead"); head.appendChild(hr); t.appendChild(head);
    var tb = el("tbody");
    out.projects.forEach(function (p) {
      var tr = el("tr");
      tr.appendChild(el("td", null, p));
      COLS.forEach(function (c) { tr.appendChild(el("td", null, out.counts[p][c])); });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    box.appendChild(t);
    GROUPS.forEach(function (g) {
      var rows = out.groups[g[0]];
      var det = el("details", "act-group");
      det.id = "act-group-" + g[0];
      var sum = el("summary", null, g[1] + " (" + rows.length + ")");
      det.appendChild(sum);
      rows.forEach(function (r) {
        var row = el("div", "act-row");
        var link = el("button", "keylink", r.key);
        link.type = "button";
        link.setAttribute("aria-label", "Select " + r.key);
        link.addEventListener("click", function () { jump(r.key); });
        row.appendChild(link);
        row.appendChild(el("span", "act-title", r.title));
        row.appendChild(el("span", "act-time", r.ts));
        det.appendChild(row);
      });
      box.appendChild(det);
    });
  }

  PlanUI.ActivityPage = {
    render: render, compute: compute, nowMs: nowMs,
    setPreset: function (name) { if (PRESETS[name]) mode = name; },
    setCustom: function (s, e) { mode = "custom"; customStart = s; customEnd = e; }
  };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
