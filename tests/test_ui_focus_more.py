"""Additional tests for PlanUI focus model — GU-G3a-fix rulings R1, R2, R3."""

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
    runner_file = tmp_path / "runner_more.js"
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
def test_r1_triangle_keep_rule(tmp_path: Path):
    """R1: triangle A-B-C with focus A.

    At hops=1: B-C edge is faded (neither end in core or dist < 1).
    At hops=2: B-C edge is kept (both ends in near, dist 1 < 2).
    """
    body = """
    var nodes = [
      { data: { id: "t:A" } },
      { data: { id: "t:B" } },
      { data: { id: "t:C" } }
    ];
    var edges = [
      { data: { id: "e:A-B", source: "t:A", target: "t:B" } },
      { data: { id: "e:A-C", source: "t:A", target: "t:C" } },
      { data: { id: "e:B-C", source: "t:B", target: "t:C" } }
    ];
    var g = { nodes: nodes, edges: edges };
    var h1 = PlanUI.focusSets(g, "t:A", { hops: 1 });
    var h2 = PlanUI.focusSets(g, "t:A", { hops: 2 });
    emit({ h1: h1, h2: h2 });
    """
    res = run_node_snippet(tmp_path, body)
    h1 = res["h1"]
    h2 = res["h2"]

    # Hops 1: A is core, B and C are near
    assert h1["core"] == ["t:A"]
    assert h1["near"] == ["t:B", "t:C"]
    assert "e:A-B" in h1["keepEdges"]
    assert "e:A-C" in h1["keepEdges"]
    # B-C edge is faded at hops 1
    assert "e:B-C" in h1["fadeEdges"]
    assert "e:B-C" not in h1["keepEdges"]

    # Hops 2: A is core, B and C are near
    assert h2["core"] == ["t:A"]
    assert h2["near"] == ["t:B", "t:C"]
    assert "e:A-B" in h2["keepEdges"]
    assert "e:A-C" in h2["keepEdges"]
    # B-C edge is KEPT at hops 2
    assert "e:B-C" in h2["keepEdges"]
    assert "e:B-C" not in h2["fadeEdges"]


@need_node
def test_r3a_parent_cycle_does_not_hang(tmp_path: Path):
    """R3 (a): cycle in parent chain terminates via visited set."""
    body = """
    var nodes = [
      { data: { id: "ph:1", parent: "ph:2" } },
      { data: { id: "ph:2", parent: "ph:3" } },
      { data: { id: "ph:3", parent: "ph:1" } },
      { data: { id: "t:1", parent: "ph:1" } }
    ];
    var g = { nodes: nodes, edges: [] };
    var res = PlanUI.focusSets(g, "t:1");
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert res["core"] == ["t:1"]
    assert set(res["context"]) == {"ph:1", "ph:2", "ph:3"}


@need_node
def test_r3b_null_safe_nodes(tmp_path: Path):
    """R3 (b): null node or node without data is skipped, never throws."""
    body = """
    var nodes = [
      null,
      { data: null },
      {},
      { data: { id: "t:1" } },
      null,
      { data: { id: "t:2" } }
    ];
    var edges = [
      { data: { id: "e:1-2", source: "t:1", target: "t:2" } }
    ];
    var g = { nodes: nodes, edges: edges };
    var res = PlanUI.focusSets(g, "t:1");
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert res["core"] == ["t:1"]
    assert res["near"] == ["t:2"]
    assert res["context"] == []
    assert res["faded"] == []
    assert res["keepEdges"] == ["e:1-2"]


@need_node
def test_r3c_missing_graph_arrays_return_null(tmp_path: Path):
    """R3 (c): focusSets({}, id) and graph without arrays returns null."""
    body = """
    emit({
      emptyObj: PlanUI.focusSets({}, "t:1"),
      noNodes: PlanUI.focusSets({ edges: [] }, "t:1"),
      noEdges: PlanUI.focusSets({ nodes: [] }, "t:1"),
      nodesNotArray: PlanUI.focusSets({ nodes: "invalid", edges: [] }, "t:1"),
      edgesNotArray: PlanUI.focusSets({ nodes: [], edges: 123 }, "t:1"),
      nullGraph: PlanUI.focusSets(null, "t:1"),
      undefinedGraph: PlanUI.focusSets(undefined, "t:1")
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["emptyObj"] is None
    assert res["noNodes"] is None
    assert res["noEdges"] is None
    assert res["nodesNotArray"] is None
    assert res["edgesNotArray"] is None
    assert res["nullGraph"] is None
    assert res["undefinedGraph"] is None


@need_node
def test_r3d_dangling_parent_excluded_from_context(tmp_path: Path):
    """R3 (d): dangling parent not in graph never enters context."""
    body = """
    var nodes = [
      { data: { id: "t:1", parent: "missing_parent_id" } }
    ];
    var g = { nodes: nodes, edges: [] };
    var res = PlanUI.focusSets(g, "t:1");
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert res["core"] == ["t:1"]
    assert res["context"] == []
    assert "missing_parent_id" not in res["context"]
    assert "missing_parent_id" not in res["faded"]


@need_node
def test_r3e_focus_reduce_copies_expand(tmp_path: Path):
    """R3 (e): focusReduce ignores action.expand and state.expand; dbltap computes fresh expand."""
    body = """
    var dummyExpand = { kind: "custom", dummy: 123 };
    var stateWithExpand = { focusId: "t:1", expand: dummyExpand };

    var rTap = PlanUI.focusReduce(stateWithExpand, { type: "tap", id: "t:2", expand: { foo: 1 } });
    var rEsc = PlanUI.focusReduce(stateWithExpand, { type: "esc", expand: { foo: 2 } });
    var rEmpty = PlanUI.focusReduce(stateWithExpand, { type: "tapEmpty", expand: { foo: 3 } });
    var rUnknown = PlanUI.focusReduce(stateWithExpand, { type: "unknown_act", expand: { foo: 4 } });

    var rDblPhc1 = PlanUI.focusReduce(stateWithExpand, { type: "dbltap", id: "phc:phase1", expand: { foo: 5 } });
    var rDblPhc2 = PlanUI.focusReduce(stateWithExpand, { type: "dbltap", id: "phc:phase1", expand: { foo: 6 } });

    var rDblPc = PlanUI.focusReduce(stateWithExpand, { type: "dbltap", id: "pc:proj1", expand: { foo: 7 } });

    emit({
      tapExpand: rTap.expand,
      escExpand: rEsc.expand,
      emptyExpand: rEmpty.expand,
      unknownExpand: rUnknown.expand,
      dblPhcExpand: rDblPhc1.expand,
      dblPcExpand: rDblPc.expand,
      freshBetweenCalls: (rDblPhc1.expand !== rDblPhc2.expand),
      notStateExpand: (rDblPhc1.expand !== dummyExpand)
    });
    """
    data = run_node_snippet(tmp_path, body)
    assert data["tapExpand"] is None
    assert data["escExpand"] is None
    assert data["emptyExpand"] is None
    assert data["unknownExpand"] is None
    assert data["dblPhcExpand"] == {"kind": "phase", "id": "phase1"}
    assert data["dblPcExpand"] == {"kind": "project", "id": "proj1"}
    assert data["freshBetweenCalls"] is True
    assert data["notStateExpand"] is True


@need_node
def test_r3f_dangling_edge_ignored(tmp_path: Path):
    """R3 (f): dangling edge neither makes phantom node near nor is kept."""
    body = """
    var nodes = [
      { data: { id: "t:1" } },
      { data: { id: "t:2" } }
    ];
    var edges = [
      { data: { id: "e:1-2", source: "t:1", target: "t:2" } },
      { data: { id: "e:1-ghost", source: "t:1", target: "missing_ghost_node" } },
      { data: { id: "e:ghost-2", source: "missing_ghost_node_2", target: "t:2" } }
    ];
    var g = { nodes: nodes, edges: edges };
    var res = PlanUI.focusSets(g, "t:1", { hops: 1 });
    emit(res);
    """
    res = run_node_snippet(tmp_path, body)
    assert res["core"] == ["t:1"]
    assert res["near"] == ["t:2"]
    assert "missing_ghost_node" not in res["near"]
    assert "missing_ghost_node_2" not in res["near"]
    assert res["keepEdges"] == ["e:1-2"]
    assert "e:1-ghost" in res["fadeEdges"]
    assert "e:ghost-2" in res["fadeEdges"]
    assert "e:1-ghost" not in res["keepEdges"]
    assert "e:ghost-2" not in res["keepEdges"]


@need_node
def test_partition_invariants_and_purity(tmp_path: Path):
    """Invariants: pairwise disjoint partitions, arrays sorted, inputs not mutated."""
    body = """
    var nodes = [
      Object.freeze({ data: Object.freeze({ id: "p:A" }) }),
      Object.freeze({ data: Object.freeze({ id: "ph:A|1", parent: "p:A" }) }),
      Object.freeze({ data: Object.freeze({ id: "t:A:1", parent: "ph:A|1" }) }),
      Object.freeze({ data: Object.freeze({ id: "t:A:2", parent: "ph:A|1" }) }),
      Object.freeze({ data: Object.freeze({ id: "t:A:3" }) })
    ];
    var edges = [
      Object.freeze({ data: Object.freeze({ id: "e:1-2", source: "t:A:1", target: "t:A:2" }) }),
      Object.freeze({ data: Object.freeze({ id: "e:2-3", source: "t:A:2", target: "t:A:3" }) })
    ];
    var g = Object.freeze({ nodes: Object.freeze(nodes.slice()), edges: Object.freeze(edges.slice()) });
    var res = PlanUI.focusSets(g, "t:A:1", { hops: 1 });
    emit({ res: res });
    """
    data = run_node_snippet(tmp_path, body)
    res = data["res"]
    c, n, x, f = set(res["core"]), set(res["near"]), set(res["context"]), set(res["faded"])
    # Disjoint
    assert c.isdisjoint(n) and c.isdisjoint(x) and c.isdisjoint(f)
    assert n.isdisjoint(x) and n.isdisjoint(f) and x.isdisjoint(f)
    # Union is all valid nodes
    assert c | n | x | f == {"p:A", "ph:A|1", "t:A:1", "t:A:2", "t:A:3"}

    # Edges disjoint and union
    ke, fe = set(res["keepEdges"]), set(res["fadeEdges"])
    assert ke.isdisjoint(fe)
    assert ke | fe == {"e:1-2", "e:2-3"}

    # Sorted
    assert res["core"] == sorted(res["core"])
    assert res["near"] == sorted(res["near"])
    assert res["context"] == sorted(res["context"])
    assert res["faded"] == sorted(res["faded"])
    assert res["keepEdges"] == sorted(res["keepEdges"])
    assert res["fadeEdges"] == sorted(res["fadeEdges"])
