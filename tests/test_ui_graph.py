"""T3a pure core — node-run tests for PlanUI graph and layout
(UI-PLAN §4, §3, §7, §11, §12, §13 checks A1, A2, A4, A5, A6, A7).
"""

from __future__ import annotations

import json
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

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

RUNNER_JS = r"""
"use strict";
var fs = require("fs"), mode = process.argv[2];
require(process.argv[3]);
var PlanUI = require(process.argv[4]);
require(process.argv[5]); require(process.argv[6]);
require(process.argv[7]); require(process.argv[8]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }

var fixturePath = process.argv[9];
var snap = fixturePath ? JSON.parse(fs.readFileSync(fixturePath, "utf8")) : null;
var vm = snap ? PlanUI.buildViewModel(snap) : null;

if (mode === "base_elements") {
  var els = PlanUI.buildElements(vm, PlanUI.defaultUI());
  emit({
    nodes: els.nodes.map(function (n) {
      return { id: n.data.id, parent: n.data.parent || null, label: n.data.label, classes: n.classes,
               width: n.data.width, height: n.data.height, project: n.data.project, phase: n.data.phase };
    }),
    edges: els.edges.map(function (e) {
      return { id: e.data.id, source: e.data.source, target: e.data.target, count: e.data.count,
               label: e.data.label, width: e.data.width, critical: e.data.critical, classes: e.classes };
    })
  });
} else if (mode === "collapsed_phase") {
  var ui = PlanUI.defaultUI();
  ui.collapsedPhases.add(process.argv[10] || "atlas|P1 Core build");
  var els = PlanUI.buildElements(vm, ui);
  emit({
    nodes: els.nodes.map(function (n) { return { id: n.data.id, parent: n.data.parent || null, width: n.data.width, height: n.data.height }; }),
    edges: els.edges.map(function (e) {
      return { id: e.data.id, source: e.data.source, target: e.data.target, count: e.data.count, label: e.data.label, width: e.data.width };
    })
  });
} else if (mode === "collapsed_project") {
  var ui = PlanUI.defaultUI();
  ui.collapsedProjects.add("borealis");
  var els = PlanUI.buildElements(vm, ui);
  emit({ nodes: els.nodes.map(function (n) { return { id: n.data.id, width: n.data.width, height: n.data.height }; }) });
} else if (mode === "query_filtering") {
  var q = process.argv[10];
  var elsDim = PlanUI.buildElements(vm, PlanUI.defaultUI({ query: q, matchMode: "dim", revealMatches: true }));
  var elsHide = PlanUI.buildElements(vm, PlanUI.defaultUI({ query: q, matchMode: "hide", revealMatches: true }));
  emit({
    countsDim: PlanUI.matchCounts(elsDim), countsHide: PlanUI.matchCounts(elsHide),
    dimB62Class: (elsDim.nodes.find(function (n) { return n.data.id === "t:borealis:B-62"; }) || {}).classes,
    hideNodeIds: elsHide.nodes.map(function (n) { return n.data.id; })
  });
} else if (mode === "initial_collapsed") {
  emit(Array.from(PlanUI.initialCollapsed(vm)));
} else if (mode === "jump_fit") {
  var ui = PlanUI.defaultUI();
  ui.collapsedPhases.add("atlas|P0 Foundations");
  ui.collapsedProjects.add("atlas");
  var patchPhase = PlanUI.revealPatch(vm, { collapsedPhases: new Set(["atlas|P0 Foundations"]) }, "atlas:T-1");
  var patchBoth = PlanUI.revealPatch(vm, ui, "atlas:T-1");
  var patchUnknown = PlanUI.revealPatch(vm, ui, "unknown:999");
  var layout = {
    "t:atlas:T-1": { x: 50, y: 100, width: 240, height: 64 },
    "phc:atlas|P0 Foundations": { x: 50, y: 100, width: 300, height: 72 }
  };
  var elsExp = PlanUI.buildElements(vm, PlanUI.defaultUI());
  var elsCol = PlanUI.buildElements(vm, PlanUI.defaultUI({ collapsedPhases: new Set(["atlas|P0 Foundations"]) }));
  emit({
    patchPhasePhases: Array.from(patchPhase.collapsedPhases || []),
    patchBothPhases: Array.from(patchBoth.collapsedPhases || []),
    patchBothProjects: Array.from(patchBoth.collapsedProjects || []),
    patchUnknown: patchUnknown,
    fitExp: PlanUI.fitTarget(elsExp, layout, "atlas:T-1"),
    fitCol: PlanUI.fitTarget(elsCol, layout, "atlas:T-1"),
    fitUnknown: PlanUI.fitTarget(elsExp, layout, "unknown:999")
  });
} else if (mode === "elk_layout") {
  var els = PlanUI.buildElements(vm, PlanUI.defaultUI());
  var elkGraph = PlanUI.buildElkGraph(els, "atlas");
  var fakeTree = {
    id: "p:atlas", x: 10, y: 20, width: 600, height: 400,
    children: [{
      id: "ph:atlas|P0 Foundations", x: 12, y: 44, width: 300, height: 200,
      children: [{ id: "t:atlas:T-1", x: 16, y: 44, width: 240, height: 64 }]
    }]
  };
  var acc = PlanUI.accumulate(fakeTree);
  var stacked = PlanUI.stackProjects([
    { "p:atlas": { x: 0, y: 0, width: 600, height: 200 } },
    { "p:borealis": { x: 0, y: 0, width: 600, height: 300 } }
  ], 80);

  var callCount = 0;
  PlanUI.LayoutService.setEngine({
    layout: function (g) {
      callCount++;
      return Promise.resolve({
        id: g.id, x: 0, y: 0, width: 500, height: 300,
        children: (g.children || []).map(function (c, i) {
          return { id: c.id, x: 10, y: i * 40, width: c.width || 300, height: c.height || 72 };
        })
      });
    }
  });
  PlanUI.LayoutService.clearCache();
  PlanUI.LayoutService.layoutProject("atlas", els).then(function () {
    var c1 = callCount;
    return PlanUI.LayoutService.layoutProject("atlas", els).then(function () {
      var c2 = callCount;
      PlanUI.LayoutService.setEngine({
        layout: function (g) {
          return new Promise(function (resolve) {
            setTimeout(function () { resolve({ id: g.id, x: 0, y: 0, width: 100, height: 100 }); }, 50);
          });
        }
      });
      PlanUI.LayoutService.clearCache();
      var pending = PlanUI.LayoutService.layoutProject("borealis", els);
      PlanUI.LayoutService.bumpEpoch();
      return pending.then(function (droppedRes) {
        emit({
          rootOptions: elkGraph.layoutOptions, phaseOptions: elkGraph.children[0].layoutOptions,
          childId: elkGraph.children[0].children[0].id,
          hasWidthHeight: !!(elkGraph.children[0].children[0].width && elkGraph.children[0].children[0].height),
          accAtlas: acc["p:atlas"], accPhase: acc["ph:atlas|P0 Foundations"], accTask: acc["t:atlas:T-1"],
          stackedAtlasY: stacked["p:atlas"].y, stackedBorealisY: stacked["p:borealis"].y,
          callCount1: c1, callCount2: c2, droppedRes: droppedRes
        });
      });
    });
  });
} else if (mode === "perf") {
  var items = snap.items.slice();
  while (items.length < 400) {
    var idx = items.length, clone = Object.assign({}, snap.items[idx % snap.items.length]);
    clone.id = "T-PERF" + idx; clone.key = clone.project + ":" + clone.id; clone.depends_on = [];
    items.push(clone);
  }
  var bigVm = PlanUI.buildViewModel(Object.assign({}, snap, { items: items }));
  var t0 = Date.now();
  for (var i = 0; i < 5; i++) PlanUI.buildElements(bigVm, PlanUI.defaultUI());
  emit({ count: items.length, avgMs: (Date.now() - t0) / 5 });
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
        str(WEB / "page_layout.js"),
        str(FIXTURE),
    ] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_a1_and_project_phase_grouping_and_target_sizes(tmp_path):
    res = run_node(tmp_path, "base_elements")
    nodes = res["nodes"]
    by_id = {n["id"]: n for n in nodes}

    compounds = [n for n in nodes if "compound" in (n.get("classes") or "")]
    assert len(compounds) >= 8  # A1: >= 8 compounds (4 projects + >= 4 phases)
    assert len(compounds) == 24  # 4 projects + 20 phases

    for proj in ["atlas", "borealis", "cinder", "dune"]:
        assert f"p:{proj}" in by_id
        phases = [n for n in nodes if n.get("parent") == f"p:{proj}"]
        assert len(phases) == 5  # Grouping requirement: collapsible per phase & project

    task_nodes = [n for n in nodes if n["id"].startswith("t:")]
    assert len(task_nodes) == 254
    for t in task_nodes:
        assert t["width"] == 240 and t["height"] == 64
        parent = by_id.get(t.get("parent"))
        assert parent is not None and parent["id"].startswith("ph:")
        grandparent = by_id.get(parent.get("parent"))
        assert grandparent is not None and grandparent["id"].startswith("p:")  # A1 ancestor


@need_node
def test_a2_collapsed_phase_and_edge_aggregation(tmp_path):
    res = run_node(tmp_path, "collapsed_phase", "atlas|P1 Core build")
    nodes, edges = res["nodes"], res["edges"]

    assert any(n["id"] == "phc:atlas|P1 Core build" for n in nodes)
    assert not any(n["id"] == "ph:atlas|P1 Core build" for n in nodes)
    assert not any(n.get("parent") == "ph:atlas|P1 Core build" for n in nodes)

    phc_node = next(n for n in nodes if n["id"] == "phc:atlas|P1 Core build")
    assert phc_node["width"] == 300 and phc_node["height"] == 72

    agg_edge = next((e for e in edges if e["source"] == "phc:atlas|P1 Core build" and e["count"] > 1), None)
    assert agg_edge is not None
    assert agg_edge["label"] == f"×{agg_edge['count']}"
    assert agg_edge["width"] > 2


@need_node
def test_collapsed_project_produces_single_leaf(tmp_path):
    res = run_node(tmp_path, "collapsed_project")
    nodes = res["nodes"]
    assert any(n["id"] == "pc:borealis" for n in nodes)
    assert not any(n["id"] == "p:borealis" for n in nodes)
    assert not any(n["id"].startswith("ph:borealis|") for n in nodes)
    pc = next(n for n in nodes if n["id"] == "pc:borealis")
    assert pc["width"] == 300 and pc["height"] == 72


@need_node
def test_a5_labels_equal_titles_and_never_bare_ids(tmp_path):
    res = run_node(tmp_path, "base_elements")
    task_nodes = [n for n in res["nodes"] if n["id"].startswith("t:")]
    assert len(task_nodes) > 0
    bare_id_re = re.compile(r"^[A-Z]-\d+$")
    for t in task_nodes:
        label = t["label"]
        assert not bare_id_re.match(label), f"Label must not match /^[A-Z]-\\d+$/: {label}"
        assert len(label) >= 4


@need_node
def test_cross_project_ghost_stubs_and_no_crossing_edges(tmp_path):
    res = run_node(tmp_path, "base_elements")
    nodes, edges = res["nodes"], res["edges"]
    by_id = {n["id"]: n for n in nodes}

    ghosts = [n for n in nodes if n["id"].startswith("g:")]
    assert len(ghosts) == 12
    for g in ghosts:
        assert g["width"] == 200 and g["height"] == 40
        assert g["label"].startswith("↗ ")

    for e in edges:
        s_node, t_node = by_id.get(e["source"]), by_id.get(e["target"])
        assert s_node and t_node
        assert s_node["project"] == t_node["project"]  # no edge crosses project box


@need_node
def test_a6_loop_nodes_and_exit_diamonds(tmp_path):
    res = run_node(tmp_path, "base_elements")
    nodes, edges = res["nodes"], res["edges"]

    loop_task = next(n for n in nodes if n["id"] == "t:atlas:T-8")
    assert "loop-node" in loop_task["classes"]
    assert "↺" in loop_task["label"]

    exit_node = next(n for n in nodes if n["id"] == "x:atlas:T-8")
    assert exit_node["label"] == "exit: D-107"
    assert "filled" in exit_node["classes"]
    assert exit_node["width"] == 100 and exit_node["height"] == 40

    exit_absent = next(n for n in nodes if n["id"] == "x:atlas:T-31")
    assert exit_absent["label"] == "exit: D-130"
    assert "hollow" in exit_absent["classes"]

    assert any(e["source"] == "t:atlas:T-8" and e["target"] == "x:atlas:T-8" for e in edges)


@need_node
def test_a7_search_filter_dim_hide_and_match_counts(tmp_path):
    res = run_node(tmp_path, "query_filtering", "state:ready owner:Atlas")
    counts_dim, counts_hide = res["countsDim"], res["countsHide"]

    assert counts_dim["nodes"] == 1
    assert counts_dim["edges"] > 0
    assert "dim" not in res["dimB62Class"]

    assert counts_hide["nodes"] == 1
    assert "t:borealis:B-62" in res["hideNodeIds"]
    assert "t:atlas:T-1" not in res["hideNodeIds"]


@need_node
def test_initial_collapsed_rule(tmp_path):
    col = run_node(tmp_path, "initial_collapsed")
    assert "atlas|P0 Foundations" in col
    assert "cinder|P4 Launch" in col
    assert "atlas|P1 Core build" not in col


@need_node
def test_reveal_patch_and_fit_target(tmp_path):
    res = run_node(tmp_path, "jump_fit")
    assert "atlas|P0 Foundations" not in res["patchPhasePhases"]
    assert "atlas|P0 Foundations" not in res["patchBothPhases"]
    assert "atlas" not in res["patchBothProjects"]
    assert res["patchUnknown"] is None

    assert res["fitExp"] == {"x": 50, "y": 100, "w": 240, "h": 64}
    assert res["fitCol"] == {"x": 50, "y": 100, "w": 300, "h": 72}
    assert res["fitUnknown"] is None


@need_node
def test_build_elk_graph_and_layout_service(tmp_path):
    res = run_node(tmp_path, "elk_layout")
    ro = res["rootOptions"]
    assert ro["elk.algorithm"] == "layered" and ro["elk.direction"] == "RIGHT"
    assert ro["elk.hierarchyHandling"] == "INCLUDE_CHILDREN" and ro["elk.spacing.nodeNode"] == "28"
    assert ro["elk.layered.spacing.nodeNodeBetweenLayers"] == "72"
    assert ro["elk.layered.spacing.edgeNodeBetweenLayers"] == "20"
    assert ro["elk.layered.considerModelOrder.strategy"] == "NODES_AND_EDGES"
    assert ro["elk.layered.nodePlacement.strategy"] == "NETWORK_SIMPLEX"
    assert ro["elk.padding"] == "[top=12,left=12,bottom=12,right=12]"

    po = res["phaseOptions"]
    assert po["elk.padding"] == "[top=44,left=16,bottom=16,right=16]"
    assert res["hasWidthHeight"] is True

    # absolute accumulation
    assert res["accAtlas"] == {"x": 10, "y": 20, "width": 600, "height": 400, "w": 600, "h": 400}
    assert res["accPhase"] == {"x": 22, "y": 64, "width": 300, "height": 200, "w": 300, "h": 200}
    assert res["accTask"] == {"x": 38, "y": 108, "width": 240, "height": 64, "w": 240, "h": 64}

    # 80 px stacking
    assert res["stackedAtlasY"] == 0
    assert res["stackedBorealisY"] == 280

    # signature cache hit & stale-epoch drop
    assert res["callCount1"] == 1 and res["callCount2"] == 1
    assert res["droppedRes"] is None


@need_node
def test_build_elements_performance_400_items(tmp_path):
    res = run_node(tmp_path, "perf")
    assert res["count"] >= 400
    assert res["avgMs"] < 250
