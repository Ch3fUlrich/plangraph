"""W6 sidebar resize — node-run tests for the drag-resize handle (#sb-resize)
in page_pages.js (PlanUI.Pages).

Dragging the handle changes the sidebar width, clamped to the source's
SBW_MIN/SBW_MAX; the width is written back to localStorage (asserted only
when the implementation persists it, per the task contract).

Skips cleanly when node is missing, like tests/test_ui_pure.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

sys.path.insert(0, str(SRC))

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

SOURCE = (WEB / "page_pages.js").read_text(encoding="utf-8")
SBW_MIN = int(re.search(r"SBW_MIN\s*=\s*(\d+)", SOURCE).group(1))
SBW_MAX = int(re.search(r"SBW_MAX\s*=\s*(\d+)", SOURCE).group(1))
SBW_KEY = re.search(r'SBW_KEY\s*=\s*"([^"]+)"', SOURCE).group(1)

HARNESS = r"""
"use strict";
var WEB = process.argv[2];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var ids = {};
function El(tag) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this.attributes = {};
  this.listeners = {};
  this._style = {};
  this.className = "";
  this.hidden = false;
  var self = this;
  this.style = {
    setProperty: function (k, v) { self._style[k] = String(v); },
    getPropertyValue: function (k) { return self._style[k] || ""; }
  };
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
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.fire = function (t, ev) {
  var f = this.listeners[t] || [];
  for (var i = 0; i < f.length; i++) f[i](ev || { type: t });
};
El.prototype.setPointerCapture = function () {};
El.prototype.closest = function () { return null; };
var clsSet = {};
El.prototype.classList = {
  add: function (c) { clsSet[c] = 1; },
  remove: function (c) { delete clsSet[c]; },
  toggle: function (c, f) { if (f) clsSet[c] = 1; else delete clsSet[c]; },
  contains: function (c) { return !!clsSet[c]; }
};
Object.defineProperty(El.prototype, "id", {
  get: function () { return this.attributes.id || ""; },
  set: function (v) { this.setAttribute("id", String(v)); }
});

var htmlEl = new El("html");
var body = new El("body");
htmlEl.appendChild(body);
var doc = {
  nodeType: 9,
  body: body,
  documentElement: htmlEl,
  createElement: function (t) { return new El(t); },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function () { return []; },
  querySelector: function () { return null; },
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

function add(parent, tag, cls, attrs) {
  var n = new El(tag);
  if (cls) n.className = cls;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}
var sb = add(body, "aside", "sb", [["id", "sidebar"]]);
var h = add(body, "div", "", [["id", "sb-resize"]]);
h.setAttribute("role", "separator");

global.PlanPage = { state: {}, setTab: function () {} };

/* Mirror the browser: the sidebar's layout width tracks --sb-w. */
var curW = 260;
sb.getBoundingClientRect = function () {
  return { width: curW, left: 0, top: 0, bottom: 0, right: curW, height: 0 };
};
var rawSet = htmlEl.style.setProperty;
htmlEl.style.setProperty = function (k, v) {
  rawSet.call(htmlEl.style, k, v);
  var m = /^(\d+)px$/.exec(String(v));
  if (k === "--sb-w" && m) curW = parseInt(m[1], 10);
};
function width() {
  var m = /^(\d+)px$/.exec(String(htmlEl.style.getPropertyValue("--sb-w") || ""));
  return m ? parseInt(m[1], 10) : null;
}

function main() {
  require(WEB + "/page.js");
  var PlanUI = require(WEB + "/page_model.js");
  require(WEB + "/page_store.js");
  require(WEB + "/page_pages.js");
  win.PlanPage = global.PlanPage;
  win.localStorage = global.localStorage;

  var ui = new PlanUI.Store({});
  PlanUI.Pages.mount(ui);
  var out = { hasHandle: !!doc.getElementById("sb-resize") };
  var hEl = doc.getElementById("sb-resize");

  function drag(x0, x1) {
    hEl.fire("pointerdown", { type: "pointerdown", clientX: x0, pointerId: 1,
                              preventDefault: function () {} });
    hEl.fire("pointermove", { type: "pointermove", clientX: x1, pointerId: 1 });
    var w = width();
    hEl.fire("pointerup", { type: "pointerup", clientX: x1, pointerId: 1 });
    return { atMove: w, atUp: width() };
  }

  out.small = drag(100, 140);          // +40px from the 260px stub layout
  out.persisted = global.localStorage.getItem("plangraph.ui.sbw.v1");
  out.toMax = drag(100, 5000);         // far right: must stop at SBW_MAX
  out.toMin = drag(5000, -5000);       // far left: must stop at SBW_MIN
  out.persistedEnd = global.localStorage.getItem("plangraph.ui.sbw.v1");
  out.aria = { now: hEl.getAttribute("aria-valuenow"),
               min: hEl.getAttribute("aria-valuemin"),
               max: hEl.getAttribute("aria-valuemax") };
  emit(out);
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run_node(tmp_path: Path) -> dict:
    harness = tmp_path / "sidebar_resize_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cmd = ["node", str(harness), str(WEB)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


@need_node
def test_drag_changes_width_and_persists(tmp_path):
    out = run_node(tmp_path)
    assert out["hasHandle"] is True
    assert out["small"]["atMove"] == 300, out["small"]  # 260px layout + 40px drag
    assert out["small"]["atUp"] == 300, out["small"]
    if out["persisted"] is not None:  # persisted iff the code persists it
        assert out["persisted"] == "300", out["persisted"]


@need_node
def test_drag_is_clamped_to_min_max(tmp_path):
    out = run_node(tmp_path)
    assert out["toMax"]["atMove"] == SBW_MAX, out["toMax"]
    assert out["toMax"]["atUp"] == SBW_MAX, out["toMax"]
    assert out["toMin"]["atMove"] == SBW_MIN, out["toMin"]
    assert out["toMin"]["atUp"] == SBW_MIN, out["toMin"]
    assert out["aria"] == {"now": str(SBW_MIN), "min": str(SBW_MIN),
                           "max": str(SBW_MAX)}, out["aria"]
    if out["persistedEnd"] is not None:
        assert out["persistedEnd"] == str(SBW_MIN), out["persistedEnd"]
