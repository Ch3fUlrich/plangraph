"""T3 pure chips model, stable colors, and convex hull tests (GU-G2a).
Node-run tests: T1 through T8 + acceptance criteria.
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
from plangraph import render as render_mod  # noqa: E402

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
if (mode === "t8_fallback") {
  require(pageP); var PlanUI = require(modelP);
  var snap = JSON.parse(fs.readFileSync(fixP, "utf8")), vm = PlanUI.buildViewModel(snap);
  emit(vm.projects.map(function(p) { return { name: p.name, color: p.color }; }));
  process.exit(0);
}
require(pageP); var PlanUI = require(modelP);
require(storeP); require(graphP); require(chipsP);
var snap = (fixP && fs.existsSync(fixP)) ? JSON.parse(fs.readFileSync(fixP, "utf8")) : null;

if (mode === "t1_palette") {
  emit({ palette: PlanUI.PROJECT_PALETTE });
} else if (mode === "t2_colors") {
  var names = JSON.parse(process.argv[9]), single = process.argv[10] || null;
  emit({
    colors: PlanUI.projectColors(names),
    single: single ? PlanUI.projectColor(single, names) : null,
    baseOnly: single ? PlanUI.projectColor(single) : null,
    empty: PlanUI.projectColor(""), nonString: PlanUI.projectColor(null),
    colors2: PlanUI.projectColors(names)
  });
} else if (mode === "t3_tint") {
  emit({
    sample: PlanUI.colorTint("#3b5bdb", 0.2), clampLow: PlanUI.colorTint("#3b5bdb", -0.5),
    clampHigh: PlanUI.colorTint("#3b5bdb", 1.5), defAlpha: PlanUI.colorTint("#3b5bdb"),
    invalid: PlanUI.colorTint("not-a-color", 0.5), nullHex: PlanUI.colorTint(null, 0.2)
  });
} else if (mode === "t4_chips_model") {
  var vm = PlanUI.buildViewModel(snap), vmBefore = JSON.stringify(vm);
  var ui1 = { filters: { projects: new Set() }, focusProject: null };
  var ui1Before = JSON.stringify({ f: Array.from(ui1.filters.projects), fp: ui1.focusProject });
  var chips1 = PlanUI.chipsModel(vm, ui1);
  var ui1After = JSON.stringify({ f: Array.from(ui1.filters.projects), fp: ui1.focusProject });
  var ui2 = { filters: { projects: new Set(["atlas"]) }, focusProject: null };
  var chips2 = PlanUI.chipsModel(vm, ui2);
  var ui3 = { filters: { projects: new Set() }, focusProject: "dune" };
  var chips3 = PlanUI.chipsModel(vm, ui3);
  emit({
    chips1: chips1, chips2: chips2, chips3: chips3,
    vmUnchanged: vmBefore === JSON.stringify(vm), uiUnchanged: ui1Before === ui1After,
    projectNames: vm.projects.map(function(p) { return p.name; }),
    projectColors: vm.projects.map(function(p) { return p.color; })
  });
} else if (mode === "t5_chip_click") {
  var table = JSON.parse(process.argv[9]);
  emit(table.map(function(row) {
    var sel = row.selected.slice();
    var out = PlanUI.chipClick(sel, row.id, row.mods, row.known);
    return { out: out, notMutated: JSON.stringify(sel) === JSON.stringify(row.selected) };
  }));
} else if (mode === "t6_hull") {
  var b1 = { x1: 10, y1: 20, x2: 110, y2: 70 }, b1_wh = { x: 10, y: 20, w: 100, h: 50 };
  var hull1 = PlanUI.hullFor([b1]), hull1_wh = PlanUI.hullFor([b1_wh]);
  var L1 = { x1: 0, y1: 0, x2: 100, y2: 50 }, L2 = { x1: 0, y1: 50, x2: 50, y2: 100 };
  var hullL = PlanUI.hullFor([L1, L2]), p = 24;
  var corners = [
    { x: L1.x1 - p, y: L1.y1 - p }, { x: L1.x2 + p, y: L1.y1 - p },
    { x: L1.x2 + p, y: L1.y2 + p }, { x: L1.x1 - p, y: L1.y2 + p },
    { x: L2.x1 - p, y: L2.y1 - p }, { x: L2.x2 + p, y: L2.y1 - p },
    { x: L2.x2 + p, y: L2.y2 + p }, { x: L2.x1 - p, y: L2.y2 + p }
  ];
  emit({
    hull1: hull1, hull1_wh: hull1_wh, hullL: hullL, nullHull: PlanUI.hullFor([]),
    cornersInside: corners.every(function(c) { return PlanUI.pointInPolygon(c, hullL.points); }),
    pipInside: PlanUI.pointInPolygon({ x: 25, y: 25 }, hullL.points),
    pipOutside: PlanUI.pointInPolygon({ x: 999, y: 999 }, hullL.points),
    pipOnEdge: PlanUI.pointInPolygon({ x: hullL.points[0].x, y: hullL.points[0].y }, hullL.points),
    pipLessThan3: PlanUI.pointInPolygon({ x: 0, y: 0 }, [{ x: 0, y: 0 }])
  });
} else if (mode === "t7_fit_box") {
  var bxs = { atlas: { x1: 0, y1: 0, x2: 50, y2: 50 }, borealis: { x1: 40, y1: 40, x2: 100, y2: 120 }, cinder: { x1: 200, y1: 200, x2: 300, y2: 300 } };
  emit({
    twoSelected: PlanUI.fitBoxFor(bxs, ["atlas", "borealis"]), allEmpty: PlanUI.fitBoxFor(bxs, []),
    missingIgnored: PlanUI.fitBoxFor(bxs, ["atlas", "missing"]),
    nothing1: PlanUI.fitBoxFor({}, []), nothing2: PlanUI.fitBoxFor(bxs, ["missing"])
  });
} else if (mode === "t8_with_chips") {
  var vm = PlanUI.buildViewModel(snap);
  emit(vm.projects.map(function(p) {
    return { name: p.name, color: p.color, expected: PlanUI.projectColor(p.name, vm.projects.map(function(x) { return x.name; })) };
  }));
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

def hex_to_rgb(h: str) -> tuple[int, int, int]:
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))

def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2])

def contrast_ratio(c1: str, c2: str) -> float:
    l1, l2 = relative_luminance(hex_to_rgb(c1)), relative_luminance(hex_to_rgb(c2))
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)

def fnv1a_test(s: str) -> int:
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h

@need_node
def test_t1_palette(tmp_path):
    palette = run_node(tmp_path, "t1_palette")["palette"]
    assert palette == EXPECTED_PALETTE
    for hex_col in palette:
        assert re.match(r"^#[0-9a-f]{6}$", hex_col)
    rgbs = [hex_to_rgb(h) for h in palette]
    min_dist = min(math.sqrt(sum((a - b) ** 2 for a, b in zip(rgbs[i], rgbs[j])))
                   for i in range(len(rgbs)) for j in range(i + 1, len(rgbs)))
    assert min_dist >= 50.0
    assert min(contrast_ratio(h, "#ffffff") for h in palette) >= 2.8
    assert min(contrast_ratio(h, "#111827") for h in palette) >= 2.8

@need_node
def test_t2_project_color_stability(tmp_path):
    names = ["atlas", "borealis", "cinder", "dune", "epsilon", "foxtrot"]
    for nm in names:
        expected = EXPECTED_PALETTE[fnv1a_test(nm) % 8]
        res = run_node(tmp_path, "t2_colors", json.dumps(names), nm)
        assert res["baseOnly"] == expected and res["single"] == expected

    res_sub = run_node(tmp_path, "t2_colors", json.dumps(["atlas", "borealis"]), "atlas")
    assert res_sub["single"] == EXPECTED_PALETTE[fnv1a_test("atlas") % 8]

    colliding = []
    for i in range(1000):
        cname = f"cand_{i}"
        if (fnv1a_test(cname) % 8) == 0:
            colliding.append(cname)
            if len(colliding) == 8:
                break
    assert len(colliding) == 8

    res_col = run_node(tmp_path, "t2_colors", json.dumps(colliding))
    cols = res_col["colors"]
    assert len(set(cols.values())) == 8
    assert cols[sorted(colliding)[0]] == EXPECTED_PALETTE[0]
    assert run_node(tmp_path, "t2_colors", json.dumps(list(reversed(colliding))))["colors"] == cols

    cand9 = next(f"c9_{i}" for i in range(1000, 2000) if (fnv1a_test(f"c9_{i}") % 8) == 0)
    for c in run_node(tmp_path, "t2_colors", json.dumps(colliding + [cand9]))["colors"].values():
        assert c in EXPECTED_PALETTE
    assert res_col["empty"] == EXPECTED_PALETTE[0] and res_col["nonString"] == EXPECTED_PALETTE[0]
    assert res_col["colors"] == res_col["colors2"]

@need_node
def test_t3_color_tint(tmp_path):
    res = run_node(tmp_path, "t3_tint")
    assert res["sample"] == "rgba(59, 91, 219, 0.2)"
    assert res["clampLow"] == "rgba(59, 91, 219, 0)"
    assert res["clampHigh"] == "rgba(59, 91, 219, 1)"
    assert res["defAlpha"] == "rgba(59, 91, 219, 0.12)"
    assert res["invalid"] == "rgba(120, 120, 120, 0.5)"
    assert res["nullHex"] == "rgba(120, 120, 120, 0.2)"

@need_node
def test_t4_chips_model(tmp_path):
    res = run_node(tmp_path, "t4_chips_model")
    chips1 = res["chips1"]
    assert chips1[0]["id"] == "all" and chips1[0]["count"] == 254
    assert chips1[0]["color"] is None and chips1[0]["active"] is True

    proj_chips = chips1[1:]
    assert len(proj_chips) == 4 and sum(c["count"] for c in proj_chips) == 254
    assert [c["id"] for c in proj_chips] == res["projectNames"]
    assert len(set(c["color"] for c in proj_chips)) == 4
    assert all(not c["active"] for c in proj_chips)

    chips2 = res["chips2"]
    assert chips2[0]["active"] is False and chips2[1]["id"] == "atlas" and chips2[1]["active"] is True
    assert all(not c["active"] for c in chips2[2:])

    chips3 = res["chips3"]
    assert chips3[0]["active"] is False
    assert next(c for c in chips3 if c["id"] == "dune")["active"] is True
    assert all(not c["active"] for c in chips3 if c["id"] not in ("dune",))

    for c in proj_chips:
        assert c["percent"] == round((c["done"] / c["count"]) * 100)
    assert res["vmUnchanged"] is True and res["uiUnchanged"] is True

@need_node
def test_t5_chip_click_table(tmp_path):
    table = [
        {"selected": ["atlas"], "id": "all", "mods": None, "known": ["atlas", "borealis"], "exp": []},
        {"selected": [], "id": "atlas", "mods": None, "known": ["atlas", "borealis"], "exp": ["atlas"]},
        {"selected": ["atlas"], "id": "atlas", "mods": None, "known": ["atlas", "borealis"], "exp": []},
        {"selected": ["atlas"], "id": "borealis", "mods": None, "known": ["atlas", "borealis"], "exp": ["borealis"]},
        {"selected": ["atlas"], "id": "borealis", "mods": {"ctrl": True}, "known": None, "exp": ["atlas", "borealis"]},
        {"selected": ["atlas"], "id": "atlas", "mods": {"ctrl": True}, "known": None, "exp": []},
        {"selected": ["atlas"], "id": "borealis", "mods": {"meta": True}, "known": None, "exp": ["atlas", "borealis"]},
        {"selected": ["atlas"], "id": "borealis", "mods": {"shift": True}, "known": None, "exp": ["atlas", "borealis"]},
        {"selected": ["atlas"], "id": "unknown", "mods": None, "known": ["atlas", "borealis"], "exp": ["atlas"]},
        {"selected": ["atlas"], "id": None, "mods": None, "known": None, "exp": ["atlas"]},
        {"selected": ["atlas"], "id": "borealis", "mods": None, "known": None, "exp": ["borealis"]},
        {"selected": ["cinder", "atlas"], "id": "borealis", "mods": {"ctrl": True}, "known": None, "exp": ["atlas", "borealis", "cinder"]}
    ]
    res = run_node(tmp_path, "t5_chip_click", json.dumps(table))
    for i, row in enumerate(table):
        assert res[i]["out"] == row["exp"] and res[i]["notMutated"] is True

@need_node
def test_t6_hull_and_point_in_polygon(tmp_path):
    res = run_node(tmp_path, "t6_hull")
    h1 = res["hull1"]
    assert len(h1["points"]) == 4 and h1["bbox"] == {"x1": -14, "y1": -4, "x2": 134, "y2": 94}
    assert h1["labelAt"] == {"x": 60, "y": 14} and res["hull1_wh"] == h1

    hl = res["hullL"]
    pts = hl["points"]
    assert len(pts) <= 8 and res["cornersInside"] is True
    def cross(a, b, c):
        return (b["x"] - a["x"]) * (c["y"] - a["y"]) - (b["y"] - a["y"]) * (c["x"] - a["x"])
    for i in range(len(pts)):
        assert cross(pts[i], pts[(i + 1) % len(pts)], pts[(i + 2) % len(pts)]) > 0

    assert hl["bbox"]["x1"] <= hl["labelAt"]["x"] <= hl["bbox"]["x2"]
    assert hl["bbox"]["y1"] <= hl["labelAt"]["y"] <= hl["bbox"]["y2"]
    assert res["nullHull"] is None and res["pipInside"] is True and res["pipOutside"] is False
    assert res["pipOnEdge"] is True and res["pipLessThan3"] is False

@need_node
def test_t7_fit_box_for(tmp_path):
    res = run_node(tmp_path, "t7_fit_box")
    assert res["twoSelected"] == {"x1": 0, "y1": 0, "x2": 100, "y2": 120}
    assert res["allEmpty"] == {"x1": 0, "y1": 0, "x2": 300, "y2": 300}
    assert res["missingIgnored"] == {"x1": 0, "y1": 0, "x2": 50, "y2": 50}
    assert res["nothing1"] is None and res["nothing2"] is None

@need_node
def test_t8_registration_and_fallback(tmp_path):
    assert render_mod.JS_FILES.count("page_graph_chips.js") == 1
    g_idx = render_mod.JS_FILES.index("page_graph.js")
    c_idx = render_mod.JS_FILES.index("page_graph_chips.js")
    l_idx = render_mod.JS_FILES.index("page_layout.js")
    assert g_idx < c_idx < l_idx

    chips_text = (WEB / "page_graph_chips.js").read_text(encoding="utf-8")
    assert "document" not in chips_text and "cytoscape" not in chips_text and "innerHTML" not in chips_text
    assert "COLORS[pi % COLORS.length]" in (WEB / "page_model.js").read_text(encoding="utf-8")

    old_colors = ["#2f6f4f", "#3b5bdb", "#b45309", "#7c3aed",
                  "#0e7490", "#be123c", "#4d7c0f", "#6d28d9"]
    for i, p in enumerate(run_node(tmp_path, "t8_fallback")):
        assert p["color"] == old_colors[i % len(old_colors)]
    for p in run_node(tmp_path, "t8_with_chips"):
        assert p["color"] == p["expected"]

@need_node
def test_acceptance_a23(tmp_path):
    res = run_node(tmp_path, "t4_chips_model")
    by_name = {c["id"]: c["color"] for c in res["chips1"][1:]}
    assert len(set(by_name.values())) == 4
    assert by_name["atlas"] == "#0e7490"
    assert by_name["borealis"] == "#b45309"
    assert by_name["cinder"] == "#2f6f4f"
    assert by_name["dune"] == "#7c3aed"
    rgbs = [hex_to_rgb(h) for h in EXPECTED_PALETTE]
    min_dist = min(math.sqrt(sum((a - b) ** 2 for a, b in zip(rgbs[i], rgbs[j])))
                   for i in range(len(rgbs)) for j in range(i + 1, len(rgbs)))
    assert min_dist >= 50.0
    min_c = min(min(contrast_ratio(h, "#ffffff"), contrast_ratio(h, "#111827")) for h in EXPECTED_PALETTE)
    assert min_c >= 2.8
