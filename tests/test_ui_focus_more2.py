"""Additional tests for PlanUI focus model — GU-G3a strengthen (part 1)."""

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

sys.path.insert(0, str(SRC))

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def run_node_snippet(tmp_path: Path, script_body: str) -> dict | list:
    runner_code = f"""
    "use strict";
    require({json.dumps(str(WEB / "page.js"))});
    var PlanUI = require({json.dumps(str(WEB / "page_model.js"))});
    require({json.dumps(str(WEB / "page_store.js"))});
    require({json.dumps(str(WEB / "page_search.js"))});
    require({json.dumps(str(WEB / "page_graph.js"))});
    require({json.dumps(str(WEB / "page_graph_focus.js"))});

    function emit(v) {{ process.stdout.write(JSON.stringify(v)); }}

    {script_body}
    """
    runner_file = tmp_path / "runner_more2.js"
    runner_file.write_text(runner_code, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(runner_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_g1_keep_rule_target_direction(tmp_path: Path):
    """G1: edge keep rule when focus is target, and hops 1 & 2 target side keep."""
    body = """
    // Directed chain: X -> S -> F (where F is the focus)
    var nodes = [
      { data: { id: "t:F" } },
      { data: { id: "t:S" } },
      { data: { id: "t:X" } },
      { data: { id: "t:Y" } }
    ];
    var edges = [
      { data: { id: "e:S-F", source: "t:S", target: "t:F" } },
      { data: { id: "e:X-S", source: "t:X", target: "t:S" } },
      { data: { id: "e:Y-X", source: "t:Y", target: "t:X" } }
    ];
    var g = { nodes: nodes, edges: edges };
    var h1 = PlanUI.focusSets(g, "t:F", { hops: 1 });
    var h2 = PlanUI.focusSets(g, "t:F", { hops: 2 });
    emit({ h1: h1, h2: h2 });
    """
    res = run_node_snippet(tmp_path, body)
    h1 = res["h1"]
    h2 = res["h2"]

    # Hops 1: F is core, S is near (dist 1). X is dist 2 (not near).
    assert h1["core"] == ["t:F"]
    assert h1["near"] == ["t:S"]
    assert "e:S-F" in h1["keepEdges"]
    assert "e:X-S" in h1["fadeEdges"]
    assert "e:X-S" not in h1["keepEdges"]

    # Hops 2: F is core, S is near (dist 1), X is near (dist 2).
    # For e:X-S: source X has dist 2, target S has dist 1 (< hops 2).
    # Mutant testing only source side would drop e:X-S.
    assert h2["core"] == ["t:F"]
    assert h2["near"] == ["t:S", "t:X"]
    assert "e:S-F" in h2["keepEdges"]
    assert "e:X-S" in h2["keepEdges"]
    assert "e:X-S" not in h2["fadeEdges"]
    assert "e:Y-X" in h2["fadeEdges"]


@need_node
def test_g1_keep_core_to_core_compound(tmp_path: Path):
    """G1: edge between two core nodes of a compound focus is kept."""
    body = """
    var nodes = [
      { data: { id: "ph:1" } },
      { data: { id: "t:1:A", parent: "ph:1" } },
      { data: { id: "t:1:B", parent: "ph:1" } },
      { data: { id: "t:outer" } }
    ];
    var edges = [
      { data: { id: "e:A-B", source: "t:1:A", target: "t:1:B" } },
      { data: { id: "e:B-out", source: "t:1:B", target: "t:outer" } }
    ];
    var g = { nodes: nodes, edges: edges };
    var res = PlanUI.focusSets(g, "ph:1", { hops: 1 });
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert set(res["core"]) == {"ph:1", "t:1:A", "t:1:B"}
    assert res["near"] == ["t:outer"]
    # Edge between two core nodes is kept
    assert "e:A-B" in res["keepEdges"]
    assert "e:A-B" not in res["fadeEdges"]
    assert "e:B-out" in res["keepEdges"]


@need_node
def test_g2_focus_reduce_actions_reset_expand_and_undefined_action(tmp_path: Path):
    """G2: tapEmpty, esc, dbltap(task), dbltap(empty), tap(empty) reset expand; undefined action."""
    body = """
    var state = { focusId: "t:orig", expand: { kind: "phase", id: "p1" } };

    var rTapEmpty = PlanUI.focusReduce(state, { type: "tapEmpty" });
    var rEsc = PlanUI.focusReduce(state, { type: "esc" });
    var rDblTask = PlanUI.focusReduce(state, { type: "dbltap", id: "t:target" });
    var rDblEmpty = PlanUI.focusReduce(state, { type: "dbltap", id: "" });
    var rTapEmptyId = PlanUI.focusReduce(state, { type: "tap", id: "" });
    var rUndefAction = PlanUI.focusReduce(state, undefined);
    var rBothUndef = PlanUI.focusReduce(undefined, undefined);

    emit({
      tapEmpty: rTapEmpty,
      esc: rEsc,
      dblTask: rDblTask,
      dblEmpty: rDblEmpty,
      tapEmptyId: rTapEmptyId,
      undefAction: rUndefAction,
      bothUndef: rBothUndef
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["tapEmpty"] == {"focusId": None, "expand": None}
    assert res["esc"] == {"focusId": None, "expand": None}
    assert res["dblTask"] == {"focusId": "t:target", "expand": None}
    assert res["dblEmpty"] == {"focusId": "t:orig", "expand": None}
    assert res["tapEmptyId"] == {"focusId": "t:orig", "expand": None}
    assert res["undefAction"] == {"focusId": "t:orig", "expand": None}
    assert res["bothUndef"] == {"focusId": None, "expand": None}


@need_node
def test_g2_focus_reduce_deep_freeze_purity(tmp_path: Path):
    """G2: dbltap never mutates frozen input state or action."""
    body = """
    var state = Object.freeze({
      focusId: "phc:phase1",
      expand: Object.freeze({ kind: "phase", id: "phase1", counter: 1 })
    });
    var actionPhc = Object.freeze({
      type: "dbltap",
      id: "phc:phase2"
    });
    var actionPc = Object.freeze({
      type: "dbltap",
      id: "pc:proj1"
    });
    var actionTask = Object.freeze({
      type: "dbltap",
      id: "t:1"
    });

    var rPhc = PlanUI.focusReduce(state, actionPhc);
    var rPc = PlanUI.focusReduce(state, actionPc);
    var rTask = PlanUI.focusReduce(state, actionTask);

    emit({
      rPhc: rPhc,
      rPc: rPc,
      rTask: rTask,
      stateFrozen: Object.isFrozen(state),
      stateExpandFrozen: Object.isFrozen(state.expand)
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["stateFrozen"] is True
    assert res["stateExpandFrozen"] is True
    assert res["rPhc"] == {"focusId": "ph:phase2", "expand": {"kind": "phase", "id": "phase2"}}
    assert res["rPc"] == {"focusId": "p:proj1", "expand": {"kind": "project", "id": "proj1"}}
    assert res["rTask"] == {"focusId": "t:1", "expand": None}


@need_node
def test_g3_compound_null_nodes_children_walk(tmp_path: Path):
    """G3: null node or node without data during compound focus children walk does not throw."""
    body = """
    var nodes = [
      null,
      undefined,
      { data: null },
      {},
      { data: {} },
      { data: { id: "ph:1" } },
      null,
      { data: { id: "t:child", parent: "ph:1" } },
      { data: null }
    ];
    var g = { nodes: nodes, edges: [] };
    var res = PlanUI.focusSets(g, "ph:1");
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert set(res["core"]) == {"ph:1", "t:child"}


@need_node
def test_g3_array_like_nodes_and_missing_edges_return_null(tmp_path: Path):
    """G3: array-like nodes or graph without edges returns null (mutant: truthiness or dropped edges check)."""
    body = """
    var arrayLikeNodes = {
      length: 1,
      0: { data: { id: "t:1" } }
    };
    var gArrayLike = { nodes: arrayLikeNodes, edges: [] };
    var gNoEdges = { nodes: [{ data: { id: "t:1" } }] };
    var gEdgesNotArray = { nodes: [{ data: { id: "t:1" } }], edges: { length: 0 } };

    emit({
      resArrayLike: PlanUI.focusSets(gArrayLike, "t:1"),
      resNoEdges: PlanUI.focusSets(gNoEdges, "t:1"),
      resEdgesNotArray: PlanUI.focusSets(gEdgesNotArray, "t:1")
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["resArrayLike"] is None
    assert res["resNoEdges"] is None
    assert res["resEdgesNotArray"] is None


@need_node
def test_g3_dangling_edge_phantom_path_at_hops_2(tmp_path: Path):
    """G3: a path through a dangling edge phantom node at hops 2 (t:1 - PH - t:2) does not make t:2 near."""
    body = """
    var nodes = [
      { data: { id: "t:1" } },
      { data: { id: "t:2" } }
    ];
    var edges = [
      { data: { id: "e:1-PH", source: "t:1", target: "PH_phantom" } },
      { data: { id: "e:PH-2", source: "PH_phantom", target: "t:2" } }
    ];
    var g = { nodes: nodes, edges: edges };
    var res = PlanUI.focusSets(g, "t:1", { hops: 2 });
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert res["core"] == ["t:1"]
    assert "t:2" not in res["near"]
    assert res["near"] == []
    assert res["faded"] == ["t:2"]
    assert "PH_phantom" not in res["near"]
    assert "PH_phantom" not in res["faded"]
    assert "e:1-PH" in res["fadeEdges"]
    assert "e:PH-2" in res["fadeEdges"]
    assert res["keepEdges"] == []


@need_node
def test_g4_project_compound_recursive_descendants(tmp_path: Path):
    """G4: focusing p: node recursively includes all descendants (phases, sub-phases, tasks)."""
    body = """
    var nodes = [
      { data: { id: "p:Proj" } },
      { data: { id: "ph:Proj|1", parent: "p:Proj" } },
      { data: { id: "t:Proj:1", parent: "ph:Proj|1" } },
      { data: { id: "ph:Proj|2", parent: "p:Proj" } },
      { data: { id: "ph:Proj|2_sub", parent: "ph:Proj|2" } },
      { data: { id: "t:Proj:2", parent: "ph:Proj|2_sub" } },
      { data: { id: "p:Other" } },
      { data: { id: "t:Other:1", parent: "p:Other" } }
    ];
    var g = { nodes: nodes, edges: [] };
    var res = PlanUI.focusSets(g, "p:Proj");

    // Independent descendant collector for verification
    var expected = new Set(["p:Proj"]);
    var changed = true;
    while (changed) {
      changed = false;
      for (var i = 0; i < nodes.length; i++) {
        var nd = nodes[i];
        if (nd.data.parent && expected.has(nd.data.parent) && !expected.has(nd.data.id)) {
          expected.add(nd.data.id);
          changed = true;
        }
      }
    }
    var expArr = [];
    expected.forEach(function(x) { expArr.push(x); });
    expArr.sort();

    emit({ res: res, expectedCore: expArr });
    """
    data = run_node_snippet(tmp_path, body)
    res = data["res"]
    expected_core = data["expectedCore"]

    assert res["core"] == expected_core
    assert res["core"] == [
        "p:Proj",
        "ph:Proj|1",
        "ph:Proj|2",
        "ph:Proj|2_sub",
        "t:Proj:1",
        "t:Proj:2",
    ]
    assert "p:Other" not in res["core"]
    assert "t:Other:1" not in res["core"]
    assert res["faded"] == ["p:Other", "t:Other:1"]


@need_node
def test_g4_phase_nested_and_three_context_exclusion_checks(tmp_path: Path):
    """G4: ph: nested descendants, and three context exclusions (core/near ancestor, non-graph id, focus itself)."""
    body = """
    var nodes = [
      { data: { id: "p:A" } },
      { data: { id: "ph:A|root", parent: "p:A" } },
      { data: { id: "ph:A|sub", parent: "ph:A|root" } },
      { data: { id: "t:A:1", parent: "ph:A|sub" } },

      { data: { id: "p:B" } },
      { data: { id: "ph:B|root", parent: "p:B" } },
      { data: { id: "t:B:1", parent: "ph:B|root" } },

      { data: { id: "t:orphan", parent: "non_existent_ancestor" } }
    ];
    var edges = [
      { data: { id: "e:A-B", source: "t:A:1", target: "t:B:1" } },
      { data: { id: "e:B-Bph", source: "t:B:1", target: "ph:B|root" } }
    ];
    var g = { nodes: nodes, edges: edges };
    // Focus ph:A|sub with hops=2:
    // Core = ph:A|sub, t:A:1
    // Dist 1 = t:B:1 (near)
    // Dist 2 = ph:B|root (near)
    var res = PlanUI.focusSets(g, "ph:A|sub", { hops: 2 });
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)

    # 1. ph: compound includes its nested task descendant
    assert set(res["core"]) == {"ph:A|sub", "t:A:1"}

    # Near includes t:B:1 and ph:B|root
    assert "t:B:1" in res["near"]
    assert "ph:B|root" in res["near"]

    # Context contains ancestor chains of core and near:
    # Core ancestors: ph:A|root, p:A
    # Near ancestors: p:B (ph:B|root is near, so it is not in context)
    assert set(res["context"]) == {"p:A", "ph:A|root", "p:B"}

    # Exclusion 1: context NEVER contains a core or near ancestor
    core_set = set(res["core"])
    near_set = set(res["near"])
    context_set = set(res["context"])
    assert context_set.isdisjoint(core_set)
    assert context_set.isdisjoint(near_set)
    # ph:B|root is ancestor of near node t:B:1, but because it is near, it is excluded from context
    assert "ph:B|root" not in context_set
    # ph:A|sub is ancestor of core node t:A:1, but because it is core, it is excluded from context
    assert "ph:A|sub" not in context_set

    # Exclusion 2: context NEVER contains a non-graph id
    assert "non_existent_ancestor" not in context_set
    for cid in context_set:
        assert any(cid.startswith(prefix) for prefix in ("p:", "ph:", "t:"))

    # Exclusion 3: context NEVER contains the focus itself
    assert "ph:A|sub" not in context_set







