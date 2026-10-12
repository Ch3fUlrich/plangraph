"""W9 real-page regression — full JS_FILES order with keeper-shaped evidence refs.

Loads ALL page scripts in render.py JS_FILES order (like the live page) on a
minimal fake DOM (same contract as tests/test_ui_shell.py) with a
keeper-shaped snapshot (evidence refs are objects, not strings).

(a) decisions: ids are "D-..." strings, no "[object Object]" in Decisions text.
    Mutation that fails it: deriveDecisions using `ev.ref` directly as the id.
(b) answer an open question via QAPage.setAnswer -> its row badge contains
    "answered"; removing restores it.
    Mutation that fails it: QAPage.refresh skipping the Q&A page render
    (only Sidebar.render), leaving the visible row stale.
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

/* keeper-shaped snapshot: decision evidence refs are OBJECTS */
var SNAP = {
  items: [
    { key: "projA:T1", id: "T1", project: "projA", phase: "P1", title: "Task one",
      kind: "lane", state: "open", owner: "alice", priority: 1, estimate_h: 2,
      depends_on: [], evidence: [{ kind: "decision", ref: { id: "D-548" }, at: "2026-01-01T00:00:00Z" }] },
    { key: "projB:T2", id: "T2", project: "projB", phase: "P1", title: "Task two",
      kind: "lane", state: "open", owner: "bob", priority: 1, estimate_h: 2,
      depends_on: [], evidence: [{ kind: "decision", ref: { id: "D-549" }, at: "2026-01-02T00:00:00Z" }] },
    { key: "projA:Q-1", id: "Q-1", project: "projA", phase: "P1", title: "Q: open question?",
      kind: "lane", state: "open", owner: "alice", priority: 5, estimate_h: 1,
      depends_on: [], evidence: [] }
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
add(body, "button", "", [["id", "tab-settings"]]);
add(body, "button", "", [["id", "sb-toggle"]]);
add(body, "div", "", [["id", "sb-scrim"]]);
var sidebar = add(body, "aside", "sb", [["id", "sidebar"]]);
add(sidebar, "button", "sb-close", [["id", "sb-close"]]);
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

function badgeOf(qid) {
  var row = doc.getElementById("qa-row-" + qid);
  if (!row) return null;
  var found = null;
  walk(row, []).forEach(function (n) {
    if (!found && n.className && n.className.split(/\s+/).indexOf("tc-badge") >= 0) found = n;
  });
  return found ? found.textContent : null;
}

function main() {
  FILES.forEach(function (f) { require(WEB + "/" + f); });
  var PlanUI = global.PlanUI;
  var vm = PlanUI.app.vm;
  var decTable = doc.getElementById("decisions-table");
  var decText = decTable ? decTable.textContent : null;
  var out = {
    files: FILES.length,
    decisionIds: (vm.decisions || []).map(function (d) { return d.id; }),
    decText: decTable ? decText.slice(0, 2000) : null,
    decTableFound: !!decTable
  };
  var qid = "Q-1";
  out.badgeBefore = badgeOf(qid);
  out.barBefore = doc.getElementById("sb-panel-qa").textContent.slice(0, 500);
  PlanUI.QAPage.setAnswer(qid, "local yes");
  out.badgeAfter = badgeOf(qid);
  out.effAfter = PlanUI.QAPage.effectiveStatus(vm.questions.filter(function (q) { return q.id === qid; })[0]);
  PlanUI.QAPage.setAnswer(qid, "");
  out.badgeRestored = badgeOf(qid);
  out.effRestored = PlanUI.QAPage.effectiveStatus(vm.questions.filter(function (q) { return q.id === qid; })[0]);
  /* Live-page repro: even when Sidebar.render leaves the Q&A rows stale
   * (counts/Needs update, rows do not), setAnswer must still update the
   * visible row via the Q&A page render. */
  var realSidebar = PlanUI.Sidebar.render;
  PlanUI.Sidebar.render = function () {};
  PlanUI.QAPage.setAnswer(qid, "local yes");
  out.badgeStubbed = badgeOf(qid);
  PlanUI.QAPage.setAnswer(qid, "");
  out.badgeStubRestored = badgeOf(qid);
  PlanUI.Sidebar.render = realSidebar;
  emit(out);
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    harness = tmp_path / "real_page_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), json.dumps(list(JS_FILES))]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


@need_node
def test_real_page_decision_refs_are_strings(tmp_path):
    # Mutation that fails it: deriveDecisions using `ev.ref` directly as id
    # (one "[object Object]" row tagged with all projects).
    out = run_node(tmp_path)
    assert out["decTableFound"] is True, "decisions-table missing on real page"
    assert sorted(out["decisionIds"]) == ["D-548", "D-549"], out["decisionIds"]
    assert all(isinstance(i, str) and i.startswith("D-") for i in out["decisionIds"])
    assert "[object Object]" not in (out["decText"] or ""), out["decText"]


@need_node
def test_real_page_answer_updates_visible_row_badge(tmp_path):
    # Mutation that fails it: QAPage.refresh without the direct Q&A page
    # render (Sidebar.render only) leaves badgeStubbed == "open".
    out = run_node(tmp_path)
    assert out["badgeBefore"] == "open", out["badgeBefore"]
    assert "answered" in (out["badgeAfter"] or ""), out["badgeAfter"]
    assert out["effAfter"] == "answered"
    assert out["badgeRestored"] == "open", out["badgeRestored"]
    assert out["effRestored"] == "open"
    assert "answered" in (out["badgeStubbed"] or ""), out["badgeStubbed"]
    assert out["badgeStubRestored"] == "open", out["badgeStubRestored"]
