"""Tests for PlanUI.viewportFor — pure viewport math (T3b-1fix)."""

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

RUNNER_JS = r"""
"use strict";
var mode = process.argv[2];
require(process.argv[3]);
var PlanUI = require(process.argv[4]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

if (mode === "calc") {
  var box = JSON.parse(process.argv[5]);
  var size = JSON.parse(process.argv[6]);
  var phone = JSON.parse(process.argv[7]);
  emit(PlanUI.viewportFor(box, size, phone));
} else if (mode === "batch") {
  var cases = JSON.parse(process.argv[5]);
  var results = cases.map(function (c) {
    return PlanUI.viewportFor(c.box, c.size, c.phone);
  });
  emit(results);
}
"""


def run_node(tmp_path: Path, mode: str, *args) -> dict | list:
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    cmd = [
        "node",
        str(runner_path),
        mode,
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_box_that_fits_gives_desktop_cap(tmp_path):
    """A box that fits inside the desktop canvas gives zoom 0.9 (desktop cap)."""
    box = {"x1": 10, "y1": 20, "w": 200, "h": 100}
    size = {"w": 1000, "h": 800}
    res = run_node(tmp_path, "calc", json.dumps(box), json.dumps(size), "false")
    assert res["zoom"] == 0.9
    assert res["pan"]["x"] == 24 - 10 * 0.9
    assert res["pan"]["y"] == 24 - 20 * 0.9


@need_node
def test_huge_box_gives_desktop_floor(tmp_path):
    """A huge box gives zoom 0.75 (desktop floor)."""
    box = {"x1": 0, "y1": 0, "w": 5000, "h": 4000}
    size = {"w": 1000, "h": 800}
    res = run_node(tmp_path, "calc", json.dumps(box), json.dumps(size), "false")
    assert res["zoom"] == 0.75
    assert res["pan"]["x"] == 24
    assert res["pan"]["y"] == 24


@need_node
def test_phone_always_0_8(tmp_path):
    """Phone viewport always uses zoom 0.8 regardless of box size."""
    cases = [
        {"box": {"x1": 0, "y1": 0, "w": 100, "h": 100}, "size": {"w": 390, "h": 844}, "phone": True},
        {"box": {"x1": 50, "y1": 100, "w": 350, "h": 600}, "size": {"w": 390, "h": 844}, "phone": True},
        {"box": {"x1": 0, "y1": 0, "w": 5000, "h": 4000}, "size": {"w": 390, "h": 844}, "phone": True},
    ]
    results = run_node(tmp_path, "batch", json.dumps(cases))
    for r in results:
        assert r["zoom"] == 0.8


@need_node
def test_pan_always_puts_box_top_left_at_rendered_24_24(tmp_path):
    """The pan always places the box top-left at rendered (24, 24)."""
    cases = [
        {"box": {"x1": 40, "y1": 80, "w": 300, "h": 200}, "size": {"w": 1440, "h": 900}, "phone": False},
        {"box": {"x1": 100, "y1": 250, "w": 3000, "h": 2000}, "size": {"w": 1440, "h": 900}, "phone": False},
        {"box": {"x1": 25, "y1": 50, "w": 350, "h": 500}, "size": {"w": 390, "h": 844}, "phone": True},
    ]
    results = run_node(tmp_path, "batch", json.dumps(cases))
    for c, r in zip(cases, results):
        box = c["box"]
        zoom = r["zoom"]
        rendered_x = box["x1"] * zoom + r["pan"]["x"]
        rendered_y = box["y1"] * zoom + r["pan"]["y"]
        assert abs(rendered_x - 24) < 1e-6
        assert abs(rendered_y - 24) < 1e-6


@need_node
def test_zero_size_box_does_not_divide_by_zero(tmp_path):
    """A zero-size box does not divide by zero; it returns the cap zoom."""
    zero_box = {"x1": 0, "y1": 0, "w": 0, "h": 0}
    d_res = run_node(tmp_path, "calc", json.dumps(zero_box), json.dumps({"w": 1000, "h": 800}), "false")
    assert d_res["zoom"] == 0.9
    assert d_res["pan"]["x"] == 24
    assert d_res["pan"]["y"] == 24

    p_res = run_node(tmp_path, "calc", json.dumps(zero_box), json.dumps({"w": 390, "h": 844}), "true")
    assert p_res["zoom"] == 0.8
    assert p_res["pan"]["x"] == 24
    assert p_res["pan"]["y"] == 24


@need_node
def test_zoom_never_leaves_bounds(tmp_path):
    """Zoom never leaves [0.75, 0.9] on desktop and equals 0.8 on phone."""
    dims = [1, 10, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 50000]
    cases = []
    for d in dims:
        cases.append({"box": {"x1": 10, "y1": 20, "w": d, "h": d}, "size": {"w": 1440, "h": 900}, "phone": False})
        cases.append({"box": {"x1": 10, "y1": 20, "w": d, "h": d}, "size": {"w": 390, "h": 844}, "phone": True})
    results = run_node(tmp_path, "batch", json.dumps(cases))
    for c, r in zip(cases, results):
        if c["phone"]:
            assert r["zoom"] == 0.8
        else:
            assert 0.75 <= r["zoom"] <= 0.9


def test_neither_boot_nor_graph_view_contains_dot_fit():
    """Static test that neither page_boot.js nor page_graph_view.js contains .fit("""
    boot_text = (WEB / "page_boot.js").read_text(encoding="utf-8")
    graph_view_text = (WEB / "page_graph_view.js").read_text(encoding="utf-8")
    assert ".fit(" not in boot_text
    assert ".fit(" not in graph_view_text


@need_node
def test_initial_viewport_fake_cy(tmp_path):
    """Checks PlanUI.GraphViewport.initial() on a fake cy for desktop and phone."""
    runner = tmp_path / "initial_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

function makeFakeCy(w, h) {
  var _z = 1, _pan = { x: 0, y: 0 };
  var nodes = {
    "p:atlas": {
      length: 1,
      boundingBox: function () {
        return { x1: -8, y1: -4, w: 5000, h: 700, x2: 4992, y2: 696 };
      }
    },
    "ph:atlas|p1": {
      length: 1,
      boundingBox: function () {
        return { x1: 0, y1: 0, w: 900, h: 560, x2: 900, y2: 560 };
      }
    }
  };
  return {
    resize: function () {},
    width: function () { return w; },
    height: function () { return h; },
    zoom: function (z) { if (z !== undefined) _z = z; return _z; },
    pan: function (p) { if (p !== undefined) _pan = p; return _pan; },
    getElementById: function (id) { return nodes[id] || { length: 0 }; },
    elements: function () { return { length: 0 }; }
  };
}

var vm = {
  projects: [
    {
      name: "atlas",
      phases: [{ name: "p1" }]
    }
  ]
};

var cyDesktop = makeFakeCy(1440, 900);
var uiDesktop = { focusProject: null };
var resDesktop = PlanUI.GraphViewport.initial(cyDesktop, vm, uiDesktop, false);

var cyPhone = makeFakeCy(390, 844);
var uiPhone = { focusProject: null, collapsedPhases: new Set() };
var resPhone = PlanUI.GraphViewport.initial(cyPhone, vm, uiPhone, true);

emit({
  desktop: {
    zoom: cyDesktop.zoom(),
    pan: cyDesktop.pan(),
    res: resDesktop,
    box: resDesktop.box
  },
  phone: {
    zoom: cyPhone.zoom(),
    pan: cyPhone.pan(),
    res: resPhone,
    box: resPhone.box
  }
});
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    d = out["desktop"]
    assert 0.75 <= d["zoom"] <= 0.9
    rendered_x = -8 * d["zoom"] + d["pan"]["x"]
    rendered_y = -4 * d["zoom"] + d["pan"]["y"]
    assert abs(rendered_x - 24) < 1e-6
    assert abs(rendered_y - 24) < 1e-6
    assert abs(d["pan"]["x"] - (24 - (-8) * d["zoom"])) < 1e-6
    assert abs(d["pan"]["y"] - (24 - (-4) * d["zoom"])) < 1e-6

    p = out["phone"]
    assert p["zoom"] == 0.8
    assert p["box"]["x1"] == 0
    assert p["box"]["y1"] == 0
    assert p["box"]["w"] == 900
    assert p["box"]["h"] == 560
    assert p["pan"]["x"] == 24
    assert p["pan"]["y"] == 24


def test_set_initial_viewport_guarded_by_positions_applied():
    """Assert in the source text of page_graph_view.js that positionsApplied occurs >= 3 times."""
    src = (WEB / "page_graph_view.js").read_text(encoding="utf-8")
    assert src.count("positionsApplied") >= 3


@need_node
def test_cluster_label(tmp_path):
    """clusterLabel replaces the first ' · ' with newline; unchanged if missing."""
    runner = tmp_path / "cluster_label_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var cases = [
  PlanUI.clusterLabel("P0 Foundations · 13 tasks · 13 done"),
  PlanUI.clusterLabel("Single line without separator"),
  PlanUI.clusterLabel(""),
  PlanUI.clusterLabel("Part 1 · Part 2 · Part 3")
];
emit(cases);
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out[0] == "P0 Foundations\n13 tasks · 13 done"
    assert out[1] == "Single line without separator"
    assert out[2] == ""
    assert out[3] == "Part 1\nPart 2 · Part 3"


@need_node
def test_focus_project_for(tmp_path):
    """focusProjectFor returns the item's project when differing, or null."""
    runner = tmp_path / "focus_project_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var vm = {
  items: new Map([
    ["itemA", { key: "itemA", project: "projA" }],
    ["itemB", { key: "itemB", project: "projB" }]
  ])
};

var cases = {
  nullFocus: PlanUI.focusProjectFor({ focusProject: null }, vm, "itemB"),
  allUpper: PlanUI.focusProjectFor({ focusProject: "All" }, vm, "itemB"),
  allLower: PlanUI.focusProjectFor({ focusProject: "all" }, vm, "itemB"),
  sameProject: PlanUI.focusProjectFor({ focusProject: "projA" }, vm, "itemA"),
  unknownKey: PlanUI.focusProjectFor({ focusProject: "projA" }, vm, "unknownKey"),
  differs: PlanUI.focusProjectFor({ focusProject: "projA" }, vm, "itemB"),
  storeGetDiffers: PlanUI.focusProjectFor({ get: function (k) { return k === "focusProject" ? "projA" : null; } }, vm, "itemB"),
  storeGetSame: PlanUI.focusProjectFor({ get: function (k) { return k === "focusProject" ? "projB" : null; } }, vm, "itemB")
};
emit(cases);
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["nullFocus"] is None
    assert out["allUpper"] is None
    assert out["allLower"] is None
    assert out["sameProject"] is None
    assert out["unknownKey"] is None
    assert out["differs"] == "projB"
    assert out["storeGetDiffers"] == "projB"
    assert out["storeGetSame"] is None


@need_node
def test_plan_viewport_hook_labels_and_weights(tmp_path):
    """__planViewport exposes collapsedLabelPx/Weight and headerLabelPx/Weight."""
    runner = tmp_path / "hook_runner.js"
    script = r"""
"use strict";
global.window = global;
var cytoscape = require(process.argv[2]);
require(process.argv[3]);
var PlanUI = require(process.argv[4]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var cy = cytoscape({
  headless: true,
  styleEnabled: true,
  elements: [
    { data: { id: "phc:atlas|p0" }, classes: "phase-collapsed" },
    { data: { id: "ph:atlas|p1" }, classes: "phase-compound" }
  ],
  style: [
    { selector: "node.phase-collapsed", style: { "font-size": 20, "font-weight": "normal" } },
    { selector: "node.phase-compound", style: { "font-size": 20, "font-weight": "normal" } }
  ]
});
cy.zoom(0.8);
cy.pan({ x: 24, y: 24 });

PlanUI.GraphViewport.installHook(cy);
var vp = window.__planViewport();
emit({
  collapsedLabelPx: vp.collapsedLabelPx,
  headerLabelPx: vp.headerLabelPx,
  collapsedLabelWeight: vp.collapsedLabelWeight,
  headerLabelWeight: vp.headerLabelWeight,
  equalPx: vp.collapsedLabelPx === vp.headerLabelPx,
  equalWeight: vp.collapsedLabelWeight === vp.headerLabelWeight
});
cy.destroy();
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(ROOT / "vendor" / "cytoscape.min.js"),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["equalPx"] is True
    assert out["equalWeight"] is True
    assert out["collapsedLabelWeight"] == "normal"
    assert abs(out["collapsedLabelPx"] - 16.0) < 1e-6


@need_node
def test_anchor_box_for(tmp_path):
    """anchorBoxFor aligns expanded phase with collapsed header when space permits."""
    runner = tmp_path / "anchor_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var b0 = { x1: 0, y1: 0, w: 300, h: 72 };
var b1 = { x1: 372, y1: 10, w: 400, h: 500 };
var b2 = { x1: 744, y1: 5, w: 500, h: 400 };

var cases = {
  phone_487: PlanUI.anchorBoxFor([b0, b1], 1, 487),
  narrow_400: PlanUI.anchorBoxFor([b0, b1], 1, 400),
  three_boxes: PlanUI.anchorBoxFor([b0, { x1: 372, y1: 0, w: 300, h: 72 }, b2], 2, 487),
  exp_0: PlanUI.anchorBoxFor([b0, b1], 0, 487),
  single: PlanUI.anchorBoxFor([b0], 0, 487),
  empty: PlanUI.anchorBoxFor([], 0, 487),
  nullBoxes: PlanUI.anchorBoxFor(null, 0, 487)
};
emit(cases);
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    # Case 1: [P0 at 0 w 300, P1 at 372], expIdx 1, availW 487 -> s = 0 (372 <= 427) -> union starting at P0's x1
    p487 = out["phone_487"]
    assert p487["x1"] == 0
    assert p487["y1"] == 0
    assert p487["w"] == 772  # max(300, 372 + 400) - 0
    assert p487["h"] == 510  # max(72, 10 + 500) - 0

    # Case 2: availW 400 -> 372 <= 340 is false -> s = 1 -> P1's own box
    n400 = out["narrow_400"]
    assert n400["x1"] == 372
    assert n400["y1"] == 10
    assert n400["w"] == 400
    assert n400["h"] == 500

    # Case 3: three boxes [P0 at 0, P1 at 372, P2 at 744], expIdx 2, availW 487 -> s = 1 (744 - 372 = 372 <= 427)
    tb = out["three_boxes"]
    assert tb["x1"] == 372
    assert tb["y1"] == 0
    assert tb["w"] == (744 + 500) - 372
    assert tb["h"] == 405  # max(72, 5 + 400)

    # Case 4: expIdx 0 -> returns first box
    e0 = out["exp_0"]
    assert e0["x1"] == 0
    assert e0["y1"] == 0
    assert e0["w"] == 300
    assert e0["h"] == 72

    # Case 5: single box
    sg = out["single"]
    assert sg["x1"] == 0
    assert sg["y1"] == 0
    assert sg["w"] == 300
    assert sg["h"] == 72

    # Case 6: empty list and null return None
    assert out["empty"] is None
    assert out["nullBoxes"] is None


@need_node
def test_anchor_box_for_empty(tmp_path):
    """anchorBoxFor returns null for empty boxes list."""
    runner = tmp_path / "empty_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }
emit({ res: PlanUI.anchorBoxFor([], 0, 487) });
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["res"] is None


@need_node
def test_pan_zoom_for(tmp_path):
    """panZoomFor centres the box: pan = size/2 - centre*zoom."""
    runner = tmp_path / "pan_zoom_runner.js"
    script = r"""
"use strict";
require(process.argv[2]);
var PlanUI = require(process.argv[3]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var cases = [
  { box: { x: 100, y: 200, w: 300, h: 400 }, size: { w: 1000, h: 800 }, z: 0.8 },
  { box: { x: 50, y: 80, w: 200, h: 100 }, size: { w: 1440, h: 900 }, z: 0.9 },
  { box: { x: 372, y: 150, w: 250, h: 120 }, size: { w: 390, h: 676 }, z: 0.8 },
  { box: { x: 0, y: 0, w: 0, h: 0 }, size: { w: 500, h: 500 }, z: 1.0 }
];

var results = cases.map(function (c) {
  return {
    c: c,
    res: PlanUI.panZoomFor(c.box, c.size, c.z)
  };
});
emit(results);
"""
    runner.write_text(script, encoding="utf-8")
    cmd = [
        "node",
        str(runner),
        str(WEB / "page.js"),
        str(WEB / "page_graph_viewport.js"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    for item in out:
        c = item["c"]
        res = item["res"]
        box = c["box"]
        size = c["size"]
        z = c["z"]

        assert res["zoom"] == z
        cx = box["x"] + box["w"] / 2.0
        cy = box["y"] + box["h"] / 2.0
        expected_pan_x = size["w"] / 2.0 - cx * z
        expected_pan_y = size["h"] / 2.0 - cy * z
        assert abs(res["pan"]["x"] - expected_pan_x) < 1e-6
        assert abs(res["pan"]["y"] - expected_pan_y) < 1e-6

        # Rendered centre must equal canvas centre
        rendered_cx = cx * z + res["pan"]["x"]
        rendered_cy = cy * z + res["pan"]["y"]
        assert abs(rendered_cx - size["w"] / 2.0) < 1e-6
        assert abs(rendered_cy - size["h"] / 2.0) < 1e-6
