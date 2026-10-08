"""Additional tests for PlanUI focus model — GU-G3a strengthen (part 2)."""

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
    runner_file = tmp_path / "runner_more3.js"
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
def test_g5_purity_deep_frozen_graph_and_labels_preserved(tmp_path: Path):
    """G5: deep-frozen graph with labels and extra properties is not mutated."""
    body = """
    function deepFreeze(o) {
      Object.freeze(o);
      Object.getOwnPropertyNames(o).forEach(function (prop) {
        if (o[prop] !== null && (typeof o[prop] === "object" || typeof o[prop] === "function") && !Object.isFrozen(o[prop])) {
          deepFreeze(o[prop]);
        }
      });
      return o;
    }

    var nodes = [
      { data: { id: "p:1", label: "Project One", meta: { foo: 123 } } },
      { data: { id: "t:1", parent: "p:1", label: "Task One", customTag: "alpha" } },
      { data: { id: "t:2", label: "Task Two", customTag: "beta" } }
    ];
    var edges = [
      { data: { id: "e:1-2", source: "t:1", target: "t:2", label: "depends-on" } }
    ];
    var g = deepFreeze({ nodes: nodes, edges: edges });

    var res = PlanUI.focusSets(g, "t:1", { hops: 1 });

    // Verify properties remain intact on input
    emit({
      res: res,
      node0Label: g.nodes[0].data.label,
      node1Label: g.nodes[1].data.label,
      node1Custom: g.nodes[1].data.customTag,
      edge0Label: g.edges[0].data.label
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["node0Label"] == "Project One"
    assert res["node1Label"] == "Task One"
    assert res["node1Custom"] == "alpha"
    assert res["edge0Label"] == "depends-on"
    assert res["res"]["core"] == ["t:1"]
    assert res["res"]["near"] == ["t:2"]
    assert res["res"]["context"] == ["p:1"]


@need_node
def test_g5_purity_result_id_contract_and_unknown_id(tmp_path: Path):
    """G5: result id field equals focus id, null returned for unknown id, dbltap on frozen state."""
    body = """
    var g = {
      nodes: [{ data: { id: "t:1" } }],
      edges: []
    };
    var knownRes = PlanUI.focusSets(g, "t:1");
    var unknownRes = PlanUI.focusSets(g, "t:nonexistent");

    // dbltap on frozen state
    var frozenState = Object.freeze({ focusId: "t:1", expand: null });
    var frozenAction = Object.freeze({ type: "dbltap", id: "phc:p1|ph1" });
    var reduced = PlanUI.focusReduce(frozenState, frozenAction);

    emit({
      knownResId: knownRes.id,
      unknownRes: unknownRes,
      reduced: reduced
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["knownResId"] == "t:1"
    assert res["unknownRes"] is None
    assert res["reduced"]["focusId"] == "ph:p1|ph1"
    assert res["reduced"]["expand"] == {"kind": "phase", "id": "p1|ph1"}


@need_node
def test_g6_focus_after_rebuild_phc_to_ph_and_pc_to_p(tmp_path: Path):
    """G6: focusAfterRebuild mapping phc->ph, pc->p, and neither existing returning null."""
    body = """
    var graph = {
      nodes: [
        { data: { id: "ph:proj1|core" } },
        { data: { id: "p:proj2" } }
      ]
    };

    var resPhcToPh = PlanUI.focusAfterRebuild(graph, "phc:proj1|core");
    var resPcToP = PlanUI.focusAfterRebuild(graph, "pc:proj2");
    var resNeither = PlanUI.focusAfterRebuild(graph, "phc:ghost|phase");

    emit({
      resPhcToPh: resPhcToPh,
      resPcToP: resPcToP,
      resNeither: resNeither
    });
    """
    res = run_node_snippet(tmp_path, body)
    assert res["resPhcToPh"] == "ph:proj1|core"
    assert res["resPcToP"] == "p:proj2"
    assert res["resNeither"] is None


@need_node
def test_g7_focus_label_boundary_and_missing_items(tmp_path: Path):
    """G7: focusLabel 60/61 boundary, unknown task key, item without title."""
    body = """
    var t60 = "A".repeat(60);
    var t61 = "A".repeat(60) + "B";

    var itemStore = {
      "k60": { title: t60 },
      "k61": { title: t61 },
      "kEmpty": { title: "" },
      "kNoTitle": {}
    };
    var vm = {
      items: {
        get: function(key) { return itemStore[key] || null; }
      }
    };

    var lbl60 = PlanUI.focusLabel(vm, "t:k60");
    var lbl61 = PlanUI.focusLabel(vm, "t:k61");
    var lblUnknown = PlanUI.focusLabel(vm, "t:nonexistent");
    var lblEmpty = PlanUI.focusLabel(vm, "t:kEmpty");
    var lblNoTitle = PlanUI.focusLabel(vm, "t:kNoTitle");
    var lblPh = PlanUI.focusLabel(vm, "ph:projA|phase1");
    var lblP = PlanUI.focusLabel(vm, "p:projA");
    var lblNull = PlanUI.focusLabel(vm, null);

    emit({
      lbl60: lbl60,
      lbl61: lbl61,
      lblUnknown: lblUnknown,
      lblEmpty: lblEmpty,
      lblNoTitle: lblNoTitle,
      lblPh: lblPh,
      lblP: lblP,
      lblNull: lblNull
    });
    """
    res = run_node_snippet(tmp_path, body)
    # 60 characters: NOT truncated
    assert res["lbl60"] == "k60 - " + ("A" * 60)
    assert not res["lbl60"].endswith("…")

    # 61 characters: truncated to 60 + "…"
    assert res["lbl61"] == "k61 - " + ("A" * 60) + "…"
    assert res["lbl61"].endswith("…")

    # Unknown key -> ""
    assert res["lblUnknown"] == ""

    # Item with empty title or no title -> "key - "
    assert res["lblEmpty"] == "kEmpty - "
    assert res["lblNoTitle"] == "kNoTitle - "

    # Phase and project formatting
    assert res["lblPh"] == "projA > phase1"
    assert res["lblP"] == "projA"
    assert res["lblNull"] == ""


@need_node
def test_g7_focus_classes_null_and_hops_normalization(tmp_path: Path):
    """G7: focusClasses(null) returns empty maps; non-integer/clamped hops normalization."""
    body = """
    var emptyClasses = PlanUI.focusClasses(null);

    // Linear chain: 1 - 2 - 3 - 4 - 5
    var nodes = [
      { data: { id: "t:1" } },
      { data: { id: "t:2" } },
      { data: { id: "t:3" } },
      { data: { id: "t:4" } },
      { data: { id: "t:5" } }
    ];
    var edges = [
      { data: { id: "e:1-2", source: "t:1", target: "t:2" } },
      { data: { id: "e:2-3", source: "t:2", target: "t:3" } },
      { data: { id: "e:3-4", source: "t:3", target: "t:4" } },
      { data: { id: "e:4-5", source: "t:4", target: "t:5" } }
    ];
    var g = { nodes: nodes, edges: edges };

    var hFloat = PlanUI.focusSets(g, "t:1", { hops: 1.5 });
    var hString = PlanUI.focusSets(g, "t:1", { hops: "2" });
    var hNaN = PlanUI.focusSets(g, "t:1", { hops: NaN });
    var hNeg = PlanUI.focusSets(g, "t:1", { hops: -1 });
    var hZero = PlanUI.focusSets(g, "t:1", { hops: 0 });
    var h99 = PlanUI.focusSets(g, "t:1", { hops: 99 });

    emit({
      emptyClasses: emptyClasses,
      hFloat: { near: hFloat.near, keep: hFloat.keepEdges },
      hString: { near: hString.near, keep: hString.keepEdges },
      hNaN: { near: hNaN.near, keep: hNaN.keepEdges },
      hNeg: { near: hNeg.near, keep: hNeg.keepEdges },
      hZero: { near: hZero.near, keep: hZero.keepEdges },
      h99: { near: h99.near, keep: h99.keepEdges }
    });
    """
    res = run_node_snippet(tmp_path, body)

    # focusClasses(null) returns empty maps
    assert res["emptyClasses"] == {"nodes": {}, "edges": {}}

    # 1.5, '2', NaN, -1, 0 are normalized to 1 hop
    for key in ("hFloat", "hString", "hNaN", "hNeg", "hZero"):
        assert res[key]["near"] == ["t:2"], f"Failed for {key}"
        assert res[key]["keep"] == ["e:1-2"], f"Failed for {key}"

    # 99 is clamped to 3 hops (near: t:2, t:3, t:4; keep: e:1-2, e:2-3, e:3-4)
    assert res["h99"]["near"] == ["t:2", "t:3", "t:4"]
    assert res["h99"]["keep"] == ["e:1-2", "e:2-3", "e:3-4"]



