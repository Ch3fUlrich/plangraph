"""W6 table page — node-run tests for page_table_view.js (PlanUI.TablePage).

Grouping by project / phase / state gives one table + one progress bar per
group with correct row counts and done-percent; the MAX_ROWS per-group cap
(leak-checked against the value in the source) truncates a 1000-row group with
an overflow control naming the total; the hover preview chip takes its text
from textContent, so a title containing "<b>x</b>" renders as literal text.

Synthetic data only (the UI fixture + a generated 1000-row snapshot, never a
live snapshot). Skips cleanly when node is missing, like tests/test_ui_pure.py.
"""

from __future__ import annotations

import json
import math
import re
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

SOURCE = (WEB / "page_table_view.js").read_text(encoding="utf-8")
MAX_ROWS = int(re.search(r"var MAX_ROWS\s*=\s*(\d+)", SOURCE).group(1))

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

function add(parent, tag, cls, attrs, text) {
  var n = new El(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}
add(body, "div", "", [["id", "list-content"]]);

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

function sectionInfo(sec) {
  function one(sel) { var h = qsa(sec, sel); return h.length ? h[0] : null; }
  var name = one(".tg-name"), cnt = one(".tg-count"),
      pct = one(".tg-pct"), bar = one(".tg-bar");
  return {
    name: name && name.textContent,
    cnt: cnt && cnt.textContent,
    pct: pct && pct.textContent,
    now: bar && bar.getAttribute("aria-valuenow"),
    tables: qsa(sec, "table").length,
    bars: qsa(sec, ".tg-bar").length,
    rows: qsa(sec, "tbody tr").length
  };
}

function main() {
  require(WEB + "/page.js");
  var PlanUI = require(WEB + "/page_model.js");
  require(WEB + "/page_store.js");
  require(WEB + "/page_search.js");
  require(WEB + "/page_table_view.js");
  win.PlanPage = global.PlanPage;
  win.localStorage = global.localStorage;

  var vm = PlanUI.buildViewModel(JSON.parse(fs.readFileSync(FIXTURE, "utf8")));
  var ui = new PlanUI.Store({});
  PlanUI.app = { ui: ui, store: ui, vm: vm, jump: function () {} };
  var out = { groups: {} };

  ["project", "phase", "state"].forEach(function (g) {
    PlanUI.TablePage.view.group = g;
    PlanUI.TablePage.view.collapsed = {};
    PlanUI.TablePage.view.more = {};
    PlanUI.TablePage.render();
    var box = doc.getElementById("list-content");
    out.groups[g] = qsa(box, ".tg").map(sectionInfo);
  });

  var recs = [];
  vm.items.forEach(function (r) {
    recs.push({ project: r.project, phase: r.phase, state: r.state, display: r.display });
  });
  out.recs = recs;

  /* 1000-row single group: cap + overflow control. */
  var items = [];
  for (var i = 0; i < 1000; i++) {
    items.push({ project: "big", id: "B-" + i, key: "big:B-" + i, title: "Bulk task " + i,
      kind: "lane", track: "P0", phase: "P0", owner: "o", state: (i % 2 ? "done" : "open"),
      estimate_h: 1, depends_on: [], done_criteria: "d", operator: false, priority: 0,
      evidence: [], updated_at: "2026-10-01T00:00:00Z" });
  }
  PlanUI.app.vm = PlanUI.buildViewModel({ items: items });
  PlanUI.TablePage.view.group = "project";
  PlanUI.TablePage.view.collapsed = {};
  PlanUI.TablePage.view.more = {};
  PlanUI.TablePage.render();
  var box2 = doc.getElementById("list-content");
  var secs = qsa(box2, ".tg");
  var more = qsa(box2, ".tg-more");
  out.cap = { groups: secs.length,
              tables: secs.length ? qsa(secs[0], "table").length : 0,
              rows: secs.length ? qsa(secs[0], "tbody tr").length : 0,
              moreText: more.length ? more[0].textContent : null };

  /* Hover preview: HTML-looking title must arrive as text. */
  var anchor = doc.createElement("button");
  doc.body.appendChild(anchor);
  PlanUI.Preview.show(anchor, { kind: "question", id: "Q-9", title: "<b>x</b>",
    text: "hello", status: "open", answer: null, project: "p", phase: "", item: "p:1" });
  var pop = doc.getElementById("pg-pop");
  out.preview = { text: pop ? pop.textContent : null,
                  bolds: pop ? qsa(pop, "b").length : -1,
                  hidden: pop ? !!pop.hidden : null };
  emit(out);
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    harness = tmp_path / "table_view_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), str(FIXTURE)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


def key_of(group: str, rec: dict) -> str:
    if group == "project":
        return rec["project"]
    if group == "phase":
        return rec["phase"]
    return rec["display"] or rec["state"]


def pct_of(done: int, live: int) -> int:
    if not live:
        return 0
    return int(math.floor(100 * done / live + 0.5))  # JS Math.round


def expected_groups(recs: list[dict], group: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in recs:
        groups.setdefault(key_of(group, r), []).append(r)
    exp = {}
    for k, rows in groups.items():
        live = [r for r in rows if r["state"] != "cancelled"]
        done = [r for r in live if r["state"] == "done"]
        exp[k] = {"n": len(rows), "done": len(done), "total": len(live),
                  "pct": pct_of(len(done), len(live))}
    return exp


@need_node
@pytest.mark.parametrize("group", ["project", "phase", "state"])
def test_group_gives_one_table_and_bar_with_counts_and_progress(tmp_path, group):
    out = run_node(tmp_path)
    exp = expected_groups(out["recs"], group)
    got = {s["name"]: s for s in out["groups"][group]}
    assert set(got) == set(exp), (group, sorted(got), sorted(exp))
    assert len(out["groups"][group]) == len(exp)
    for name, want in exp.items():
        sec = got[name]
        assert sec["tables"] == 1, (group, name)  # one table per group
        assert sec["bars"] == 1, (group, name)  # one progress bar per group
        assert sec["rows"] == want["n"], (group, name, sec["rows"], want["n"])
        assert str(want["n"]) in sec["cnt"], (group, name, sec["cnt"])
        assert sec["now"] == str(want["pct"]), (group, name, sec["now"])
        assert sec["pct"] == "%d/%d · %d%%" % (want["done"], want["total"], want["pct"])


@need_node
def test_max_rows_cap_truncates_with_overflow_row(tmp_path):
    out = run_node(tmp_path)
    cap = out["cap"]
    assert MAX_ROWS < 1000
    assert cap["groups"] == 1
    assert cap["tables"] == 1
    assert cap["rows"] == MAX_ROWS, (cap["rows"], MAX_ROWS)  # capped, not all 1000
    assert cap["moreText"] is not None, "overflow control missing"
    assert "1000" in cap["moreText"], cap["moreText"]  # names the hidden remainder


@need_node
def test_preview_chip_text_comes_from_text_content(tmp_path):
    out = run_node(tmp_path)
    pv = out["preview"]
    assert pv["hidden"] is False
    assert "<b>x</b>" in (pv["text"] or ""), pv["text"]  # literal text, not markup
    assert pv["bolds"] == 0, "title parsed as HTML"
