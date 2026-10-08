"""W6 Q&A page — node-run tests for page_qa.js (PlanUI.QAPage).

The All/Open/Answered status filter shows the right rows; answering a
question makes the export JSON contain {question, answer, answered_at};
"Remove local answer" clears the local answer and restores the row.

Fixture: tests/fixtures/ui/synthetic_snapshot.json (12 questions: 10 open,
2 answered). Skips cleanly when node is missing, like tests/test_ui_qa_counts.py.
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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "synthetic_snapshot.json"

sys.path.insert(0, str(SRC))

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

HARNESS = r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var FIXTURE = process.argv[3];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

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
  walk(c, []).concat([c]).forEach(function (n) {  // live-DOM fidelity: detached ids vanish
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
  var f = this.listeners.click || [];
  for (var i = 0; i < f.length; i++) f[i]({ type: "click", preventDefault: function () {} });
};
El.prototype.closest = function () { return null; };
El.prototype.querySelectorAll = function (sel) { return qsa(this, sel); };
El.prototype.querySelector = function (sel) { return qsa(this, sel)[0] || null; };
El.prototype.getBoundingClientRect = function () {
  return { left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 };
};
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
  removeEventListener: function () {}
};
var win = { innerWidth: 1024, innerHeight: 768, addEventListener: function () {} };
win.document = doc;
global.window = win;
global.document = doc;
var store = {};
global.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; }
};
var clipboardCapture = null;
win.navigator = { clipboard: { writeText: function (t) { clipboardCapture = String(t); } } };

function add(parent, tag, cls, attrs, text) {
  var n = new El(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}

var sidebar = add(body, "aside", "sb", [["id", "sidebar"]]);
add(sidebar, "button", "sb-close", [["id", "sb-close"]]);
var tablist = add(sidebar, "div", "sb-tabs");
["needs", "qa", "answers", "settings"].forEach(function (t) {
  var b = add(tablist, "button", "sb-tab", [["data-tab", t], ["role", "tab"]]);
  var c = add(b, "span", "sb-count", [["id", "sb-count-" + t], ["hidden", ""]]);
  c.hidden = true;
});
var sbBody = add(sidebar, "div", "sb-body");
["needs", "qa", "answers", "settings"].forEach(function (t) {
  add(sbBody, "section", "sb-panel", [["id", "sb-panel-" + t], ["data-panel", t]]);
});
var tabbar = add(body, "nav", "tabs", [["id", "tabbar"]]);
add(tabbar, "span", "tab-badge", [["id", "inbox-badge"]]);

global.PlanPage = {
  state: { selected: null, projectsOff: {}, tracksOff: {} },
  $: function (id) { return doc.getElementById(id); },
  el: function (tag, cls, text) {
    var n = new El(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  },
  clear: function (n) { while (n.firstChild) n.removeChild(n.firstChild); },
  selectNode: function (k) { this.state.selected = k; },
  focusNode: function (k) { this.state.selected = k; },
  visibleItems: function () { return []; },
  setTab: function () {},
  renderGraph: function () {}
};

function clickText(root, txt) {
  var found = null;
  walk(root, []).forEach(function (n) {
    if (!found && n.tagName === "BUTTON" && n.textContent === txt) found = n;
  });
  if (found) found.click();
  return !!found;
}
function qaPanel() { return doc.getElementById("sb-panel-qa"); }
function qaRowIds() {
  var t = doc.getElementById("qa-table");
  if (!t) return null;
  return qsa(t, "tbody tr").map(function (r) { return r.getAttribute("id"); });
}
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
  require(WEB + "/page.js");
  var PlanUI = require(WEB + "/page_model.js");
  require(WEB + "/page_store.js");
  require(WEB + "/page_search.js");
  require(WEB + "/page_side.js");
  PlanUI.Preview = { attach: function () {} };
  require(WEB + "/page_qa.js");
  win.PlanPage = global.PlanPage;
  win.localStorage = global.localStorage;

  var vm = PlanUI.buildViewModel(JSON.parse(fs.readFileSync(FIXTURE, "utf8")));
  var ui = new PlanUI.Store({});
  PlanUI.app = { ui: ui, store: ui, vm: vm, jump: function () {} };
  PlanUI.Sidebar.mount(ui, vm);
  PlanUI.QAPage.render(vm);

  var openQ = null;
  vm.questions.forEach(function (q) { if (!openQ && q.status === "open") openQ = q; });
  var out = { qid: openQ.id, all: qaRowIds(),
              statuses: vm.questions.map(function (q) { return { id: q.id, status: q.status }; }) };

  clickText(qaPanel(), "Open");
  out.open = qaRowIds();
  clickText(qaPanel(), "Answered");
  out.answered = qaRowIds();
  clickText(qaPanel(), "All");
  out.allAgain = qaRowIds();

  /* Answer one open question: badge + export payload. */
  PlanUI.QAPage.setAnswer(openQ.id, "my local answer");
  out.badge = badgeOf(openQ.id);
  out.stored = global.localStorage.getItem("plangraph.qa.answers.v1");
  clickText(qaPanel(), "Open");
  out.openAfter = qaRowIds();
  clickText(qaPanel(), "Answered");
  out.answeredAfter = qaRowIds();
  clickText(qaPanel(), "Copy as JSON");
  out.clipboard = clipboardCapture;

  /* Open this row's editor and click "Remove local answer". */
  var rows = qsa(doc.getElementById("qa-table"), "tbody tr");
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].getAttribute("id") === "qa-row-" + openQ.id) {
      var btns = qsa(rows[i], "button");
      for (var j = 0; j < btns.length; j++) {
        if (btns[j].textContent === "Edit") { btns[j].click(); break; }
      }
      break;
    }
  }
  out.removeClicked = clickText(qaPanel(), "Remove local answer");
  out.storedAfter = global.localStorage.getItem("plangraph.qa.answers.v1");
  out.effectiveAfter = PlanUI.QAPage.effectiveStatus(openQ);
  clickText(qaPanel(), "All");
  out.badgeAfter = badgeOf(openQ.id);
  clickText(qaPanel(), "Open");
  out.openRestored = qaRowIds();
  clickText(qaPanel(), "Answered");
  out.answeredRestored = qaRowIds();
  emit(out);
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    harness = tmp_path / "qa_page_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), str(FIXTURE)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


def row_id(qid: str) -> str:
    return "qa-row-" + qid


@need_node
def test_status_filters_show_the_right_rows(tmp_path):
    out = run_node(tmp_path)
    by_status = {q["id"]: q["status"] for q in out["statuses"]}
    want_open = {row_id(q) for q, s in by_status.items() if s == "open"}
    want_answered = {row_id(q) for q, s in by_status.items() if s == "answered"}
    assert set(out["all"]) == want_open | want_answered
    assert set(out["open"]) == want_open
    assert set(out["answered"]) == want_answered
    assert len(out["open"]) == 10 and len(out["answered"]) == 2
    assert set(out["allAgain"]) == set(out["all"])


@need_node
def test_answer_export_json_contains_question_answer_answered_at(tmp_path):
    out = run_node(tmp_path)
    qid = out["qid"]
    assert out["badge"] == "answered (local)", out["badge"]  # row badge follows
    assert qid not in {r.replace("qa-row-", "") for r in out["openAfter"]}
    assert row_id(qid) in out["answeredAfter"]  # answered filter gains the row
    payload = json.loads(out["clipboard"])  # the Copy-as-JSON export
    assert payload == [{"question": qid, "answer": "my local answer",
                        "answered_at": json.loads(out["stored"])[qid]["at"]}]
    entry = payload[0]
    assert set(entry) == {"question", "answer", "answered_at"}


@need_node
def test_remove_local_answer_clears_it(tmp_path):
    out = run_node(tmp_path)
    qid = out["qid"]
    assert out["removeClicked"] is True, "Remove local answer button missing"
    stored = json.loads(out["storedAfter"] or "{}")
    assert qid not in stored  # local answer cleared
    assert out["badgeAfter"] == "open", out["badgeAfter"]
    assert out["effectiveAfter"] == "open"
    assert set(out["openRestored"]) == set(out["open"])  # filters back to snapshot
    assert set(out["answeredRestored"]) == set(out["answered"])
