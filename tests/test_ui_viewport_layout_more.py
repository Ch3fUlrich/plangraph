"""More tests for PlanUI viewport layout, __planViewport, focusNode, and styles (T3b-1fix6)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


@need_node
def test_plan_viewport_reader(tmp_path: Path):
    """T3: __planViewport reader with fake cy and real markup DOM.

    sheetTop is null on desktop (visible or hidden) and when hidden on phone;
    a number on phone with a visible sheet;
    selectedAboveSheet true when sheetTop is null;
    false when node box bottom is below sheet top.
    """
    runner = tmp_path / "plan_viewport_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

function makeEl(tag, id, cls, hidden, rect) {
  var classes = (cls || "").split(/\s+/).filter(Boolean);
  return {
    tagName: tag.toUpperCase(), id: id, className: cls || "", hidden: !!hidden,
    getAttribute: function (k) { return k === "hidden" ? (hidden ? "" : null) : null; },
    hasAttribute: function (k) { return k === "hidden" ? !!hidden : false; },
    classList: { contains: function (c) { return classes.indexOf(c) >= 0; } },
    getBoundingClientRect: function () { return rect || { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; }
  };
}

function makeDoc(opts) {
  var width = opts.width || 1280;
  var detailsHidden = opts.detailsHidden !== undefined ? opts.detailsHidden : false;
  var sheetTop = opts.sheetTop !== undefined ? opts.sheetTop : 600;
  var sheetHeight = opts.sheetHeight !== undefined ? opts.sheetHeight : 200;
  var win = {
    innerWidth: width,
    matchMedia: function (q) { return { matches: width <= 700 }; }
  };
  var detEl = makeEl("aside", "details", "details bottom-sheet", detailsHidden,
    { top: sheetTop, bottom: sheetTop + sheetHeight, left: 0, right: width, width: width, height: sheetHeight });
  var cyEl = makeEl("div", "cy", "cy", false, { top: 0, bottom: 800, left: 0, right: width, width: width, height: 800 });
  var doc = {
    defaultView: win,
    getElementById: function (id) {
      if (id === "details") return detEl;
      if (id === "cy") return cyEl;
      return null;
    }
  };
  return { doc: doc, win: win };
}

function runViewportTest(doc, win, nodeY2) {
  global.document = doc;
  global.window = win;
  var selNode = {
    length: 1,
    boundingBox: function () { return { x1: 50, y1: 50, x2: 150, y2: nodeY2 }; },
    renderedBoundingBox: function () { return { x1: 50, y1: 50, x2: 150, y2: nodeY2 }; },
    style: function () { return null; }
  };
  var fakeCy = {
    zoom: function () { return 1; },
    pan: function () { return { x: 0, y: 0 }; },
    extent: function () { return { x1: 0, y1: 0, x2: 1000, y2: 800 }; },
    nodes: function (sel) {
      if (sel === ":selected") {
        return {
          first: function () { return selNode; },
          forEach: function (fn) { fn(selNode); }
        };
      }
      return {
        first: function () { return null; },
        forEach: function () {}
      };
    }
  };
  PlanUI.GraphViewport.installHook(fakeCy);
  return window.__planViewport();
}

var d1 = makeDoc({ width: 1280, detailsHidden: false, sheetTop: 400 });
var v1 = runViewportTest(d1.doc, d1.win, 500);

var d2 = makeDoc({ width: 1280, detailsHidden: true, sheetTop: 400 });
var v2 = runViewportTest(d2.doc, d2.win, 500);

var d3 = makeDoc({ width: 390, detailsHidden: true, sheetTop: 600 });
var v3 = runViewportTest(d3.doc, d3.win, 500);

var d4 = makeDoc({ width: 390, detailsHidden: false, sheetTop: 600 });
var v4 = runViewportTest(d4.doc, d4.win, 500);

var d5 = makeDoc({ width: 390, detailsHidden: false, sheetTop: 600 });
var v5 = runViewportTest(d5.doc, d5.win, 700);

emit({
  desktop_visible: { sheetTop: v1.sheetTop, selectedAboveSheet: v1.selectedAboveSheet },
  desktop_hidden: { sheetTop: v2.sheetTop, selectedAboveSheet: v2.selectedAboveSheet },
  phone_hidden: { sheetTop: v3.sheetTop, selectedAboveSheet: v3.selectedAboveSheet },
  phone_visible_above: { sheetTop: v4.sheetTop, selectedAboveSheet: v4.selectedAboveSheet },
  phone_visible_below: { sheetTop: v5.sheetTop, selectedAboveSheet: v5.selectedAboveSheet }
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = ["node", str(runner), str(WEB / "page.js"), str(WEB / "page_graph_viewport.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert out["desktop_visible"]["sheetTop"] is None
    assert out["desktop_visible"]["selectedAboveSheet"] is True

    assert out["desktop_hidden"]["sheetTop"] is None
    assert out["desktop_hidden"]["selectedAboveSheet"] is True

    assert out["phone_hidden"]["sheetTop"] is None
    assert out["phone_hidden"]["selectedAboveSheet"] is True

    assert out["phone_visible_above"]["sheetTop"] == 600
    assert out["phone_visible_above"]["selectedAboveSheet"] is True

    assert out["phone_visible_below"]["sheetTop"] == 600
    assert out["phone_visible_below"]["selectedAboveSheet"] is False


@need_node
def test_focus_node(tmp_path: Path):
    """T4: focusNode through node with a fake cy recording pan/zoom it is asked for.

    desktop (1280, sheet visible with the class) => the same pan as the no-inset formula
    for cluster branch AND non-cluster branch;
    phone with a visible sheet => the pan reflects the inset in both branches.
    """
    runner = tmp_path / "focus_node_runner.js"
    script = r"""
"use strict";
var animatedCalls = [];
var fakeNode = {
  length: 1,
  boundingBox: function () { return { x1: 100, y1: 200, w: 50, h: 50 }; },
  select: function () {}
};
var fakeCy = {
  _zoom: 1.0,
  _w: 1280,
  _h: 800,
  zoom: function () { return this._zoom; },
  pan: function () { return { x: 0, y: 0 }; },
  width: function () { return this._w; },
  height: function () { return this._h; },
  nodes: function () { return { unselect: function () {} }; },
  elements: function () { return []; },
  getElementById: function (id) { return fakeNode; },
  batch: function (fn) { fn(); },
  on: function () {},
  style: function () {},
  animate: function (opts) { animatedCalls.push(opts); }
};

global.window = {
  innerWidth: 1280,
  matchMedia: function () { return { matches: false }; },
  addEventListener: function () {}
};
global.window.PlanPage = {};
global.window.cytoscape = function () { return fakeCy; };
global.getComputedStyle = function () { return { getPropertyValue: function () { return ""; } }; };
global.document = {
  getElementById: function (id) {
    if (id === "cy") return { textContent: "", getBoundingClientRect: function () { return { top: 0, bottom: fakeCy._h }; } };
    if (id === "details") return { className: "details bottom-sheet", hidden: false, getBoundingClientRect: function () { return { top: 400, bottom: fakeCy._h }; } };
    return null;
  },
  querySelector: function () { return null; },
  createElement: function () { return { classList: { add: function(){} }, style: {}, appendChild: function(){}, setAttribute: function(){}, addEventListener: function(){} }; },
  documentElement: {},
  body: { appendChild: function(){} }
};

require(process.argv[2]);
var PlanUI = require(process.argv[3]);
PlanUI.buildElements = function () { return { nodes: [], edges: [] }; };
PlanUI.LayoutService = { bumpEpoch: function() { return 1; }, getEpoch: function() { return 1; }, layoutProject: function() { return Promise.resolve(null); } };
PlanUI.stackProjects = function () { return null; };
PlanUI.revealPatch = function () { return null; };
PlanUI = require(process.argv[4]);

var fakeUI = { get: function () { return {}; }, set: function () {}, subscribe: function () {}, patch: function () {} };
var fakeVM = { projects: [], items: new Map() };

PlanUI.GraphView.mount(fakeUI, fakeVM);

function runCase(isPhoneMode, hasFitTarget, cb) {
  animatedCalls = [];
  fakeCy._zoom = isPhoneMode ? 0.8 : 1.0;
  fakeCy._w = isPhoneMode ? 390 : 1280;
  fakeCy._h = isPhoneMode ? 844 : 800;
  global.window.innerWidth = fakeCy._w;
  global.window.matchMedia = function () { return { matches: isPhoneMode }; };

  global.document.getElementById = function (id) {
    if (id === "cy") return { textContent: "", getBoundingClientRect: function () { return { top: 0, bottom: fakeCy._h }; } };
    if (id === "details") {
      var sTop = isPhoneMode ? 644 : 400;
      return { className: "details bottom-sheet", hidden: false, getBoundingClientRect: function () { return { top: sTop, bottom: fakeCy._h }; } };
    }
    return null;
  };

  if (hasFitTarget) {
    PlanUI.fitTarget = function () { return { x: 100, y: 200, w: 50, h: 50 }; };
  } else {
    PlanUI.fitTarget = function () { return null; };
  }

  global.window.PlanPage.focusNode("task1");
  setTimeout(function () {
    cb(animatedCalls.length ? animatedCalls[0] : null);
  }, 20);
}

runCase(false, false, function (resDesktopNonCluster) {
  runCase(false, true, function (resDesktopCluster) {
    runCase(true, false, function (resPhoneNonCluster) {
      runCase(true, true, function (resPhoneCluster) {
        process.stdout.write(JSON.stringify({
          desktopNonCluster: resDesktopNonCluster,
          desktopCluster: resDesktopCluster,
          phoneNonCluster: resPhoneNonCluster,
          phoneCluster: resPhoneCluster
        }));
      });
    });
  });
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
        str(WEB / "page_graph_view.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # Box: cx = 125, cy = 225
    # Desktop no-inset formula: z = 1.0, w = 1280, h = 800
    # expected pan x = 1280/2 - 125*1 = 515
    # expected pan y = 800/2 - 225*1 = 175
    d_nc = out["desktopNonCluster"]["pan"]
    d_c = out["desktopCluster"]["pan"]
    assert abs(d_nc["x"] - 515) < 1e-6
    assert abs(d_nc["y"] - 175) < 1e-6
    assert abs(d_c["x"] - 515) < 1e-6
    assert abs(d_c["y"] - 175) < 1e-6

    # Phone with visible sheet: z = 0.8, w = 390, h = 844, sheetTop = 644 => bottom inset 200
    # expected pan x = 390/2 - 125*0.8 = 195 - 100 = 95
    # expected pan y = (844 - 200)/2 - 225*0.8 = 322 - 180 = 142
    p_nc = out["phoneNonCluster"]["pan"]
    p_c = out["phoneCluster"]["pan"]
    assert abs(p_nc["x"] - 95) < 1e-6
    assert abs(p_nc["y"] - 142) < 1e-6
    assert abs(p_c["x"] - 95) < 1e-6
    assert abs(p_c["y"] - 142) < 1e-6


def test_style_fields():
    """T5: Collapsed-node style in page_graph_view.js.

    The style builder is not exported on PlanUI/PP (built internally in initGraphView).
    We assert on the source text of the exact style rule:
    - text-max-width is 10000
    - label measurer limit is 272
    """
    view_src = (WEB / "page_graph_view.js").read_text(encoding="utf-8")
    lines = [line for line in view_src.splitlines() if "node.phase-collapsed, node.project-collapsed" in line]
    assert len(lines) == 1, "Expected single style line for collapsed node selector"
    rule_line = lines[0]

    # Verify text-max-width is 10000
    assert re.search(r'"text-max-width":\s*10000\b', rule_line) is not None, "text-max-width must be 10000"

    # Verify label measurer limit is 272
    assert re.search(r'wrapLabel\([^,]+,\s*272\b', rule_line) is not None, "measurer limit must be 272"
