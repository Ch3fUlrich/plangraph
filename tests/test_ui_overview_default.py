"""Tests for the Graph page default project overview (W1).

Fresh load (no collapsedPhases / collapsedProjects state) must seed the
project overview (every project and phase collapsed) and fit all visible
elements; the style must not map data(width)/data(height) on unguarded
selectors.
"""

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


def _run(runner: Path, args: list[str]) -> dict:
    proc = subprocess.run(
        ["node", str(runner)] + args,
        capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_fresh_state_seeds_project_overview(tmp_path: Path):
    """Mounting with empty collapse state collapses every project and phase."""
    runner = tmp_path / "overview_seed_runner.js"
    script = r"""
"use strict";
function mkEl() {
  return {
    classList: { add: function () {} }, style: {},
    appendChild: function () {}, setAttribute: function () {},
    addEventListener: function () {}, hidden: false, textContent: ""
  };
}
var fakeCy = {
  resize: function () {}, width: function () { return 1440; }, height: function () { return 900; },
  zoom: function (z) { if (z !== undefined) this._z = z; return this._z || 0.8; },
  pan: function (p) { if (p !== undefined) this._p = p; return this._p || { x: 0, y: 0 }; },
  elements: function () { return { length: 0, forEach: function () {} }; },
  nodes: function () { return { forEach: function () {}, unselect: function () {} }; },
  batch: function (fn) { fn(); }, remove: function () {}, add: function () {},
  on: function () {}, style: function () {},
  getElementById: function () { return { length: 0 }; }
};
global.window = {
  innerWidth: 1440,
  matchMedia: function () { return { matches: false }; },
  addEventListener: function () {}, PlanPage: {},
  cytoscape: function () { return fakeCy; }
};
global.getComputedStyle = function () { return { getPropertyValue: function () { return ""; } }; };
global.document = {
  getElementById: function (id) { if (id === "cy") return mkEl(); return null; },
  querySelector: function () { return null; },
  createElement: function () { return mkEl(); },
  documentElement: {}, body: { appendChild: function () {} }
};
require(process.argv[2]);
require(process.argv[3]);
require(process.argv[4]);
require(process.argv[5]);
require(process.argv[6]);
var PlanUI = global.window.PlanUI || global.PlanUI;
PlanUI.LayoutService = { bumpEpoch: function () { return 1; }, getEpoch: function () { return 1; }, layoutProject: function () { return Promise.resolve(null); } };
PlanUI.layoutSignature = function () { return "seed"; };
PlanUI.stackProjects = function () { return {}; };

function makeStore() {
  return {
    _d: {},
    get: function (k) { if (k === undefined) return this._d; return this._d[k]; },
    set: function (k, v) { this._d[k] = v; },
    patch: function (o) { for (var k in o) this._d[k] = o[k]; },
    subscribe: function () {}
  };
}
var vm = {
  projects: [
    { name: "pA", label: "PA", color: "#111", phases: [{ name: "x", total: 1, done: 0, keys: [] }, { name: "y", total: 2, done: 1, keys: [] }] },
    { name: "pB", label: "PB", color: "#222", phases: [{ name: "z", total: 3, done: 3, keys: [] }] }
  ],
  items: new Map()
};
var ui = makeStore();
PlanUI.GraphView.mount(ui, vm);
function arr(v) { return v ? Array.from(v).sort() : null; }
process.stdout.write(JSON.stringify({
  collapsedProjects: arr(ui._d.collapsedProjects),
  collapsedPhases: arr(ui._d.collapsedPhases),
  hasInitialCollapsed: typeof PlanUI.initialCollapsed === "function",
  hasOverviewState: typeof PlanUI.overviewState === "function"
}));
"""
    runner.write_text(script, encoding="utf-8")
    out = _run(runner, [
        str(WEB / "page.js"), str(WEB / "page_graph.js"),
        str(WEB / "page_graph_cluster.js"), str(WEB / "page_graph_viewport.js"),
        str(WEB / "page_graph_view.js"),
    ])
    assert out["hasOverviewState"] is True
    assert out["hasInitialCollapsed"] is True
    assert out["collapsedProjects"] == ["pA", "pB"]
    assert out["collapsedPhases"] == ["pA|x", "pA|y", "pB|z"]


@need_node
def test_initial_fits_all_on_fresh_overview(tmp_path: Path):
    """initial() with the overview state calls fit over all elements, zoom <= 1."""
    runner = tmp_path / "overview_fit_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
require(process.argv[4]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

function makeCy(w, h) {
  var eles = {
    length: 7,
    boundingBox: function () { return { x1: 0, y1: 0, w: 600, h: 200, x2: 600, y2: 200 }; }
  };
  var calls = [], _z = 0.75, _pan = { x: 0, y: 0 };
  var cy = {
    resize: function () {},
    width: function () { return w; },
    height: function () { return h; },
    elements: function () { return eles; },
    fit: function (e, pad) { calls.push({ n: e && e.length, pad: pad }); _z = 2.5; },
    zoom: function (z) { if (z !== undefined) _z = z; return _z; },
    pan: function (p) { if (p !== undefined) _pan = p; return _pan; },
    getElementById: function () { return { length: 0 }; }
  };
  return { cy: cy, calls: calls };
}
var vm = { projects: [{ name: "a", phases: [{ name: "p1" }] }, { name: "b", phases: [{ name: "p2" }] }] };
function overviewUI() {
  return {
    focusProject: null,
    collapsedProjects: new Set(["a", "b"]),
    collapsedPhases: new Set(["a|p1", "b|p2"])
  };
}
var d = makeCy(1440, 900);
var resD = PlanUI.GraphViewport.initial(d.cy, vm, overviewUI(), false);
var p = makeCy(390, 844);
var resP = PlanUI.GraphViewport.initial(p.cy, vm, overviewUI(), true);
emit({
  desktopFit: d.calls, desktopZoom: d.cy.zoom(), desktopResZoom: resD.zoom,
  phoneFit: p.calls, phoneZoom: p.cy.zoom(), phoneResZoom: resP.zoom
});
"""
    runner.write_text(script, encoding="utf-8")
    out = _run(runner, [str(WEB / "page.js"), str(WEB / "page_graph_viewport.js"), str(WEB / "page_graph_pan.js")])
    for key in ("desktopFit", "phoneFit"):
        assert len(out[key]) == 1
        assert out[key][0]["n"] == 7
        assert 20 <= out[key][0]["pad"] <= 28
    assert out["desktopZoom"] <= 1.0
    assert out["phoneZoom"] <= 1.0
    assert out["desktopResZoom"] <= 1.0
    assert out["phoneResZoom"] <= 1.0


@need_node
def test_initial_keeps_anchor_with_focus_or_expanded(tmp_path: Path):
    """A focusProject or an expanded project keeps the anchored behaviour (no fit)."""
    runner = tmp_path / "overview_anchor_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
require(process.argv[4]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

function makeCy() {
  var calls = [], _z = 1, _pan = { x: 0, y: 0 };
  var cy = {
    resize: function () {},
    width: function () { return 1440; },
    height: function () { return 900; },
    elements: function () { return { length: 0 }; },
    fit: function () { calls.push(true); },
    zoom: function (z) { if (z !== undefined) _z = z; return _z; },
    pan: function (p) { if (p !== undefined) _pan = p; return _pan; },
    getElementById: function (id) {
      if (id === "p:a") {
        return { length: 1, boundingBox: function () { return { x1: 10, y1: 20, w: 200, h: 100, x2: 210, y2: 120 }; } };
      }
      return { length: 0 };
    }
  };
  return { cy: cy, calls: calls };
}
var vm = { projects: [{ name: "a", phases: [{ name: "p1" }] }, { name: "b", phases: [{ name: "p2" }] }] };
var focusUI = { focusProject: "a", collapsedProjects: new Set(["a", "b"]), collapsedPhases: new Set(["a|p1", "b|p2"]) };
var expandedUI = { focusProject: null, collapsedProjects: new Set(["a"]), collapsedPhases: new Set() };

var f = makeCy();
var resF = PlanUI.GraphViewport.initial(f.cy, vm, focusUI, false);
var e = makeCy();
var resE = PlanUI.GraphViewport.initial(e.cy, vm, expandedUI, false);
emit({
  focusFitCalls: f.calls.length, focusZoom: f.cy.zoom(), focusBox: resF.box,
  expandedFitCalls: e.calls.length, expandedZoom: e.cy.zoom(), expandedBox: resE.box
});
"""
    runner.write_text(script, encoding="utf-8")
    out = _run(runner, [str(WEB / "page.js"), str(WEB / "page_graph_viewport.js"), str(WEB / "page_graph_pan.js")])
    assert out["focusFitCalls"] == 0
    assert out["expandedFitCalls"] == 0
    assert 0.75 <= out["focusZoom"] <= 0.9
    assert 0.75 <= out["expandedZoom"] <= 0.9
    assert out["focusBox"]["w"] == 200
    assert out["expandedBox"]["w"] == 200


def test_view_seeds_overview_state_on_fresh_load():
    """page_graph_view.js seeds overviewState at project level on a fresh load."""
    src = (WEB / "page_graph_view.js").read_text(encoding="utf-8")
    compact = " ".join(src.split())
    assert 'overviewState(currentVM, { level: "project" })' in compact
    assert "PlanUI.initialCollapsed" in (ROOT / "src" / "plangraph" / "web" / "page_graph.js").read_text(encoding="utf-8")


def test_style_has_no_unguarded_data_size_mapping(tmp_path: Path):
    """Width/height data mappings only appear on selectors requiring that data."""
    runner = tmp_path / "overview_style_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
require(process.argv[3]);
require(process.argv[4]);
var PlanUI = global.PlanUI;
function emit(o) { process.stdout.write(JSON.stringify(o)); }
var rules = PlanUI.graphStyleRules({ cssVar: function () { return ""; }, isPhone: function () { return false; } });
emit(rules.map(function (r) { return { selector: r.selector, width: r.style.width, height: r.style.height }; }));
"""
    runner.write_text(script, encoding="utf-8")
    out = _run(runner, [
        str(WEB / "page.js"), str(WEB / "page_graph_viewport.js"), str(WEB / "page_graph_ext.js"),
    ])
    by_sel = {r["selector"]: r for r in out}
    assert by_sel["node"].get("width") != "data(width)"
    assert by_sel["node"].get("height") != "data(height)"
    assert by_sel["edge"].get("width") != "data(width)"
    assert by_sel["node[width][height]"]["width"] == "data(width)"
    assert by_sel["node[width][height]"]["height"] == "data(height)"
    assert by_sel["edge[width]"]["width"] == "data(width)"
    for r in out:
        if r.get("width") == "data(width)":
            assert "[width]" in r["selector"], r["selector"]
        if r.get("height") == "data(height)":
            assert "[height]" in r["selector"], r["selector"]
