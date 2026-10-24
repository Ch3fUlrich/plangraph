"""W15 Activity page — full JS_FILES order with a hand-made snapshot+activity.

Loads ALL page scripts in render.py JS_FILES order (like the live page) on a
minimal fake DOM (same contract as tests/test_ui_real_page.py) with a
hand-made snapshot carrying snapshot.activity (oldest first).

Rules (one mutation per rule named in each test):
  now = max activity ts (not wall clock); 24 h vs 7 d counts differ;
  created/done/cancelled/progress per project; questions asked/answered by
  OS-/Q- id prefix; unblocked = all deps done now AND latest dep done in
  range; blocked = created in range AND still has an unmet dep; custom range;
  empty range shows "Nothing happened"; key link selects the item.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

sys.path.insert(0, str(SRC))

from plangraph.render import JS_FILES  # noqa: E402

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

HARNESS = r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var FILES = JSON.parse(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }
console.log = function () {};

var ids = {};
function El(tag) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this.attributes = {};
  this.listeners = {};
  this.style = { setProperty: function () {}, getPropertyValue: function () { return ""; } };
  this.className = "";
  this.hidden = false;
  this.disabled = false;
  this.value = "";
  this.checked = false;
  this.clientWidth = 0;
  this.clientHeight = 0;
}
Object.defineProperty(El.prototype, "firstChild", {
  get: function () { return this.childNodes.length ? this.childNodes[0] : null; }
});
Object.defineProperty(El.prototype, "textContent", {
  get: function () {
    var t = this._text || "";
    for (var i = 0; i < this.childNodes.length; i++) t += this.childNodes[i].textContent || "";
    return t;
  },
  set: function (v) { this._text = String(v); this.childNodes = []; }
});
El.prototype.appendChild = function (c) {
  c.parentNode = this; this.childNodes.push(c); return c;
};
El.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i >= 0) { this.childNodes.splice(i, 1); c.parentNode = null; }
  walk(c, []).concat([c]).forEach(function (n) {
    if (n.attributes && n.attributes.id && ids[n.attributes.id] === n) delete ids[n.attributes.id];
  });
  return c;
};
El.prototype.setAttribute = function (n, v) {
  this.attributes[n] = String(v);
  if (n === "id") ids[String(v)] = this;
  if (n === "class") this.className = String(v);
};
El.prototype.getAttribute = function (n) {
  return (n in this.attributes) ? this.attributes[n] : null;
};
El.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.click = function () {
  (this.listeners.click || []).slice().forEach(function (f) { f({ type: "click", preventDefault: function () {} }); });
};
El.prototype.focus = function () {};
El.prototype.blur = function () {};
El.prototype.select = function () {};
El.prototype.closest = function () { return null; };
El.prototype.getBoundingClientRect = function () {
  return { left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 };
};
El.prototype.getClientRects = function () { return []; };
El.prototype.querySelectorAll = function (sel) { return qsa(this, sel); };
El.prototype.querySelector = function (sel) { return qsa(this, sel)[0] || null; };
El.prototype.classList = {
  add: function () {}, remove: function () {},
  toggle: function () {}, contains: function () { return false; }
};
Object.defineProperty(El.prototype, "id", {
  get: function () { return this.attributes.id || ""; },
  set: function (v) { this.setAttribute("id", String(v)); }
});
function tokenize(sel) {
  var out = [], i = 0, buf = "";
  while (i < sel.length) {
    var c = sel.charAt(i);
    if (c === "[") { var j = sel.indexOf("]", i); buf += sel.slice(i, j + 1); i = j + 1; }
    else if (c === " ") { if (buf) { out.push(buf); buf = ""; } i++; }
    else { buf += c; i++; }
  }
  if (buf) out.push(buf);
  return out;
}
function matches(node, part) {
  if (!node || node.nodeType !== 1) return false;
  var re = /\[([\w-]+)=['"]([^'"]*)['"]\]/g, m, rest = part;
  while ((m = re.exec(part))) {
    if (node.getAttribute(m[1]) !== m[2]) return false;
    rest = rest.replace(m[0], "");
  }
  var cls = rest.match(/\.[\w-]+/g) || [];
  var tag = rest.replace(/\.[\w-]+/g, "").replace(/#[\w-]+/g, "");
  var idm = part.match(/#([\w-]+)/);
  if (idm && node.getAttribute("id") !== idm[1]) return false;
  if (tag && tag !== "*") {
    if (node.tagName !== tag.toUpperCase()
        && node.tagName.toLowerCase() !== String(tag).toLowerCase()) return false;
  }
  var own = (node.className || "").split(/\s+/);
  for (var i = 0; i < cls.length; i++) {
    if (own.indexOf(cls[i].slice(1)) < 0) return false;
  }
  return true;
}
function walk(node, out) {
  if (!node || !node.childNodes) return out;
  node.childNodes.forEach(function (c) { out.push(c); walk(c, out); });
  return out;
}
function ancestorOk(node, parts, k) {
  for (var a = node.parentNode; k >= 0; k--, a = (a ? a.parentNode : null)) {
    while (a && !matches(a, parts[k])) a = a.parentNode;
    if (!a) return false;
  }
  return true;
}
function qsa(root, sel) {
  var parts = tokenize(sel), last = parts[parts.length - 1], hits = [];
  walk(root, []).forEach(function (n) {
    if (last && matches(n, last) && ancestorOk(n, parts, parts.length - 2)) hits.push(n);
  });
  return hits;
}

var htmlEl = new El("html");
var body = new El("body");
htmlEl.appendChild(body);
var doc = {
  nodeType: 9,
  readyState: "complete",
  body: body,
  documentElement: htmlEl,
  createElement: function (t) { return new El(t); },
  createTextNode: function (t) {
    var n = new El("#text");
    n.nodeType = 3;
    n.textContent = String(t);
    return n;
  },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function (sel) { return qsa(body, sel); },
  querySelector: function (sel) { return qsa(body, sel)[0] || null; },
  addEventListener: function () {},
  removeEventListener: function () {},
  execCommand: function () { return false; }
};
global.document = doc;
global.window = global;
function setG(k, v) { try { Object.defineProperty(global, k, { value: v, configurable: true, writable: true }); } catch (e) { try { global[k] = v; } catch (x) {} } }
setG("navigator", { clipboard: { writeText: function () {} } });
setG("location", { hash: "", pathname: "/", search: "" });
setG("history", { replaceState: function () {} });
setG("performance", { now: function () { return 0; }, mark: function () {} });
setG("innerWidth", 1280);
setG("innerHeight", 800);
setG("getComputedStyle", function () { return { getPropertyValue: function () { return ""; }, display: "block" }; });
setG("matchMedia", function () { return { matches: false, addEventListener: function () {}, addListener: function () {} }; });
setG("requestAnimationFrame", function (fn) { fn(); return 0; });
setG("addEventListener", function () {});
setG("removeEventListener", function () {});
global.URL = { createObjectURL: function () { return ""; }, revokeObjectURL: function () {} };
global.Blob = function () {};
var store = {};
global.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; }
};

function add(parent, tag, cls, attrs, text) {
  var n = new El(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}

function item(proj, iid, title, state, deps) {
  return { key: proj + ":" + iid, id: iid, project: proj, phase: "P1",
    title: title, kind: "lane", state: state, owner: "op",
    priority: 1, estimate_h: 2, depends_on: deps || [], evidence: [] };
}
/* Hand-made snapshot+activity, oldest first. now = 2026-09-30T10:00:00Z. */
var SNAP = {
  generated: "2026-09-30T10:00:00Z",
  items: [
    item("p1", "A", "Alpha task", "open", []),
    item("p1", "F", "Doomed task", "cancelled", []),
    item("p1", "B", "Base task", "done", []),
    item("p1", "Q-9", "Pick retry policy", "done", []),
    item("p2", "D", "Dependent task", "open", ["p2:E"]),
    item("p2", "E", "Free task", "open", []),
    item("p1", "C", "Child of base", "open", ["p1:B"]),
    item("p2", "G", "Claimed task", "claimed", []),
    item("p1", "H", "Submitted task", "in_review", []),
    item("p1", "W", "Half-waiting task", "open", ["p1:B", "p2:E"]),
    item("p2", "OS-2", "Ops question plain", "open", [])
  ],
  activity: [
    { ts: "2026-09-20T12:00:00Z", key: "p1:A", type: "item_created", actor: "op" },
    { ts: "2026-09-21T12:00:00Z", key: "p1:F", type: "item_created", actor: "op" },
    { ts: "2026-09-22T12:00:00Z", key: "p1:B", type: "item_created", actor: "op" },
    { ts: "2026-09-24T12:00:00Z", key: "p1:Q-9", type: "item_created", actor: "op" },
    { ts: "2026-09-24T13:00:00Z", key: "p2:D", type: "item_created", actor: "op" },
    { ts: "2026-09-24T14:00:00Z", key: "p2:OS-2", type: "item_created", actor: "op" },
    { ts: "2026-09-27T12:00:00Z", key: "p1:Q-9", type: "review_passed", actor: "op" },
    { ts: "2026-09-29T15:00:00Z", key: "p1:W", type: "item_created", actor: "op" },
    { ts: "2026-09-29T16:00:00Z", key: "p1:C", type: "item_created", actor: "op" },
    { ts: "2026-09-29T16:30:00Z", key: "p2:G", type: "item_created", actor: "op" },
    { ts: "2026-09-30T02:00:00Z", key: "p2:G", type: "claimed", actor: "op" },
    { ts: "2026-09-30T07:00:00Z", key: "p1:B", type: "closed_on_decision", actor: "op" },
    { ts: "2026-09-30T08:00:00Z", key: "p1:H", type: "item_created", actor: "op" },
    { ts: "2026-09-30T09:00:00Z", key: "p1:H", type: "submitted", actor: "op" },
    { ts: "2026-09-30T10:00:00Z", key: "p1:F", type: "cancelled", actor: "op" }
  ]
};

/* shell markup from page_template.html (hand-built: no parser) */
add(body, "header", "top");
add(body, "input", "", [["id", "search"]]);
add(body, "button", "", [["id", "search-toggle"]]);
add(body, "button", "", [["id", "tab-graph"]]);
add(body, "button", "", [["id", "tab-list"]]);
add(body, "button", "", [["id", "tab-qa"]]);
add(body, "button", "", [["id", "tab-needs"]]);
add(body, "button", "", [["id", "tab-activity"]]);
add(body, "button", "", [["id", "tab-settings"]]);
add(body, "button", "", [["id", "sb-toggle"]]);
add(body, "div", "", [["id", "sb-scrim"]]);
var sidebar = add(body, "aside", "sb", [["id", "sidebar"]]);
add(sidebar, "button", "sb-close", [["id", "sb-close"]]);
var sbmenu = add(sidebar, "nav", "sb-menu", []);
add(sbmenu, "button", "", [["id", "sb-activity"], ["data-page", "activity"]]);
add(sidebar, "div", "", [["id", "sb-panel-projects"]]);
add(body, "div", "", [["id", "sb-resize"]]);
add(body, "div", "", [["id", "panel-graph"]]);
add(body, "div", "", [["id", "panel-list"]]);
add(body, "div", "", [["id", "list-content"]]);
add(body, "div", "", [["id", "panel-qa"]]);
add(body, "div", "", [["id", "sb-panel-qa"]]);
add(body, "div", "", [["id", "sb-panel-answers"]]);
add(body, "div", "", [["id", "panel-needs"]]);
add(body, "div", "", [["id", "sb-panel-needs"]]);
add(body, "div", "", [["id", "panel-activity"]]);
add(body, "div", "", [["id", "sb-panel-activity"]]);
add(body, "div", "", [["id", "panel-settings"]]);
add(body, "div", "", [["id", "sb-panel-settings"]]);
add(body, "div", "", [["id", "project-tree"]]);
add(body, "div", "", [["id", "meta-line"]]);
add(body, "div", "", [["id", "progress-bar"]]);
add(body, "div", "", [["id", "progress-text"]]);
add(body, "div", "", [["id", "legend"]]);
add(body, "div", "", [["id", "cy"]]);
add(body, "div", "", [["id", "graph-empty"]]);
add(body, "input", "", [["id", "collapse-done"]]);
add(body, "span", "", [["id", "inbox-badge"]]);
add(body, "span", "", [["id", "sb-count-qa"]]);
add(body, "span", "", [["id", "sb-count-needs"]]);
add(body, "span", "", [["id", "sb-count-answers"]]);
var snapEl = add(body, "script", "", [["id", "snapshot-data"], ["type", "application/json"]]);
snapEl.textContent = JSON.stringify(SNAP);
var tabbar = add(body, "nav", "tabs", [["id", "tabbar"]]);
["graph", "table", "qa", "needs", "menu", "settings"].forEach(function (s) {
  add(tabbar, "button", "tab-btn", [["data-screen", s]]);
});
add(body, "aside", "details", [["id", "details"]]);

function keysOf(groupId) {
  var g = doc.getElementById(groupId);
  if (!g) return null;
  var out = [];
  walk(g, []).forEach(function (n) {
    if (n.tagName === "BUTTON" && (n.className || "").split(/\s+/).indexOf("keylink") >= 0)
      out.push(n.textContent);
  });
  return out;
}
function scen() {
  PlanUI.ActivityPage.render();
  var box = doc.getElementById("sb-panel-activity");
  var groups = {};
  ["created", "done", "cancelled", "progress", "qasked", "qanswered", "unblocked", "blocked"].forEach(function (g) {
    groups[g] = keysOf("act-group-" + g);
  });
  var ct = doc.getElementById("act-counts");
  return { text: box.textContent, groups: groups,
           countsText: ct ? ct.textContent : null };
}

function main() {
  FILES.forEach(function (f) { require(WEB + "/" + f); });
  var PlanUI = global.PlanUI;
  var AP = PlanUI.ActivityPage;
  var now = AP.nowMs(SNAP);
  var D = 86400000;
  function comp(start, end) { return AP.compute(SNAP, start, end).counts; }
  var out = {
    files: FILES.length,
    hasPage: !!(PlanUI.ActivityPage && PlanUI.ActivityPage.render && PlanUI.ActivityPage.compute),
    now: new Date(now).toISOString(),
    c24: comp(now - D, now),
    c7: comp(now - 7 * D, now)
  };
  AP.setPreset("24h");
  out.r24 = scen();
  AP.setPreset("7d");
  out.r7 = scen();
  AP.setCustom("2026-09-26T00:00:00Z", "2026-09-28T00:00:00Z");
  out.custom = scen();
  AP.setCustom("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z");
  out.empty = scen();
  /* header wiring: show the page like the tab button does */
  PlanUI.Pages.show("activity");
  out.panelHidden = doc.getElementById("panel-activity").hidden;
  out.tabSelected = doc.getElementById("tab-activity").getAttribute("aria-selected");
  /* phone menu wiring: the sidebar entry reaches the page with no tabbar entry */
  PlanUI.Pages.show("graph");
  doc.getElementById("sb-activity").click();
  out.menuPanelHidden = doc.getElementById("panel-activity").hidden;
  /* key link selects the item */
  AP.setPreset("24h");
  AP.render();
  var first = null;
  walk(doc.getElementById("act-group-created"), []).forEach(function (n) {
    if (!first && n.tagName === "BUTTON" && (n.className || "").split(/\s+/).indexOf("keylink") >= 0) first = n;
  });
  out.firstKey = first ? first.textContent : null;
  if (first) first.click();
  out.selected = global.PlanPage.state.selected;
  emit(out);
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    harness = tmp_path / "activity_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), json.dumps(list(JS_FILES))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


@need_node
def test_activity_page_registered_and_now_is_max_activity_ts(tmp_path):
    # Mutation that fails it: nowMs falling back to Date.now() (ranges drift
    # with the wall clock instead of ending at the latest activity event).
    out = run_node(tmp_path)
    assert out["hasPage"] is True
    assert "page_activity.js" in list(JS_FILES)
    assert out["now"] == "2026-09-30T10:00:00.000Z", out["now"]


@need_node
def test_24h_vs_7d_counts_differ(tmp_path):
    # Mutation that fails it: preset cutoffs swapped (7 d uses the 24 h
    # window), so c24 == c7.
    out = run_node(tmp_path)
    assert out["c24"]["p1"]["created"] == 3, out["c24"]  # C, H, W
    assert out["c24"]["p2"]["created"] == 1, out["c24"]  # G
    assert out["c7"]["p1"]["created"] == 4, out["c7"]  # + Q-9
    assert out["c7"]["p2"]["created"] == 3, out["c7"]  # + D, OS-2
    assert out["c24"] != out["c7"]


@need_node
def test_done_cancelled_created_progress_per_project(tmp_path):
    # Mutation that fails it: done counting `legacy_state_set` of any state
    # (or item_created) instead of closed_on_decision/review_passed/done-set.
    out = run_node(tmp_path)
    assert out["c24"]["p1"]["done"] == 1  # B
    assert out["c24"]["p1"]["cancelled"] == 1  # F
    assert out["c24"]["p1"]["progress"] == 1  # H submitted
    assert out["c24"]["p2"]["progress"] == 1  # G claimed
    assert out["c7"]["p1"]["done"] == 2  # B + Q-9
    # Mutation that fails it: progress counting `claimed` only (submitted
    # dropped) leaves p1 progress at 0 in the 24 h window.
    assert sorted(out["r24"]["groups"]["progress"]) == ["p1:H", "p2:G"]
    assert sorted(out["r24"]["groups"]["done"]) == ["p1:B"]
    assert out["r24"]["groups"]["cancelled"] == ["p1:F"]
    assert sorted(out["r24"]["groups"]["created"]) == ["p1:C", "p1:H", "p1:W", "p2:G"]


@need_node
def test_questions_asked_answered_by_id_prefix(tmp_path):
    # Mutation that fails it: isQuestion requiring a "Q:" title prefix, so
    # p1:Q-9 (plain title, Q- id) is never a question.
    out = run_node(tmp_path)
    assert sorted(out["r7"]["groups"]["qasked"]) == ["p1:Q-9", "p2:OS-2"], out["r7"]["groups"]
    assert out["r7"]["groups"]["qanswered"] == ["p1:Q-9"]
    assert out["r24"]["groups"]["qasked"] == []
    assert out["r24"]["groups"]["qanswered"] == []
    assert out["c7"]["p1"]["qanswered"] == 1
    assert out["c7"]["p2"]["qasked"] == 1


@need_node
def test_unblocked_needs_all_deps_done_with_latest_in_range(tmp_path):
    # Mutation that fails it: unblocked = ANY dep done in range, so p1:W
    # (B done in range, E still open) wrongly appears.
    out = run_node(tmp_path)
    assert out["r24"]["groups"]["unblocked"] == ["p1:C"], out["r24"]["groups"]
    assert out["r7"]["groups"]["unblocked"] == ["p1:C"]
    assert "p1:W" not in out["r7"]["groups"]["unblocked"]


@need_node
def test_blocked_is_created_in_range_with_unmet_dep(tmp_path):
    # Mutation that fails it: blocked = every created item (ignoring deps),
    # so p1:C/p2:G/p1:H wrongly appear alongside p1:W.
    out = run_node(tmp_path)
    assert out["r24"]["groups"]["blocked"] == ["p1:W"], out["r24"]["groups"]
    assert sorted(out["r7"]["groups"]["blocked"]) == ["p1:W", "p2:D"]
    assert "p1:C" not in out["r7"]["groups"]["blocked"]


@need_node
def test_custom_range(tmp_path):
    # Mutation that fails it: the custom inputs ignored (preset re-rendered),
    # so 2026-09-26..28 shows created rows instead of only the Q-9 pass.
    out = run_node(tmp_path)
    assert out["custom"]["groups"]["created"] == []
    assert out["custom"]["groups"]["done"] == ["p1:Q-9"]
    assert out["custom"]["groups"]["qanswered"] == ["p1:Q-9"]


@need_node
def test_empty_range_shows_nothing_happened(tmp_path):
    # Mutation that fails it: the empty message never rendered (blank panel
    # or a zero-row counts table instead).
    out = run_node(tmp_path)
    assert "Nothing happened" in out["empty"]["text"], out["empty"]["text"]
    assert out["empty"]["countsText"] is None
    assert out["empty"]["groups"]["created"] is None


@need_node
def test_header_page_wiring_and_key_link_selects_item(tmp_path):
    # Mutations that fail it: PANELS/TABS missing `activity` (show falls back
    # to graph, panel stays hidden); the key rendered as a span, so the
    # click never selects the item; the sidebar menu entry unbound, so the
    # phone menu cannot reach the page (tabbar stays at 6 entries max).
    out = run_node(tmp_path)
    assert out["panelHidden"] is False
    assert out["tabSelected"] == "true"
    assert out["menuPanelHidden"] is False
    assert out["firstKey"] == "p1:W"
    assert out["selected"] == "p1:W", out["selected"]


def test_now_prefers_render_time():
    """L1: presets end at snapshot.generated when present (not at the latest event)."""
    src = (WEB / "page_activity.js").read_text(encoding="utf-8")
    i = src.index("function nowMs(snap)")
    body = src[i:i + 400]
    assert body.index("snap.generated") < body.index("snap.activity")
