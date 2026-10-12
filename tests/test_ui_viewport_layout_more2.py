"""Tests for PlanUI viewport layout: M08, X01, X02, and hidden sheet variants (T3b-1fix6-STRENGTHEN)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


@need_node
def test_m08_focus_node_hidden_sheet_peek(tmp_path: Path):
    """M08: focusNode on phone with hidden sheet applies 120 px peek inset; desktop has no inset."""
    runner = tmp_path / "m08_runner.js"
    script = r"""
"use strict";
var animatedCalls = [];
var fakeNode = { length: 1, boundingBox: function () { return { x1: 100, y1: 200, w: 50, h: 50 }; }, select: function () {} };
var fakeCy = {
  _zoom: 1.0, _w: 1280, _h: 800,
  zoom: function () { return this._zoom; }, pan: function () { return { x: 0, y: 0 }; },
  width: function () { return this._w; }, height: function () { return this._h; },
  nodes: function () { return { unselect: function () {} }; }, elements: function () { return []; },
  getElementById: function (id) { return fakeNode; }, batch: function (fn) { fn(); },
  on: function () {}, style: function () {}, animate: function (opts) { animatedCalls.push(opts); }
};

function makeDoc(w, h) {
  return {
    getElementById: function (id) {
      if (id === "cy") return { textContent: "", getBoundingClientRect: function () { return { top: 0, bottom: h, left: 0, right: w, width: w, height: h }; } };
      if (id === "details") return {
        id: "details", className: "details bottom-sheet", hidden: true,
        getAttribute: function (k) { return k === "hidden" ? "" : null; },
        hasAttribute: function (k) { return k === "hidden"; },
        classList: { contains: function (c) { return c === "details" || c === "bottom-sheet"; } },
        getBoundingClientRect: function () { return { top: h, bottom: h + 200, left: 0, right: w, width: w, height: 200 }; }
      };
      return null;
    },
    querySelector: function () { return null; },
    createElement: function () { return { classList: { add: function(){} }, style: {}, appendChild: function(){}, setAttribute: function(){}, addEventListener: function(){} }; },
    documentElement: {}, body: { appendChild: function(){} }
  };
}

global.window = { innerWidth: 1280, matchMedia: function () { return { matches: false }; }, addEventListener: function () {}, PlanPage: {}, cytoscape: function () { return fakeCy; } };
global.getComputedStyle = function () { return { getPropertyValue: function () { return ""; } }; };
global.document = makeDoc(1280, 800);

require(process.argv[2]);
var PlanUI = require(process.argv[3]);
PlanUI.buildElements = function () { return { nodes: [], edges: [] }; };
PlanUI.LayoutService = { bumpEpoch: function() { return 1; }, getEpoch: function() { return 1; }, layoutProject: function() { return Promise.resolve(null); } };
PlanUI.stackProjects = function () { return null; };
PlanUI.revealPatch = function () { return null; };
PlanUI = require(process.argv[4]);
PlanUI.GraphView.mount({ get: function () { return {}; }, set: function () {}, subscribe: function () {}, patch: function () {} }, { projects: [], items: new Map() });

function runCase(isPhoneMode, cb) {
  animatedCalls = [];
  var w = isPhoneMode ? 390 : 1280, h = isPhoneMode ? 844 : 800, z = isPhoneMode ? 0.8 : 1.0;
  fakeCy._zoom = z; fakeCy._w = w; fakeCy._h = h;
  global.window.innerWidth = w;
  global.window.matchMedia = function (q) { return { matches: isPhoneMode }; };
  global.document = makeDoc(w, h);
  PlanUI.fitTarget = function () { return null; };
  global.window.PlanPage.focusNode("task1");
  setTimeout(function () { cb(animatedCalls.length ? animatedCalls[0] : null); }, 20);
}

runCase(false, function (resDesktop) {
  runCase(true, function (resPhone) {
    process.stdout.write(JSON.stringify({ desktop: resDesktop, phone: resPhone }));
  });
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page.js"), str(WEB / "page_graph_viewport.js"), str(WEB / "page_graph_view.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # Box: cx = 125, cy = 225
    # Desktop: w=1280, h=800, z=1.0, inset=0 -> pan x = 1280/2 - 125*1 = 515, pan y = 800/2 - 225*1 = 175
    d_pan = out["desktop"]["pan"]
    assert abs(d_pan["x"] - 515) < 1e-6
    assert abs(d_pan["y"] - 175) < 1e-6

    # Phone: w=390, h=844, z=0.8, inset={top:0, bottom:120, left:0, right:0}
    # pan x = 390/2 - 125*0.8 = 95, pan y = (844 - 120)/2 - 225*0.8 = 362 - 180 = 182
    p_pan = out["phone"]["pan"]
    assert abs(p_pan["x"] - 95) < 1e-6
    assert abs(p_pan["y"] - 182) < 1e-6


@need_node
def test_x01_sheet_inset_floor_120(tmp_path: Path):
    """X01: sheetInset floors bottom inset at 120 px on phone with visible sheet."""
    runner = tmp_path / "x01_runner.js"
    script = r"""
"use strict";
var PlanUI = require(process.argv[2]);
var win = { innerWidth: 390, matchMedia: function () { return { matches: true }; } };
var canvasH = 800;
var cyEl = { getBoundingClientRect: function () { return { top: 0, bottom: canvasH, left: 0, right: 390, width: 390, height: canvasH }; } };

function makeDocForSheet(sheetTop, sheetHeight) {
  var detEl = {
    id: "details", className: "details bottom-sheet", hidden: false,
    hasAttribute: function () { return false; }, getAttribute: function () { return null; },
    getBoundingClientRect: function () { return { top: sheetTop, bottom: sheetTop + sheetHeight, left: 0, right: 390, width: 390, height: sheetHeight }; }
  };
  return {
    defaultView: win,
    getElementById: function (id) {
      if (id === "cy") return cyEl;
      if (id === "details") return detEl;
      return null;
    }
  };
}

var cases = {
  h44: PlanUI.sheetInset(makeDocForSheet(canvasH - 44, 44), win),
  h120: PlanUI.sheetInset(makeDocForSheet(canvasH - 120, 120), win),
  h200: PlanUI.sheetInset(makeDocForSheet(canvasH - 200, 200), win),
  negativeOverlap: PlanUI.sheetInset(makeDocForSheet(canvasH + 50, 200), win)
};
process.stdout.write(JSON.stringify(cases));
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page_graph_viewport.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # Rect height 44 (top = 800 - 44 = 756): overlap 44, floored at 120
    assert out["h44"]["bottom"] == 120
    # Rect height 120 (top = 800 - 120 = 680): overlap 120, floored at 120
    assert out["h120"]["bottom"] == 120
    # Rect height 200 (top = 800 - 200 = 600): overlap 200, > 120
    assert out["h200"]["bottom"] == 200
    # Rect top below canvas bottom (overlap = -50): floored at 120
    assert out["negativeOverlap"]["bottom"] == 120

    for k in ["h44", "h120", "h200", "negativeOverlap"]:
        assert out[k]["top"] == 0
        assert out[k]["left"] == 0
        assert out[k]["right"] == 0


@need_node
def test_x02_is_phone_match_media_precedence(tmp_path: Path):
    """X02: isPhone in page_graph_view prefers matchMedia over innerWidth, falls back to innerWidth."""
    runner = tmp_path / "x02_runner.js"
    script = r"""
"use strict";
var animatedCalls = [];
var fakeNode = { length: 1, boundingBox: function () { return { x1: 100, y1: 200, w: 50, h: 50 }; }, select: function () {} };
var fakeCy = {
  _zoom: 1.0, _w: 1000, _h: 800,
  zoom: function () { return this._zoom; }, pan: function () { return { x: 0, y: 0 }; },
  width: function () { return this._w; }, height: function () { return this._h; },
  nodes: function () { return { unselect: function () {} }; }, elements: function () { return []; },
  getElementById: function () { return fakeNode; }, batch: function (fn) { fn(); },
  on: function () {}, style: function () {}, animate: function (opts) { animatedCalls.push(opts); }
};

global.window = { innerWidth: 1000, addEventListener: function () {}, PlanPage: {}, cytoscape: function () { return fakeCy; } };
global.getComputedStyle = function () { return { getPropertyValue: function () { return ""; } }; };
global.document = {
  getElementById: function (id) {
    if (id === "cy") return { textContent: "", getBoundingClientRect: function () { return { top: 0, bottom: 800, left: 0, right: 1000, width: 1000, height: 800 }; } };
    if (id === "details") return {
      id: "details", className: "details bottom-sheet", hidden: true,
      getAttribute: function (k) { return k === "hidden" ? "" : null; },
      hasAttribute: function (k) { return k === "hidden"; },
      classList: { contains: function (c) { return c === "details" || c === "bottom-sheet"; } },
      getBoundingClientRect: function () { return { top: 800, bottom: 1000, left: 0, right: 1000, width: 1000, height: 200 }; }
    };
    return null;
  },
  querySelector: function () { return null; },
  createElement: function () { return { classList: { add: function(){} }, style: {}, appendChild: function(){}, setAttribute: function(){}, addEventListener: function(){} }; },
  documentElement: {}, body: { appendChild: function(){} }
};

require(process.argv[2]);
var PlanUI = require(process.argv[3]);
PlanUI.buildElements = function () { return { nodes: [], edges: [] }; };
PlanUI.LayoutService = { bumpEpoch: function() { return 1; }, getEpoch: function() { return 1; }, layoutProject: function() { return Promise.resolve(null); } };
PlanUI.stackProjects = function () { return null; };
PlanUI.revealPatch = function () { return null; };
PlanUI = require(process.argv[4]);
PlanUI.GraphView.mount({ get: function () { return {}; }, set: function () {}, subscribe: function () {}, patch: function () {} }, { projects: [], items: new Map() });
PlanUI.fitTarget = function () { return null; };

function runCase(innerWidth, matchMediaFn, cb) {
  animatedCalls = [];
  global.window.innerWidth = innerWidth;
  if (matchMediaFn !== undefined) global.window.matchMedia = matchMediaFn;
  else delete global.window.matchMedia;

  global.window.PlanPage.focusNode("task1");
  setTimeout(function () { cb(animatedCalls.length ? animatedCalls[0] : null); }, 20);
}

runCase(1280, function () { return { matches: true }; }, function (resNarrowMQ) {
  runCase(390, function () { return { matches: false }; }, function (resWideMQ) {
    runCase(390, undefined, function (resFallbackPhone) {
      runCase(1280, undefined, function (resFallbackDesktop) {
        process.stdout.write(JSON.stringify({
          narrowMQ: resNarrowMQ,
          wideMQ: resWideMQ,
          fallbackPhone: resFallbackPhone,
          fallbackDesktop: resFallbackDesktop
        }));
      });
    });
  });
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page.js"), str(WEB / "page_graph_viewport.js"), str(WEB / "page_graph_view.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # Box: cx = 125, cy = 225, w = 1000, h = 800, z = 1.0
    # Phone pan.y = (800 - 120)/2 - 225*1 = 340 - 225 = 115
    # Desktop pan.y = 800/2 - 225*1 = 400 - 225 = 175
    # pan.x = 1000/2 - 125 = 375 for both

    # 1. innerWidth 1280 + matchMedia true behaves as PHONE
    assert abs(out["narrowMQ"]["pan"]["x"] - 375) < 1e-6
    assert abs(out["narrowMQ"]["pan"]["y"] - 115) < 1e-6

    # 2. innerWidth 390 + matchMedia false behaves as DESKTOP
    assert abs(out["wideMQ"]["pan"]["x"] - 375) < 1e-6
    assert abs(out["wideMQ"]["pan"]["y"] - 175) < 1e-6

    # 3. innerWidth 390 + no matchMedia falls back to PHONE
    assert abs(out["fallbackPhone"]["pan"]["x"] - 375) < 1e-6
    assert abs(out["fallbackPhone"]["pan"]["y"] - 115) < 1e-6

    # 4. innerWidth 1280 + no matchMedia falls back to DESKTOP
    assert abs(out["fallbackDesktop"]["pan"]["x"] - 375) < 1e-6
    assert abs(out["fallbackDesktop"]["pan"]["y"] - 175) < 1e-6


@need_node
def test_extra_sheet_inset_hidden_checks(tmp_path: Path):
    """Extra: sheetInset detects hidden status independently via hidden property or hidden attribute.

    Proves each check works alone:
    1. hidden property true, hasAttribute absent -> inset 0
    2. hidden property false, hasAttribute('hidden') true -> inset 0
    3. hidden property absent/undefined, hasAttribute('hidden') true -> inset 0
    4. both false -> sheet is active, bottom inset = 200
    """
    runner = tmp_path / "extra_runner.js"
    script = r"""
"use strict";
var PlanUI = require(process.argv[2]);
var win = { innerWidth: 390, matchMedia: function () { return { matches: true }; } };
var cyEl = { getBoundingClientRect: function () { return { top: 0, bottom: 800, left: 0, right: 390, width: 390, height: 800 }; } };
function docFor(detEl) {
  return {
    defaultView: win,
    getElementById: function (id) {
      if (id === "cy") return cyEl;
      if (id === "details") return detEl;
      return null;
    }
  };
}
var rect = { top: 600, bottom: 800, left: 0, right: 390, width: 390, height: 200 };

var results = {
  propOnly: PlanUI.sheetInset(docFor({ id: "details", className: "details bottom-sheet", hidden: true, getBoundingClientRect: function () { return rect; } }), win),
  attrOnly: PlanUI.sheetInset(docFor({ id: "details", className: "details bottom-sheet", hidden: false, hasAttribute: function (k) { return k === "hidden"; }, getBoundingClientRect: function () { return rect; } }), win),
  attrOnlyNoProp: PlanUI.sheetInset(docFor({ id: "details", className: "details bottom-sheet", hasAttribute: function (k) { return k === "hidden"; }, getBoundingClientRect: function () { return rect; } }), win),
  visible: PlanUI.sheetInset(docFor({ id: "details", className: "details bottom-sheet", hidden: false, hasAttribute: function () { return false; }, getBoundingClientRect: function () { return rect; } }), win)
};
process.stdout.write(JSON.stringify(results));
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page_graph_viewport.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert out["propOnly"]["bottom"] == 0
    assert out["propOnly"]["top"] == 0
    assert out["attrOnly"]["bottom"] == 0
    assert out["attrOnly"]["top"] == 0
    assert out["attrOnlyNoProp"]["bottom"] == 0
    assert out["attrOnlyNoProp"]["top"] == 0

    assert out["visible"]["bottom"] == 200
    assert out["visible"]["top"] == 0
