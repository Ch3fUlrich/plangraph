"""Additional pure chips model, stable colors, and convex hull tests (GU-G2a-STRENGTHEN) - Part 2.
Tests: T5 (hull geometry, padding, collinearity, fitBox) and T6 (page_model hook collision).
"""

from __future__ import annotations

import json
import shutil
import struct
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

EXPECTED_PALETTE = [
    "#2f6f4f", "#3b5bdb", "#b45309", "#7c3aed",
    "#0e7490", "#be123c", "#4d7c0f", "#c026d3"
]

RUNNER_JS = r"""
"use strict";
var fs = require("fs"), mode = process.argv[2];
var pageP = process.argv[3], modelP = process.argv[4], storeP = process.argv[5], graphP = process.argv[6], chipsP = process.argv[7], fixP = process.argv[8];
function emit(o) { process.stdout.write(JSON.stringify(o)); }

if (mode === "t6_without_chips") {
  require(pageP); var PlanUI = require(modelP);
  var candA = process.argv[9], candB = process.argv[10];
  var snap = { items: [{ key: "k1", project: candA }, { key: "k2", project: candB }] };
  var vm = PlanUI.buildViewModel(snap);
  emit(vm.projects.map(function(p) { return { name: p.name, color: p.color }; }));
  process.exit(0);
}

require(pageP); var PlanUI = require(modelP);
require(storeP); require(graphP); require(chipsP);

if (mode === "t5_hull_details") {
  function deepFreeze(o) {
    if (!o || typeof o !== "object") return o;
    Object.freeze(o);
    Object.getOwnPropertyNames(o).forEach(function(p) {
      if (o[p] !== null && typeof o[p] === "object" && !Object.isFrozen(o[p])) deepFreeze(o[p]);
    });
    return o;
  }

  // 1. Custom pad honoured: 0, 5, 20
  var singleBox = deepFreeze([{ x1: 10, y1: 20, x2: 30, y2: 40 }]);
  var hPad0 = PlanUI.hullFor(singleBox, 0);
  var hPad5 = PlanUI.hullFor(singleBox, 5);
  var hPad20 = PlanUI.hullFor(singleBox, 20);

  // 2. Collinear points removed from BOTH chains: 3 horizontal collinear boxes
  var colBoxes = deepFreeze([
    { x1: 0, y1: 0, x2: 10, y2: 10 },
    { x1: 20, y1: 0, x2: 30, y2: 10 },
    { x1: 40, y1: 0, x2: 50, y2: 10 }
  ]);
  var hCol = PlanUI.hullFor(colBoxes, 0);

  // 3. Sorting by x then y: equal x, different y
  var ptsEqualX = deepFreeze([
    { x1: 10, y1: 50, x2: 10, y2: 50 },
    { x1: 10, y1: 10, x2: 10, y2: 10 },
    { x1: 40, y1: 30, x2: 40, y2: 30 }
  ]);
  var hSort = PlanUI.hullFor(ptsEqualX, 0);

  // 4. Four-box fixture-like case
  var fourBoxes = deepFreeze([
    { x1: 0, y1: 0, x2: 10, y2: 10 },
    { x1: 40, y1: 0, x2: 50, y2: 10 },
    { x1: 0, y1: 40, x2: 10, y2: 50 },
    { x1: 40, y1: 40, x2: 50, y2: 50 }
  ]);
  var hFour = PlanUI.hullFor(fourBoxes, 10);

  // 5. Empty / null / garbage
  var hEmpty = PlanUI.hullFor([]);
  var hNull = PlanUI.hullFor(null);
  var hGarbage = PlanUI.hullFor([null, { foo: "bar" }, { x1: NaN, y1: 0, x2: 1, y2: 2 }]);

  // 6. fitBoxFor with {x, y, w, h} and Set selection
  var boxesWh = {
    p1: { x: 10, y: 20, w: 30, h: 40 },
    p2: { x: 100, y: 100, w: 50, h: 50 }
  };
  var fitSet = PlanUI.fitBoxFor(boxesWh, new Set(["p1"]));
  var fitAllWh = PlanUI.fitBoxFor(boxesWh, null);

  emit({
    hPad0: hPad0,
    hPad5: hPad5,
    hPad20: hPad20,
    hCol: hCol,
    hSort: hSort,
    hFour: hFour,
    hEmpty: hEmpty,
    hNull: hNull,
    hGarbage: hGarbage,
    fitSet: fitSet,
    fitAllWh: fitAllWh
  });
} else if (mode === "t6_with_chips") {
  var candA = process.argv[9], candB = process.argv[10];
  var snap = { items: [{ key: "k1", project: candA }, { key: "k2", project: candB }] };
  var vm = PlanUI.buildViewModel(snap);
  var singleA = PlanUI.projectColor(candA);
  var singleB = PlanUI.projectColor(candB);
  emit({
    projects: vm.projects.map(function(p) { return { name: p.name, color: p.color }; }),
    singleA: singleA,
    singleB: singleB
  });
}
"""

def run_node(tmp_path: Path, mode: str, *args) -> dict:
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    cmd = [
        "node", str(runner_path), mode,
        str(WEB / "page.js"), str(WEB / "page_model.js"), str(WEB / "page_store.js"),
        str(WEB / "page_graph.js"), str(WEB / "page_graph_chips.js"), str(FIXTURE),
    ] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

@need_node
def test_t5_hull_and_fit_box(tmp_path):
    res = run_node(tmp_path, "t5_hull_details")

    # 1. Custom pad honoured (0, 5, 20): exact 4 padded corners of single box [10..30, 20..40]
    assert res["hPad0"]["points"] == [
        {"x": 10, "y": 20}, {"x": 30, "y": 20},
        {"x": 30, "y": 40}, {"x": 10, "y": 40}
    ]
    assert res["hPad5"]["points"] == [
        {"x": 5, "y": 15}, {"x": 35, "y": 15},
        {"x": 35, "y": 45}, {"x": 5, "y": 45}
    ]
    assert res["hPad20"]["points"] == [
        {"x": -10, "y": 0}, {"x": 50, "y": 0},
        {"x": 50, "y": 60}, {"x": -10, "y": 60}
    ]

    # 2. Collinear points removed from BOTH chains: 3 horizontal collinear boxes give exactly 4 vertices
    assert res["hCol"]["points"] == [
        {"x": 0, "y": 0}, {"x": 50, "y": 0},
        {"x": 50, "y": 10}, {"x": 0, "y": 10}
    ]

    # 3. Sorting by x then y
    assert res["hSort"]["points"] == [
        {"x": 10, "y": 10}, {"x": 40, "y": 30}, {"x": 10, "y": 50}
    ]

    # 4. Four-box fixture-like case: label anchor and bbox exact values
    assert res["hFour"]["bbox"] == {"x1": -10, "y1": -10, "x2": 60, "y2": 60}
    assert res["hFour"]["labelAt"] == {"x": 25, "y": 8}
    assert res["hFour"]["points"] == [
        {"x": -10, "y": -10}, {"x": 60, "y": -10},
        {"x": 60, "y": 60}, {"x": -10, "y": 60}
    ]

    # 5. Empty / null / garbage returns null
    assert res["hEmpty"] is None
    assert res["hNull"] is None
    assert res["hGarbage"] is None

    # 6. fitBoxFor with {x, y, w, h} boxes and Set selection
    assert res["fitSet"] == {"x1": 10, "y1": 20, "x2": 40, "y2": 60}
    assert res["fitAllWh"] == {"x1": 10, "y1": 20, "x2": 150, "y2": 150}

@need_node
def test_t6_page_model_hook_collision(tmp_path):
    def fnv1a_local(s: str) -> int:
        h = 2166136261
        for ch in s:
            h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
        return h

    cands = []
    i = 0
    while len(cands) < 2:
        cand = f"proj_c_{i}"
        if fnv1a_local(cand) % 8 == 2:
            cands.append(cand)
        i += 1
    cand_a, cand_b = sorted(cands)

    base_hash_col = EXPECTED_PALETTE[fnv1a_local(cand_a) % 8]
    assert base_hash_col == EXPECTED_PALETTE[fnv1a_local(cand_b) % 8]

    # Run 1: With chips module loaded: colors are distinct
    res_with = run_node(tmp_path, "t6_with_chips", cand_a, cand_b)
    vm_projects = res_with["projects"]
    assert len(vm_projects) == 2
    assert vm_projects[0]["color"] != vm_projects[1]["color"]
    assert vm_projects[0]["color"] in EXPECTED_PALETTE
    assert vm_projects[1]["color"] in EXPECTED_PALETTE
    assert res_with["singleA"] == base_hash_col
    assert res_with["singleB"] == base_hash_col

    # Run 2: Without chips module loaded (separate node run)
    res_without = run_node(tmp_path, "t6_without_chips", cand_a, cand_b)
    fallback_colors = ["#2f6f4f", "#3b5bdb", "#b45309", "#7c3aed",
                       "#0e7490", "#be123c", "#4d7c0f", "#6d28d9"]
    for idx, p in enumerate(res_without):
        assert p["color"] == fallback_colors[idx % len(fallback_colors)]

