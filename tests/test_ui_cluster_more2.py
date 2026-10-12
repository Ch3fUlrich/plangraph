"""Node-run tests for PlanUI cluster model strengthening (GU-G1-STRENGTHEN S5-S8)."""

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

function itm(p, id, ph, st, deps, crit) {
  return { project: p, id: id, key: p + ":" + id, title: id, kind: "lane", track: ph, phase: ph, owner: "O", state: st || "ready", estimate_h: 1, depends_on: deps || [], operator: false, priority: 1, epoch: 1, evidence: [], critical: !!crit };
}
function makeVm(items) {
  return PlanUI.buildViewModel({ generated: "2026-10-04T00:00:00Z", head: "h2", items: items });
}

if (mode === "s5_edge_fields") {
  var items = [
    itm("p0", "T1", "ph0", "ready", [], true),
    itm("p0", "T2", "ph1", "ready", ["p0:T1"], true),
    itm("p0", "T3", "ph0", "ready", [], true),
    itm("p0", "T4", "ph1", "ready", ["p0:T3"], false),
    itm("p0", "T5", "ph0", "ready", [], false),
    itm("p0", "T6", "ph1", "ready", ["p0:T5"], true),
    itm("p0", "T7", "ph0", "ready", [], false),
    itm("p0", "T8", "ph1", "ready", ["p0:T7"], false)
  ];
  var vm = makeVm(items);
  vm.items.get("p0:T1").critical = true; vm.items.get("p0:T2").critical = true;
  vm.items.get("p0:T3").critical = true; vm.items.get("p0:T4").critical = false;
  vm.items.get("p0:T5").critical = false; vm.items.get("p0:T6").critical = true;
  vm.items.get("p0:T7").critical = false; vm.items.get("p0:T8").critical = false;

  var stTask = { collapsedProjects: new Set(), collapsedPhases: new Set() };
  var resTask = PlanUI.metaEdges(vm, stTask, { limit: 50 });
  var stCol = { collapsedProjects: new Set(), collapsedPhases: new Set(["p0|ph0", "p0|ph1"]) };
  var resCol = PlanUI.metaEdges(vm, stCol, { limit: 50 });

  var vmNC = makeVm([items[2], items[3], items[6], items[7]]);
  vmNC.items.get("p0:T3").critical = true; vmNC.items.get("p0:T4").critical = false;
  vmNC.items.get("p0:T7").critical = false; vmNC.items.get("p0:T8").critical = false;
  var resNC = PlanUI.metaEdges(vmNC, stCol, { limit: 50 });

  emit({
    taskEdges: resTask.edges.map(function (e) {
      return { id: e.id, level: e.level, critical: e.critical, pairs: e.pairs, exact: e.exact };
    }),
    mergedEdge: resCol.edges[0],
    nonCritMergedEdge: resNC.edges[0]
  });
} else if (mode === "s6_purity_and_validation") {
  function deepFreeze(obj) {
    if (obj === null || typeof obj !== "object") return obj;
    Object.freeze(obj);
    Object.getOwnPropertyNames(obj).forEach(function (p) {
      if (obj[p] !== null && typeof obj[p] === "object") deepFreeze(obj[p]);
    });
    return obj;
  }
  var vm = makeVm([itm("p0", "T1", "ph0", "ready", []), itm("p0", "T2", "ph0", "ready", ["p0:T1"])]);
  deepFreeze(vm);
  var st0 = { collapsedProjects: Object.freeze(new Set(["p0"])), collapsedPhases: Object.freeze(new Set(["p0|ph0"])) };
  Object.freeze(st0);

  var tProj = PlanUI.toggleCluster(st0, "project", "p0");
  var tPhase = PlanUI.toggleCluster(st0, "phase", "p0|ph0");
  var tUnk = PlanUI.toggleCluster(st0, "invalid_kind", "p0");
  var expAll = PlanUI.expandAllIn(st0, vm, "p0");
  var ovExp = PlanUI.overviewState(vm, { level: "project", expandProjects: ["p0"] });
  var expB = PlanUI.edgeBudget(vm, st0);

  emit({
    toggledOnceOpenedP0: !tProj.collapsedProjects.has("p0"),
    st0P0StillCollapsed: st0.collapsedProjects.has("p0"),
    toggledPhaseOpenedPh: !tPhase.collapsedPhases.has("p0|ph0"),
    st0PhStillCollapsed: st0.collapsedPhases.has("p0|ph0"),
    unkIsNewObj: tUnk !== st0,
    unkSetsEqual: Array.from(tUnk.collapsedProjects).join(",") === Array.from(st0.collapsedProjects).join(",") &&
      Array.from(tUnk.collapsedPhases).join(",") === Array.from(st0.collapsedPhases).join(","),
    expAllOpenedP0: !expAll.collapsedProjects.has("p0") && !expAll.collapsedPhases.has("p0|ph0"),
    ovExpandProjectsOpened: !ovExp.collapsedProjects.has("p0"),
    expectedBudget: expB,
    limFloat: PlanUI.metaEdges(vm, st0, { limit: 2.5 }).limit,
    limStr: PlanUI.metaEdges(vm, st0, { limit: "5" }).limit,
    limNaN: PlanUI.metaEdges(vm, st0, { limit: NaN }).limit,
    limNeg: PlanUI.metaEdges(vm, st0, { limit: -1 }).limit,
    limZero: PlanUI.metaEdges(vm, st0, { limit: 0 }).limit,
    limValid: PlanUI.metaEdges(vm, st0, { limit: 5 }).limit
  });
} else if (mode === "s7_breakdown_and_summary") {
  var pairs = [], bdItems = [];
  for (var i = 0; i < 25; i++) {
    pairs.push({ from: "p0:s" + i, to: "p1:t" + i });
    bdItems.push(itm("p0", "s" + i, "ph", "ready", []));
    bdItems.push(itm("p1", "t" + i, "ph", "ready", ["p0:s" + i]));
  }
  var bdVm = makeVm(bdItems);
  var mockEdge = { id: "m:pc:p0->pc:p1", pairs: pairs };
  var bdDef = PlanUI.edgeBreakdown(bdVm, mockEdge);
  var bd5 = PlanUI.edgeBreakdown(bdVm, mockEdge, 5);
  var bdNull = PlanUI.edgeBreakdown(bdVm, null);

  var sumItems = [itm("p1", "d1", "ph0", "done", [])];
  for (var r = 1; r <= 4; r++) sumItems.push(itm("p1", "r" + r, "ph0", "ready", []));
  for (var b = 1; b <= 3; b++) sumItems.push(itm("p1", "b" + b, "ph0", "ready", ["p1:r1"]));
  for (var d = 2; d <= 6; d++) sumItems.push(itm("p1", "d" + d, "ph1", "done", []));
  sumItems.push(itm("p1", "r5", "ph1", "ready", []));
  for (var b2 = 4; b2 <= 5; b2++) sumItems.push(itm("p1", "b" + b2, "ph1", "ready", ["p1:r5"]));

  var sumVm = makeVm(sumItems);
  sumVm.projects.unshift({ name: "p0", label: "Project 0", color: "#ccc", phases: [] });
  var cs = PlanUI.clusterSummary(sumVm, { collapsedProjects: new Set(["p0"]), collapsedPhases: new Set(["p1|ph0"]) });

  emit({
    bdDefRowsLen: bdDef.rows.length, bdDefMore: bdDef.more, bdFirstRow: bdDef.rows[0],
    bd5RowsLen: bd5.rows.length, bd5More: bd5.more, bdNull: bdNull, clusterSummary: cs
  });
} else if (mode === "s8_g1fix_internal_edges") {
  // (a) 62 internal edges in p0|ph0:
  // Edge 0..44 (45 edges): critical=true, count=1 -> slots 0..44
  // Edge 45..54 (10 edges): critical=false, count=2 -> slots 45..49 survive (5 edges), slots 50..54 surplus (5 edges, pairs=10)
  // Edge 55..61 (7 edges): critical=false, count=1 -> surplus (7 edges, pairs=7)
  // Total surplus pairs in intraHidden = 5*2 + 7*1 = 17
  var aItems = [];
  // 45 critical edges: src_c_i -> tgt_c_i
  for (var ci = 0; ci < 45; ci++) {
    aItems.push(itm("p0", "sc" + ci, "ph0", "ready", []));
    aItems.push(itm("p0", "tc" + ci, "ph0", "ready", ["p0:sc" + ci]));
  }
  // 10 count=2 edges: src_k2_i -> tgt_k2_i (deps has src twice)
  for (var k = 0; k < 10; k++) {
    aItems.push(itm("p0", "sk" + k, "ph0", "ready", []));
    aItems.push(itm("p0", "tk" + k, "ph0", "ready", ["p0:sk" + k, "p0:sk" + k]));
  }
  // 7 count=1 edges: src_k1_i -> tgt_k1_i
  for (var j = 0; j < 7; j++) {
    aItems.push(itm("p0", "sj" + j, "ph0", "ready", []));
    aItems.push(itm("p0", "tj" + j, "ph0", "ready", ["p0:sj" + j]));
  }
  var avm = makeVm(aItems);
  for (var m = 0; m < 45; m++) {
    avm.items.get("p0:sc" + m).critical = true;
    avm.items.get("p0:tc" + m).critical = true;
  }
  var aSt = { collapsedProjects: new Set(), collapsedPhases: new Set() };
  var aRes = PlanUI.metaEdges(avm, aSt, { limit: 100 });

  // (b) Same project, different phases (p0|ph0 expanded, p0|ph1 collapsed): NOT internal
  var bItems = [itm("p0", "bp1", "ph0", "ready", []), itm("p0", "bp2", "ph1", "ready", ["p0:bp1"])];
  var bvm = makeVm(bItems);
  var bRes = PlanUI.metaEdges(bvm, { collapsedProjects: new Set(), collapsedPhases: new Set(["p0|ph1"]) });
  var bTaskEdges = bRes.edges.filter(function (e) { return e.level === "task"; });

  // (c) Done-done internal pairs: skipped without showAllEdges
  var cItems = [itm("p0", "cd1", "ph0", "done", []), itm("p0", "cd2", "ph0", "done", ["p0:cd1"])];
  var cvm = makeVm(cItems);
  var cNoShow = PlanUI.metaEdges(cvm, aSt, { showAllEdges: false });
  var cShow = PlanUI.metaEdges(cvm, aSt, { showAllEdges: true });

  // (d) Limit smaller than internal edge count: cut to limit, cut count goes to hidden
  var dItems = [
    itm("p0", "d1", "ph0", "ready", []), itm("p0", "d2", "ph0", "ready", ["p0:d1"]),
    itm("p0", "d3", "ph0", "ready", []), itm("p0", "d4", "ph0", "ready", ["p0:d3", "p0:d3"])
  ];
  var dvm = makeVm(dItems);
  dvm.items.get("p0:d1").critical = false;
  dvm.items.get("p0:d2").critical = false;
  dvm.items.get("p0:d3").critical = false;
  dvm.items.get("p0:d4").critical = false;
  var dRes = PlanUI.metaEdges(dvm, aSt, { limit: 1 });

  emit({
    aEdgeCount: aRes.edges.length,
    aEdgeIds: aRes.edges.map(function (e) { return e.id; }),
    aIntraHidden: aRes.intraHidden,
    bTaskEdgesCount: bTaskEdges.length,
    bEdges: bRes.edges.map(function (e) { return e.id; }),
    cNoShowEdgesCount: cNoShow.edges.length,
    cNoShowTotal: cNoShow.total,
    cShowEdgesCount: cShow.edges.length,
    cShowTotal: cShow.total,
    dEdgesCount: dRes.edges.length,
    dEdgeIds: dRes.edges.map(function (e) { return e.id; }),
    dHidden: dRes.hidden
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
def test_s5_edge_fields(tmp_path):
    res = run_node(tmp_path, "s5_edge_fields")
    task_edges = {e["id"]: e for e in res["taskEdges"]}
    # Pair 1: both critical -> critical: true
    e1 = task_edges["m:t:p0:T1->t:p0:T2"]
    assert (
        e1["critical"] is True
        and e1["level"] == "task"
        and e1["exact"] is True
        and e1["pairs"] == [{"from": "p0:T1", "to": "p0:T2"}]
    )
    # Pairs 2, 3, 4: not both critical -> critical: false
    assert task_edges["m:t:p0:T3->t:p0:T4"]["critical"] is False
    assert task_edges["m:t:p0:T5->t:p0:T6"]["critical"] is False
    assert task_edges["m:t:p0:T7->t:p0:T8"]["critical"] is False
    # Merge with critical pair -> critical is OR (true)
    assert (
        res["mergedEdge"]["id"] == "m:phc:p0|ph0->phc:p0|ph1"
        and res["mergedEdge"]["critical"] is True
        and res["mergedEdge"]["count"] == 4
    )
    # Merge with only non-critical pairs -> critical is false
    assert (
        res["nonCritMergedEdge"]["critical"] is False
        and res["nonCritMergedEdge"]["count"] == 2
    )


@need_node
def test_s6_purity_and_validation(tmp_path):
    res = run_node(tmp_path, "s6_purity_and_validation")
    assert res["toggledOnceOpenedP0"] and res["st0P0StillCollapsed"]
    assert res["toggledPhaseOpenedPh"] and res["st0PhStillCollapsed"]
    assert res["unkIsNewObj"] and res["unkSetsEqual"]
    assert res["expAllOpenedP0"] and res["ovExpandProjectsOpened"]
    b = res["expectedBudget"]
    assert (
        res["limFloat"] == b
        and res["limStr"] == b
        and res["limNaN"] == b
        and res["limNeg"] == b
        and res["limZero"] == b
    )
    assert res["limValid"] == 5


@need_node
def test_s7_breakdown_and_summary(tmp_path):
    res = run_node(tmp_path, "s7_breakdown_and_summary")
    assert res["bdDefRowsLen"] == 20 and res["bdDefMore"] == 5
    assert res["bdFirstRow"] == {
        "from": "p0:s0",
        "to": "p1:t0",
        "fromTitle": "s0",
        "toTitle": "t0",
        "fromProject": "p0",
        "toProject": "p1",
    }
    assert res["bd5RowsLen"] == 5 and res["bd5More"] == 20
    assert res["bdNull"] == {"rows": [], "more": 0}
    cs = res["clusterSummary"]
    assert (
        cs["visible"]["projects"] == 2
        and cs["visible"]["collapsedPhases"] == 1
        and cs["visible"]["expandedPhases"] == 1
        and cs["visible"]["tasks"] == 8
    )
    projs = {p["project"]: p for p in cs["projects"]}
    assert (
        projs["p0"]["total"] == 0
        and projs["p0"]["percent"] == 0
        and projs["p0"]["collapsed"] is True
    )
    assert (
        projs["p1"]["total"] == 16
        and projs["p1"]["done"] == 6
        and projs["p1"]["percent"] == 38
    )
    phases = {f["phase"]: f for f in cs["phases"]}
    assert (
        phases["ph0"]["id"] == "phc:p1|ph0"
        and phases["ph0"]["percent"] == 13
        and phases["ph0"]["ready"] == 4
        and phases["ph0"]["blocked"] == 3
    )
    assert (
        phases["ph1"]["id"] == "ph:p1|ph1"
        and phases["ph1"]["percent"] == 63
        and phases["ph1"]["ready"] == 1
        and phases["ph1"]["blocked"] == 2
    )


@need_node
def test_s8_g1fix_internal_edges(tmp_path):
    res = run_node(tmp_path, "s8_g1fix_internal_edges")
    # (a) 50 surviving internal edges out of 62:
    # 45 critical (sorted by id) + 5 count=2 (sorted by id) = 50 edges
    assert res["aEdgeCount"] == 50
    # Expected surviving ids: 45 critical ones first (sc0..sc44), then first 5 of count=2 (sk0..sk4)
    # Note in JS string sort: 'm:t:p0:sc0->t:p0:tc0', 'm:t:p0:sc1...', etc.
    crit_ids = sorted(f"m:t:p0:sc{i}->t:p0:tc{i}" for i in range(45))
    k2_ids = sorted(f"m:t:p0:sk{i}->t:p0:tk{i}" for i in range(10))
    expected_surviving = crit_ids + k2_ids[:5]
    assert res["aEdgeIds"] == expected_surviving
    # Surplus pair count: 5 remaining count=2 edges (5*2=10) + 7 count=1 edges (7*1=7) = 17
    assert res["aIntraHidden"] == 17

    # (b) Cross-phase in same project is not internal
    assert res["bTaskEdgesCount"] == 0
    assert res["bEdges"] == ["m:t:p0:bp1->phc:p0|ph1"]

    # (c) Done-done internal pairs skipped without showAllEdges
    assert res["cNoShowEdgesCount"] == 0 and res["cNoShowTotal"] == 0
    assert res["cShowEdgesCount"] == 1 and res["cShowTotal"] == 1

    # (d) opts.limit smaller than internal edges: cut to limit, cut count goes to hidden
    assert res["dEdgesCount"] == 1
    # 2 internal edges: d1->d2 (count 1) and d3->d4 (count 2). Count 2 is higher priority!
    assert res["dEdgeIds"] == ["m:t:p0:d3->t:p0:d4"]
    # The cut edge d1->d2 has count 1, so hidden = 1
    assert res["dHidden"] == 1
