"""Additional pure chips model, stable colors, and convex hull tests (GU-G2a-STRENGTHEN).
Tests: F1, T1 through T6 pin specific behaviours and mutation resistance.
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
require(pageP); var PlanUI = require(modelP);
require(storeP); require(graphP); require(chipsP);

if (mode === "f1_dangerous") {
  var names = JSON.parse(process.argv[9]);
  var colors = PlanUI.projectColors(names);
  var revColors = PlanUI.projectColors(names.slice().reverse());
  var singles = Object.create(null);
  for (var i = 0; i < names.length; i++) {
    singles[names[i]] = PlanUI.projectColor(names[i], names);
  }
  emit({ colors: colors, revColors: revColors, singles: singles });
} else if (mode === "t1_hash_probing") {
  var payload = JSON.parse(process.argv[9]);
  var results = {};
  if (payload.names) {
    results.colors = PlanUI.projectColors(payload.names);
  }
  if (payload.singles) {
    results.singles = {};
    for (var k = 0; k < payload.singles.length; k++) {
      var item = payload.singles[k];
      results.singles[item.name] = PlanUI.projectColor(item.name, item.names);
    }
  }
  emit(results);
} else if (mode === "t2_tint") {
  emit({
    nanAlpha: PlanUI.colorTint("#3b5bdb", NaN),
    posInfAlpha: PlanUI.colorTint("#3b5bdb", Infinity),
    negInfAlpha: PlanUI.colorTint("#3b5bdb", -Infinity),
    underflow: PlanUI.colorTint("#3b5bdb", -5),
    overflow: PlanUI.colorTint("#3b5bdb", 5),
    exactZero: PlanUI.colorTint("#3b5bdb", 0),
    exactOne: PlanUI.colorTint("#3b5bdb", 1),
    invalidHexNan: PlanUI.colorTint("xyz", NaN),
    invalidHexOver: PlanUI.colorTint("xyz", 3)
  });
} else if (mode === "t3_chips") {
  function deepFreeze(obj) {
    if (!obj || typeof obj !== "object") return obj;
    Object.freeze(obj);
    Object.getOwnPropertyNames(obj).forEach(function(prop) {
      if (obj[prop] !== null && (typeof obj[prop] === "object" || typeof obj[prop] === "function") && !Object.isFrozen(obj[prop])) {
        deepFreeze(obj[prop]);
      }
    });
    return obj;
  }
  var candA = process.argv[9], candB = process.argv[10];
  var vm = {
    projects: [
      {
        name: candA,
        label: "Custom " + candA,
        phases: [{ total: 10, done: 4 }, { total: 5, done: 3 }]
      },
      {
        name: candB,
        phases: [{ total: 0, done: 0 }]
      }
    ]
  };
  var uiFilterAndFocus = {
    filters: { projects: new Set([candA]) },
    focusProject: candB
  };
  deepFreeze(vm);
  deepFreeze(uiFilterAndFocus);
  var chipsFF = PlanUI.chipsModel(vm, uiFilterAndFocus);
  var chipsNull = PlanUI.chipsModel(null, null);
  var chipsUndefined = PlanUI.chipsModel(undefined, undefined);
  var chipsEmpty = PlanUI.chipsModel({}, {});
  emit({
    chipsFF: chipsFF,
    chipsNull: chipsNull,
    chipsUndefined: chipsUndefined,
    chipsEmpty: chipsEmpty
  });
} else if (mode === "t4_chip_click") {
  var tests = [
    function() {
      var sel = Object.freeze(["atlas", "borealis", "cinder"]);
      return { out: PlanUI.chipClick(sel, "borealis", null), notSame: true };
    },
    function() {
      var sel = Object.freeze(["atlas"]);
      return { out: PlanUI.chipClick(sel, "atlas", null), notSame: true };
    },
    function() {
      var sel = Object.freeze(["atlas"]);
      return { out: PlanUI.chipClick(sel, "atlas", { ctrl: true }), notSame: true };
    },
    function() {
      var sel = Object.freeze(["atlas", "cinder"]);
      return { out: PlanUI.chipClick(sel, "borealis", { meta: true }), notSame: true };
    },
    function() {
      var sel = Object.freeze(["b", "a", "b"]);
      var outEmpty = PlanUI.chipClick(sel, "", { ctrl: true });
      var outNull = PlanUI.chipClick(sel, null, null);
      return { outEmpty: outEmpty, outNull: outNull, notSame: (outEmpty !== sel) && (outNull !== sel) };
    },
    function() {
      var sel = Object.freeze(["z", "a", "z", "b"]);
      return { out: PlanUI.chipClick(sel, "c", { shift: true }) };
    }
  ];
  emit(tests.map(function(fn) { return fn(); }));
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

def fnv1a_utf16(s: str) -> int:
    h = 2166136261
    raw = s.encode("utf-16-le")
    units = struct.unpack(f"<{len(raw)//2}H", raw)
    for u in units:
        h = ((h ^ u) * 16777619) & 0xFFFFFFFF
    return h

@need_node
def test_f1_dangerous_names_project_colors(tmp_path):
    dangerous = [
        "constructor", "toString", "hasOwnProperty",
        "valueOf", "__proto__", "isPrototypeOf",
        "norm_a", "norm_b"
    ]
    res = run_node(tmp_path, "f1_dangerous", json.dumps(dangerous))
    colors = res["colors"]
    rev_colors = res["revColors"]
    singles = res["singles"]

    # Every name gets a palette colour
    for name in dangerous:
        assert name in colors
        assert colors[name] in EXPECTED_PALETTE
        assert singles[name] == colors[name]

    # Colours of all 8 names are distinct up to palette size 8
    assert len(set(colors.values())) == 8

    # Results do not depend on the order of names
    assert colors == rev_colors

@need_node
def test_t1_hash_palette_utf16_and_probing(tmp_path):
    # Non-BMP characters: emoji and CJK extension B
    emoji_name = "proj_🚀"
    cjk_ext_b_name = "proj_𠀀"
    expected_emoji_col = EXPECTED_PALETTE[fnv1a_utf16(emoji_name) % 8]
    expected_cjk_col = EXPECTED_PALETTE[fnv1a_utf16(cjk_ext_b_name) % 8]

    # Probing direction, step and wrap:
    # Find 3 names that all have base hash 6
    colliding_base6 = []
    i = 0
    while len(colliding_base6) < 3:
        cand = f"target6_{i}"
        if fnv1a_utf16(cand) % 8 == 6:
            colliding_base6.append(cand)
        i += 1
    # Sorted order of these three names
    s_col = sorted(colliding_base6)
    # First gets base 6, second gets base+1 = 7, third wraps to (6+2)%8 = 0
    exp_col0 = EXPECTED_PALETTE[6]
    exp_col1 = EXPECTED_PALETTE[7]
    exp_col2 = EXPECTED_PALETTE[0]

    # Collision test with duplicate names: duplicates in names are deduplicated
    names_with_dups = colliding_base6 + [colliding_base6[0], colliding_base6[1]]

    # Appended-name path: a name not in names array that collides with an occupied slot
    appended_colliding = next(f"extra_{k}" for k in range(i, i + 500) if fnv1a_utf16(f"extra_{k}") % 8 == 6)

    payload = {
        "names": names_with_dups,
        "singles": [
            {"name": emoji_name, "names": None},
            {"name": emoji_name, "names": []},
            {"name": emoji_name, "names": [emoji_name]},
            {"name": cjk_ext_b_name, "names": None},
            {"name": s_col[0], "names": colliding_base6},
            {"name": s_col[1], "names": colliding_base6},
            {"name": s_col[2], "names": colliding_base6},
            {"name": appended_colliding, "names": colliding_base6},
        ]
    }
    res = run_node(tmp_path, "t1_hash_probing", json.dumps(payload))

    # UTF-16 code units hash verification
    assert res["singles"][emoji_name] == expected_emoji_col
    assert res["singles"][cjk_ext_b_name] == expected_cjk_col

    # Probing step, direction, wrap
    colors = res["colors"]
    assert colors[s_col[0]] == exp_col0
    assert colors[s_col[1]] == exp_col1
    assert colors[s_col[2]] == exp_col2
    # Duplicates did not consume extra slots (only 3 unique names in colors map)
    assert len(colors) == 3

    # Appended name colliding at slot 6: slots 6, 7, 0 are used, so it probes 6 -> 7 -> 0 -> 1
    exp_appended = EXPECTED_PALETTE[1]
    assert res["singles"][appended_colliding] == exp_appended

@need_node
def test_t2_color_tint_nan_and_range(tmp_path):
    res = run_node(tmp_path, "t2_tint")
    assert res["nanAlpha"] == "rgba(59, 91, 219, 0.12)"
    assert res["posInfAlpha"] == "rgba(59, 91, 219, 0.12)"
    assert res["negInfAlpha"] == "rgba(59, 91, 219, 0.12)"
    assert res["underflow"] == "rgba(59, 91, 219, 0)"
    assert res["overflow"] == "rgba(59, 91, 219, 1)"
    assert res["exactZero"] == "rgba(59, 91, 219, 0)"
    assert res["exactOne"] == "rgba(59, 91, 219, 1)"
    assert res["invalidHexNan"] == "rgba(120, 120, 120, 0.12)"
    assert res["invalidHexOver"] == "rgba(120, 120, 120, 1)"

@need_node
def test_t3_chips_model_precedence_and_invariants(tmp_path):
    # Find two names that collide at base hash 2
    cands = []
    i = 0
    while len(cands) < 2:
        cand = f"cand_c2_{i}"
        if fnv1a_utf16(cand) % 8 == 2:
            cands.append(cand)
        i += 1
    cand_a, cand_b = cands[0], cands[1]

    res = run_node(tmp_path, "t3_chips", cand_a, cand_b)
    chips_ff = res["chipsFF"]

    # All chip
    assert chips_ff[0]["id"] == "all"
    assert chips_ff[0]["label"] == "All"
    assert chips_ff[0]["count"] == 15  # 10 + 5 + 0
    assert chips_ff[0]["active"] is False  # Because filter/focus is active

    # Filter vs focus precedence: filter has cand_a, focus is cand_b -> filter wins
    chip_a = next(c for c in chips_ff if c["id"] == cand_a)
    chip_b = next(c for c in chips_ff if c["id"] == cand_b)
    assert chip_a["active"] is True
    assert chip_b["active"] is False

    # Done count summed over phases
    assert chip_a["count"] == 15
    assert chip_a["done"] == 7
    assert chip_a["percent"] == 47  # round(7 / 15 * 100)

    # 0 tasks gives percent 0
    assert chip_b["count"] == 0
    assert chip_b["percent"] == 0

    # Chip color collision: colliding projects get distinct colors
    assert chip_a["color"] != chip_b["color"]
    assert chip_a["color"] in EXPECTED_PALETTE
    assert chip_b["color"] in EXPECTED_PALETTE

    # Label equals project label or name
    assert chip_a["label"] == f"Custom {cand_a}"
    assert chip_b["label"] == cand_b

    # Minimal model for null, undefined, empty
    minimal = [{"id": "all", "label": "All", "count": 0, "color": None, "active": True}]
    assert res["chipsNull"] == minimal
    assert res["chipsUndefined"] == minimal
    assert res["chipsEmpty"] == minimal

@need_node
def test_t4_chip_click_selection_semantics(tmp_path):
    res = run_node(tmp_path, "t4_chip_click")

    # 1. Plain click on one of several selected chips selects ONLY that chip
    assert res[0]["out"] == ["borealis"]
    assert res[0]["notSame"] is True

    # 2. Plain click on the only selected chip clears filter (empty array)
    assert res[1]["out"] == []
    assert res[1]["notSame"] is True

    # 3. Toggle that removes the last selected chip clears the filter
    assert res[2]["out"] == []
    assert res[2]["notSame"] is True

    # 4. Toggle adds without touching others
    assert res[3]["out"] == ["atlas", "borealis", "cinder"]
    assert res[3]["notSame"] is True

    # 5. Empty/null id returns sorted-unique COPY, not treated as toggle
    assert res[4]["outEmpty"] == ["a", "b"]
    assert res[4]["outNull"] == ["a", "b"]
    assert res[4]["notSame"] is True

    # 6. Duplicates in selection are removed
    assert res[5]["out"] == ["a", "b", "c", "z"]



