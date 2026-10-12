"""Node-run tests for PlanUI cluster model strengthening (GU-G1-STRENGTHEN S1-S4)."""

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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ui" / "synthetic_snapshot.json"

sys.path.insert(0, str(SRC))

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

RUNNER_JS = r"""
"use strict";
var fs = require("fs"), mode = process.argv[2];
require(process.argv[3]);
var PlanUI = require(process.argv[4]);
require(process.argv[5]); require(process.argv[6]);
require(process.argv[7]); require(process.argv[8]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

function makeVm(projectsData, itemsData) {
  var snap = {
    generated: "2026-10-04T00:00:00Z",
    head: "test-snap",
    items: itemsData
  };
  return PlanUI.buildViewModel(snap);
}

if (mode === "s1_edge_budget") {
  // Build a VM with 1 project having 8 phases
  var phases = [];
  for (var i = 0; i < 8; i++) phases.push("ph" + i);
  var items = [];
  phases.forEach(function (ph, idx) {
    items.push({
      project: "p0", id: "T-" + idx, key: "p0:T-" + idx, title: "T" + idx,
      kind: "lane", track: ph, phase: ph, owner: "O", state: "ready", estimate_h: 1,
      depends_on: [], operator: false, priority: 1, epoch: 1, evidence: []
    });
  });
  // Also project p1 with 2 phases
  items.push({
    project: "p1", id: "T-8", key: "p1:T-8", title: "T8",
    kind: "lane", track: "p1ph0", phase: "p1ph0", owner: "O", state: "ready", estimate_h: 1,
    depends_on: [], operator: false, priority: 1, epoch: 1, evidence: []
  });
  var vm = makeVm(null, items);

  function budgetForN(n) {
    // Expand n phases of p0
    var cPh = new Set();
    phases.forEach(function (ph, idx) {
      if (idx >= n) cPh.add("p0|" + ph);
    });
    cPh.add("p1|p1ph0");
    return PlanUI.edgeBudget(vm, { collapsedProjects: new Set(), collapsedPhases: cPh });
  }

  // Also test collapsedProjects: p0 has expanded phases in cPhases (none in cPhases),
  // but p0 is in collapsedProjects
  var collapsedState = {
    collapsedProjects: new Set(["p0"]),
    collapsedPhases: new Set() // all phases expanded according to cPhases
  };
  var bCollapsed = PlanUI.edgeBudget(vm, collapsedState);

  emit({
    b0: budgetForN(0),
    b1: budgetForN(1),
    b2: budgetForN(2),
    b3: budgetForN(3),
    b7: budgetForN(7),
    bCollapsed: bCollapsed
  });
} else if (mode === "s2_show_all_edges") {
  var items = [
    { project: "p0", id: "T-1", key: "p0:T-1", title: "T1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "done", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-2", key: "p0:T-2", title: "T2", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "done", estimate_h: 1, depends_on: ["p0:T-1"], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-3", key: "p0:T-3", title: "T3", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-4", key: "p0:T-4", title: "T4", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-3"], operator: false, priority: 1, epoch: 1, evidence: [] }
  ];
  var vm = makeVm(null, items);
  var st = { collapsedProjects: new Set(), collapsedPhases: new Set() };
  var resDefault = PlanUI.metaEdges(vm, st, { showAllEdges: false });
  var resAll = PlanUI.metaEdges(vm, st, { showAllEdges: true });
  emit({
    defTotal: resDefault.total,
    defEdgeIds: resDefault.edges.map(function (e) { return e.id; }),
    allTotal: resAll.total,
    allEdgeIds: resAll.edges.map(function (e) { return e.id; })
  });
} else if (mode === "s3_aggregation") {
  // Part A: Two source reps to one target, one source rep to two targets (un-lifted)
  var itemsA = [
    { project: "p0", id: "T-1", key: "p0:T-1", title: "T1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-2", key: "p0:T-2", title: "T2", kind: "lane", track: "ph1", phase: "ph1", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-3", key: "p0:T-3", title: "T3", kind: "lane", track: "ph2", phase: "ph2", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-1", "p0:T-2"], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-4", key: "p0:T-4", title: "T4", kind: "lane", track: "ph3", phase: "ph3", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-3"], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-5", key: "p0:T-5", title: "T5", kind: "lane", track: "ph4", phase: "ph4", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-3"], operator: false, priority: 1, epoch: 1, evidence: [] }
  ];
  var vmA = makeVm(null, itemsA);
  var stA = { collapsedProjects: new Set(), collapsedPhases: new Set() };
  var resA = PlanUI.metaEdges(vmA, stA, { limit: 100 });

  // Part B: Project-level lift
  var itemsB = [
    // p0:A1 not critical, p1:B1 critical -> pair critical is false
    { project: "p0", id: "A1", key: "p0:A1", title: "A1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [], critical: false },
    { project: "p1", id: "B1", key: "p1:B1", title: "B1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:A1"], operator: false, priority: 1, epoch: 1, evidence: [], critical: true },
    // p0:A2 critical, p1:B2 critical -> pair critical is true
    { project: "p0", id: "A2", key: "p0:A2", title: "A2", kind: "lane", track: "ph1", phase: "ph1", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [], critical: true },
    { project: "p1", id: "B2", key: "p1:B2", title: "B2", kind: "lane", track: "ph1", phase: "ph1", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:A2"], operator: false, priority: 1, epoch: 1, evidence: [], critical: true }
  ];
  var vmB = makeVm(null, itemsB);
  var stB = { collapsedProjects: new Set(), collapsedPhases: new Set() };
  var resB = PlanUI.metaEdges(vmB, stB, { limit: 1 });

  emit({
    unliftedEdges: resA.edges,
    liftedEdges: resB.edges
  });
} else if (mode === "s4_priority") {
  var items = [
    // Internal edge (p0|ph0)
    { project: "p0", id: "T-int1", key: "p0:T-int1", title: "int1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-int2", key: "p0:T-int2", title: "int2", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-int1"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // E1: task-ended, critical=false, cross=false, count=1 (T-t1 in ph0 -> T-t2 in ph1)
    { project: "p0", id: "T-t2", key: "p0:T-t2", title: "t2", kind: "lane", track: "ph1", phase: "ph1", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-int1"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // E2: meta-ended, critical=true, cross=false, count=1 (ph_crit_a -> ph_crit_b)
    { project: "p0", id: "T-ca", key: "p0:T-ca", title: "ca", kind: "lane", track: "ph_crit_a", phase: "ph_crit_a", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [], critical: true },
    { project: "p0", id: "T-cb", key: "p0:T-cb", title: "cb", kind: "lane", track: "ph_crit_b", phase: "ph_crit_b", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-ca"], operator: false, priority: 1, epoch: 1, evidence: [], critical: true },

    // E3: meta-ended, critical=false, cross=true, count=1 (ph_cross -> pc:p1)
    { project: "p0", id: "T-cr", key: "p0:T-cr", title: "cr", kind: "lane", track: "ph_cross", phase: "ph_cross", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p1", id: "T-p1", key: "p1:T-p1", title: "p1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-cr"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // E4: meta-ended, critical=false, cross=false, count=2 (ph_cnt_a -> ph_cnt_b)
    { project: "p0", id: "T-c1", key: "p0:T-c1", title: "c1", kind: "lane", track: "ph_cnt_a", phase: "ph_cnt_a", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-c2", key: "p0:T-c2", title: "c2", kind: "lane", track: "ph_cnt_a", phase: "ph_cnt_a", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-d1", key: "p0:T-d1", title: "d1", kind: "lane", track: "ph_cnt_b", phase: "ph_cnt_b", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-c1"], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-d2", key: "p0:T-d2", title: "d2", kind: "lane", track: "ph_cnt_b", phase: "ph_cnt_b", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-c2"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // E5: meta-ended, count=1, id_a (ph_ida -> ph_idb)
    { project: "p0", id: "T-ida", key: "p0:T-ida", title: "ida", kind: "lane", track: "ph_ida", phase: "ph_ida", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-idb", key: "p0:T-idb", title: "idb", kind: "lane", track: "ph_idb", phase: "ph_idb", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-ida"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // E6: meta-ended, count=1, id_z (ph_idz1 -> ph_idz2)
    { project: "p0", id: "T-idz1", key: "p0:T-idz1", title: "idz1", kind: "lane", track: "ph_idz1", phase: "ph_idz1", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "p0", id: "T-idz2", key: "p0:T-idz2", title: "idz2", kind: "lane", track: "ph_idz2", phase: "ph_idz2", owner: "O", state: "ready", estimate_h: 1, depends_on: ["p0:T-idz1"], operator: false, priority: 1, epoch: 1, evidence: [] }
  ];
  var vm = makeVm(null, items);
  var st = {
    collapsedProjects: new Set(["p1"]),
    collapsedPhases: new Set([
      "p0|ph_crit_a", "p0|ph_crit_b",
      "p0|ph_cross",
      "p0|ph_cnt_a", "p0|ph_cnt_b",
      "p0|ph_ida", "p0|ph_idb",
      "p0|ph_idz1", "p0|ph_idz2"
    ])
  };
  var resFull = PlanUI.metaEdges(vm, st, { limit: 100 });

  // Budget biting test: 4 project-level edges competing for limit: 3
  var biteItems = [
    // P0 -> P1 (critical: true, count: 1)
    { project: "bp0", id: "T-1", key: "bp0:T-1", title: "b1", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [], critical: true },
    { project: "bp1", id: "T-2", key: "bp1:T-2", title: "b2", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: ["bp0:T-1"], operator: false, priority: 1, epoch: 1, evidence: [], critical: true },

    // P0 -> P2 (critical: false, count: 2)
    { project: "bp0", id: "T-3", key: "bp0:T-3", title: "b3", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "bp0", id: "T-4", key: "bp0:T-4", title: "b4", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "bp2", id: "T-5", key: "bp2:T-5", title: "b5", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: ["bp0:T-3"], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "bp2", id: "T-6", key: "bp2:T-6", title: "b6", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: ["bp0:T-4"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // P0 -> P3 (critical: false, count: 1, id 'bp3')
    { project: "bp0", id: "T-7", key: "bp0:T-7", title: "b7", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "bp3", id: "T-8", key: "bp3:T-8", title: "b8", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: ["bp0:T-7"], operator: false, priority: 1, epoch: 1, evidence: [] },

    // P0 -> P4 (critical: false, count: 1, id 'bp4')
    { project: "bp0", id: "T-9", key: "bp0:T-9", title: "b9", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: [], operator: false, priority: 1, epoch: 1, evidence: [] },
    { project: "bp4", id: "T-10", key: "bp4:T-10", title: "b10", kind: "lane", track: "ph", phase: "ph", owner: "O", state: "ready", estimate_h: 1, depends_on: ["bp0:T-9"], operator: false, priority: 1, epoch: 1, evidence: [] }
  ];
  var biteVm = makeVm(null, biteItems);
  var biteState = { collapsedProjects: new Set(["bp0", "bp1", "bp2", "bp3", "bp4"]), collapsedPhases: new Set() };
  var resBite = PlanUI.metaEdges(biteVm, biteState, { limit: 3 });

  emit({
    fullEdgeIds: resFull.edges.map(function (e) { return e.id; }),
    biteEdgeIds: resBite.edges.map(function (e) { return e.id; }),
    biteHidden: resBite.hidden
  });
}
"""


def run_node(tmp_path: Path, mode: str, *args) -> dict:
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    cmd = [
        "node",
        str(runner_path),
        mode,
        str(WEB / "page.js"),
        str(WEB / "page_model.js"),
        str(WEB / "page_store.js"),
        str(WEB / "page_search.js"),
        str(WEB / "page_graph.js"),
        str(WEB / "page_graph_cluster.js"),
        str(FIXTURE),
    ] + [str(a) for a in args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_s1_edge_budget(tmp_path):
    res = run_node(tmp_path, "s1_edge_budget")
    assert res["b0"] == 40
    assert res["b1"] == 150
    assert res["b2"] == 200
    assert res["b3"] == 250
    assert res["b7"] == 400
    # collapsed project p0 has 8 phases not in collapsedPhases, but because p0 is collapsed,
    # only p1's 1 phase counts as expanded (n = 1 -> budget 150)
    assert res["bCollapsed"] == 150


@need_node
def test_s2_show_all_edges(tmp_path):
    res = run_node(tmp_path, "s2_show_all_edges")
    assert res["defTotal"] == 1
    assert res["defEdgeIds"] == ["m:t:p0:T-3->t:p0:T-4"]
    assert res["allTotal"] == 2
    assert sorted(res["allEdgeIds"]) == [
        "m:t:p0:T-1->t:p0:T-2",
        "m:t:p0:T-3->t:p0:T-4",
    ]


@need_node
def test_s3_aggregation(tmp_path):
    res = run_node(tmp_path, "s3_aggregation")
    unlifted = res["unliftedEdges"]
    assert len(unlifted) == 4
    # All unlifted task-level edges
    for e in unlifted:
        assert e["exact"] is True
        assert e["level"] == "task"
        assert len(e["pairs"]) == 1
        assert e["count"] == 1

    by_id = {e["id"]: e for e in unlifted}
    assert "m:t:p0:T-1->t:p0:T-3" in by_id
    assert "m:t:p0:T-2->t:p0:T-3" in by_id
    assert "m:t:p0:T-3->t:p0:T-4" in by_id
    assert "m:t:p0:T-3->t:p0:T-5" in by_id

    # Lifted edges
    lifted = res["liftedEdges"]
    assert len(lifted) == 1
    edge = lifted[0]
    assert edge["id"] == "m:pc:p0->pc:p1"
    assert edge["exact"] is False
    assert edge["count"] == 2
    assert edge["level"] == "meta"
    assert edge["crossProject"] is True
    assert edge["critical"] is True
    assert edge["pairs"] == [
        {"from": "p0:A1", "to": "p1:B1"},
        {"from": "p0:A2", "to": "p1:B2"},
    ]


@need_node
def test_s4_priority_order_and_budget_truncation(tmp_path):
    res = run_node(tmp_path, "s4_priority")
    expected_full = [
        "m:t:p0:T-int1->t:p0:T-int2",  # internal edge first
        "m:t:p0:T-int1->t:p0:T-t2",  # rule 1: task-ended first
        "m:phc:p0|ph_crit_a->phc:p0|ph_crit_b",  # rule 2: critical first
        "m:phc:p0|ph_cross->pc:p1",  # rule 3: crossProject first
        "m:phc:p0|ph_cnt_a->phc:p0|ph_cnt_b",  # rule 4: count (2) descending
        "m:phc:p0|ph_ida->phc:p0|ph_idb",  # rule 5: id ascending (ida < idz1)
        "m:phc:p0|ph_idz1->phc:p0|ph_idz2",
    ]
    assert res["fullEdgeIds"] == expected_full

    # Budget bites with limit 3: 4 project-level edges competing for 3 slots
    # E1: critical (beats all non-critical)
    # E2: count=2 (beats count=1)
    # E3: count=1, id="m:pc:bp0->pc:bp3" (beats id="m:pc:bp0->pc:bp4")
    # E4: cut! hidden = 1
    assert res["biteEdgeIds"] == [
        "m:pc:bp0->pc:bp1",
        "m:pc:bp0->pc:bp2",
        "m:pc:bp0->pc:bp3",
    ]
    assert res["biteHidden"] == 1



