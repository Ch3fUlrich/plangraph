"""W5 Q&A local-answer counts — node-run test for the Decisions & Q&A fix.

Saving a local answer (PlanUI.QAPage.setAnswer, localStorage key
plangraph.qa.answers.v1) must update, without a reload: (a) the status badge
in the Q&A table row, (b) the "Answered" count, (c) the Needs You list/count
and the phone #inbox-badge. Removing the local answer restores everything.

Fixture: tests/fixtures/ui/synthetic_snapshot.json (12 questions: 10 open,
2 answered). Skips cleanly when node is missing. Follows the node-runner
pattern of tests/test_ui_questions.py / tests/test_ui_shell.py.
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

/* ---- minimal fake DOM (same contract as tests/test_ui_shell.py) ------------ */
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
El.prototype.closest = function () { return null; };
El.prototype.querySelectorAll = function (sel) { return qsa(this, sel); };
El.prototype.querySelector = function (sel) { return qsa(this, sel)[0] || null; };
El.prototype.classList = {
  add: function (c) {},
  remove: function (c) {},
  toggle: function () {},
  contains: function (c) { return false; }
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
  var tag = rest.replace(/\.[\w-]+/g, "");
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
  node.childNodes.forEach(function (c) { out.push(c); walk(c, out); });
  return out;
}
function ancestorOk(node, parts, k) {
  for (var a = node.parentNode; k >= 0; k--, a = a.parentNode) {
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

var body = new El("body");
var doc = {
  nodeType: 9,
  readyState: "complete",
  body: body,
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
global.document = doc;

/* localStorage fake for plangraph.qa.answers.v1 */
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

/* shell markup the sidebar + Q&A page expect */
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
  state: { selected: null, items: [], unblocks: {}, booted: true, cy: null,
           projectsOff: {}, tracksOff: {} },
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
  renderGraph: function () {},
  isFilterOn: function () { return true; },
  toggleFilter: function () { return true; }
};

function main() {
  require(WEB + "/page.js");
  var PlanUI = require(WEB + "/page_model.js");
  require(WEB + "/page_store.js");
  require(WEB + "/page_search.js");
  require(WEB + "/page_side.js");
  PlanUI.Preview = { attach: function () {} };   // hover popups need no DOM here
  require(WEB + "/page_qa.js");

  var vm = PlanUI.buildViewModel(JSON.parse(fs.readFileSync(FIXTURE, "utf8")));
  var ui = new PlanUI.Store({});
  PlanUI.app = { ui: ui, store: ui, vm: vm, jump: function () {} };
  PlanUI.Sidebar.mount(ui, vm);

  var openQ = null;
  vm.questions.forEach(function (q) { if (!openQ && q.status === "open") openQ = q; });
  if (!openQ) { emit({ error: "fixture has no OPEN question" }); return; }
  var qid = openQ.id;

  function badgeOf(id) {           // text of the tc-badge span in this question's row
    var row = doc.getElementById("qa-row-" + id);
    if (!row) return null;
    var found = null;
    walk(row, []).forEach(function (n) {
      if (!found && n.className && n.className.split(/\s+/).indexOf("tc-badge") >= 0) found = n;
    });
    return found ? found.textContent : null;
  }
  function snap() {
    return {
      badge: badgeOf(qid),
      needsYou: vm.needsYou.length,
      answered: vm.answers.length,
      inbox: doc.getElementById("inbox-badge").textContent,
      answeredCount: doc.getElementById("sb-count-answers").textContent,
      needsPanel: doc.getElementById("sb-panel-needs").textContent.slice(0, 120)
    };
  }

  var before = snap();
  var N = before.needsYou, A = before.answered;

  PlanUI.QAPage.setAnswer(qid, "x");       // must re-render table + counts + badge
  var afterSave = snap();
  var effSave = PlanUI.QAPage.effectiveStatus(openQ);

  PlanUI.QAPage.setAnswer(qid, "");        // removing restores the snapshot status
  var afterRemove = snap();
  var effRemove = PlanUI.QAPage.effectiveStatus(openQ);

  emit({ qid: qid, N: N, A: A, before: before,
         afterSave: afterSave, effSave: effSave,
         afterRemove: afterRemove, effRemove: effRemove,
         hasDecider: typeof PlanUI.qaEffectiveStatus === "function" });
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    harness = tmp_path / "qa_counts_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), str(FIXTURE)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


@need_node
def test_local_answer_updates_badge_counts_and_inbox(tmp_path):
    out = run_node(tmp_path)
    assert out["hasDecider"] is True  # one function decides the EFFECTIVE status
    qid = out["qid"]
    N, A = out["N"], out["A"]
    assert N > 0 and A >= 0

    before = out["before"]
    assert before["badge"] == "open"
    assert before["inbox"] == str(N)
    assert "Needs You (%d)" % N in before["needsPanel"]

    saved = out["afterSave"]
    assert out["effSave"] == "answered"
    assert saved["badge"] == "answered (local)", saved["badge"]  # (a) row badge
    assert saved["answered"] == A + 1, saved  # (b) Answered count
    assert saved["answeredCount"] == str(A + 1), saved
    assert saved["needsYou"] == N - 1, saved  # (c) Needs You list/count
    assert "Needs You (%d)" % (N - 1) in saved["needsPanel"], saved["needsPanel"]
    assert saved["inbox"] == str(N - 1), saved  # (c) phone #inbox-badge


@need_node
def test_removing_local_answer_restores_snapshot_status(tmp_path):
    out = run_node(tmp_path)
    N, A = out["N"], out["A"]

    removed = out["afterRemove"]
    assert out["effRemove"] == "open"
    assert removed["badge"] == "open", removed["badge"]
    assert removed["needsYou"] == N, removed
    assert removed["answered"] == A, removed
    assert removed["inbox"] == str(N), removed
    assert removed["answeredCount"] == str(A), removed
    assert "Needs You (%d)" % N in removed["needsPanel"], removed["needsPanel"]
