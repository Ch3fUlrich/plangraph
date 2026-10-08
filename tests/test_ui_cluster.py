"""Node-run tests for PlanUI graph cluster model (UI-PLAN GU-G1, T1-T8)."""

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

if (mode === "constants") {
  emit({ EDGE_BUDGET: PlanUI.EDGE_BUDGET });
} else if (mode === "t1_overview_state") {
  var before = JSON.stringify(vm);
  var defState = PlanUI.overviewState(vm);
  var afterDef = JSON.stringify(vm);
  var projState = PlanUI.overviewState(vm, { level: "project" });
  var firstPhase = Array.from(projState.collapsedPhases)[0];
  var expOne = PlanUI.overviewState(vm, { level: "project", expandPhases: [firstPhase] });
  var ignState = PlanUI.overviewState(vm, { expandProjects: ["unknown_p"], expandPhases: ["unknown_ph"] });
  var emptyState = PlanUI.overviewState(null);
  emit({
    notMutated: (before === afterDef),
    defProjs: Array.from(defState.collapsedProjects), defPhases: Array.from(defState.collapsedPhases),
    projProjs: Array.from(projState.collapsedProjects), projPhases: Array.from(projState.collapsedPhases),
    firstPhase: firstPhase, expOneProjs: Array.from(expOne.collapsedProjects), expOnePhases: Array.from(expOne.collapsedPhases),
    ignProjs: Array.from(ignState.collapsedProjects), ignPhases: Array.from(ignState.collapsedPhases),
    emptyProjs: Array.from(emptyState.collapsedProjects), emptyPhases: Array.from(emptyState.collapsedPhases)
  });
} else if (mode === "t2_toggle_expand") {
  var s0 = PlanUI.overviewState(vm);
  var origCopy = { collapsedProjects: new Set(s0.collapsedProjects), collapsedPhases: new Set(s0.collapsedPhases) };
  var tProj1 = PlanUI.toggleCluster(s0, "project", "atlas");
  var tProj2 = PlanUI.toggleCluster(tProj1, "project", "atlas");
  var projKeepsPhases = Array.from(tProj1.collapsedPhases).sort().join(",") === Array.from(s0.collapsedPhases).sort().join(",");
  var projTwiceEqual = Array.from(tProj2.collapsedProjects).sort().join(",") === Array.from(s0.collapsedProjects).sort().join(",");

  var sProj = PlanUI.overviewState(vm, { level: "project" });
  var phId = "atlas|P0 Foundations";
  var tPh1 = PlanUI.toggleCluster(sProj, "phase", phId);
  var phaseOpensProj = !tPh1.collapsedProjects.has("atlas") && !tPh1.collapsedPhases.has(phId);
  var s0PhToggle2 = PlanUI.toggleCluster(PlanUI.toggleCluster(s0, "phase", phId), "phase", phId);
  var phaseTwiceEqual = Array.from(s0PhToggle2.collapsedPhases).sort().join(",") === Array.from(s0.collapsedPhases).sort().join(",");

  var unk = PlanUI.toggleCluster(s0, "unknown_kind", "foo");
  var origUnchanged = (s0.collapsedProjects.size === origCopy.collapsedProjects.size) &&
    (s0.collapsedPhases.size === origCopy.collapsedPhases.size);

  var sAllProj = PlanUI.overviewState(vm, { level: "project" });
  var expAll = PlanUI.expandAllIn(sAllProj, vm, "atlas");
  var atlasOpen = !expAll.collapsedProjects.has("atlas");
  var atlasPhasesOpen = Array.from(expAll.collapsedPhases).every(function (p) { return p.indexOf("atlas|") !== 0; });

  emit({
    projKeepsPhases: projKeepsPhases, projTwiceEqual: projTwiceEqual,
    phaseOpensProj: phaseOpensProj, phaseTwiceEqual: phaseTwiceEqual,
    origUnchanged: origUnchanged, atlasOpen: atlasOpen, atlasPhasesOpen: atlasPhasesOpen
  });
} else if (mode === "t3_rep_of") {
  emit({
    repCollapsedPh: PlanUI.repOf(vm, PlanUI.overviewState(vm), "atlas:T-1"),
    repCollapsedPr: PlanUI.repOf(vm, PlanUI.overviewState(vm, { level: "project" }), "atlas:T-1"),
    repOpen: PlanUI.repOf(vm, PlanUI.overviewState(vm, { expandPhases: ["atlas|P0 Foundations"] }), "atlas:T-1"),
    repUnknown: PlanUI.repOf(vm, PlanUI.overviewState(vm), "nonexistent:key")
  });
} else if (mode === "t4_meta_edges_overview") {
  var s = PlanUI.overviewState(vm), res = PlanUI.metaEdges(vm, s);
  emit({
    limit: res.limit, total: res.total, shown: res.shown,
    intraHidden: res.intraHidden, aggregated: res.aggregated, hidden: res.hidden,
    edges: res.edges.map(function (e) {
      return { id: e.id, source: e.source, target: e.target, count: e.count, level: e.level, crossProject: e.crossProject };
    })
  });
} else if (mode === "t5_one_expanded_phase") {
  var atlasProj = vm.projects.find(function (p) { return p.name === "atlas"; });
  var firstPhaseId = "atlas|" + atlasProj.phases[0].name;
  var s = PlanUI.overviewState(vm, { expandPhases: [firstPhaseId] });
  var b = PlanUI.edgeBudget(vm, s), res = PlanUI.metaEdges(vm, s);
  var taskEdges = res.edges.filter(function (e) { return e.level === "task"; });
  var firstPhaseKeys = new Set(atlasProj.phases[0].keys), internalDepCount = 0;
  atlasProj.phases[0].keys.forEach(function (k) {
    var it = vm.items.get(k);
    (it.deps || []).forEach(function (dk) {
      if (firstPhaseKeys.has(dk)) {
        var dep = vm.items.get(dk);
        if (!(dep.state === "done" && it.state === "done")) internalDepCount++;
      }
    });
  });
  emit({
    firstPhaseId: firstPhaseId, edgeBudget: b, limit: res.limit, total: res.total,
    shown: res.shown, intraHidden: res.intraHidden, aggregated: res.aggregated,
    hidden: res.hidden, edgesLength: res.edges.length,
    taskEdgesCount: taskEdges.length, internalDepCount: internalDepCount,
    taskEdges: taskEdges.map(function (e) { return { id: e.id, source: e.source, target: e.target }; })
  });
} else if (mode === "t6_large_synthetic") {
  var seed = 42;
  function rand() { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }
  var projects = ["p0", "p1", "p2", "p3", "p4", "p5"], phases = ["ph0", "ph1", "ph2", "ph3", "ph4", "ph5"];
  var items = [], allKeys = [], states = ["done", "ready", "blocked"], taskNum = 1;
  projects.forEach(function (p) {
    phases.forEach(function (ph) {
      for (var t = 0; t < 12; t++) {
        var key = p + ":T-" + taskNum;
        taskNum++; allKeys.push(key);
        var deps = [], numDeps = 1 + Math.floor(rand() * 4);
        for (var d = 0; d < numDeps; d++) {
          if (allKeys.length > 1) {
            var targetKey = allKeys[Math.floor(rand() * (allKeys.length - 1))];
            if (deps.indexOf(targetKey) < 0) deps.push(targetKey);
          }
        }
        items.push({
          project: p, id: "T-" + items.length, key: key, title: "Task " + key,
          kind: "lane", track: ph, phase: ph, owner: "Owner " + p,
          state: states[Math.floor(rand() * states.length)], estimate_h: 4,
          depends_on: deps, operator: false, priority: Math.floor(rand() * 10),
          epoch: 1, evidence: []
        });
      }
    });
  });
  var largeSnap = { generated: "2026-10-01T00:00:00Z", head: "synth432", items: items };
  var largeVm = PlanUI.buildViewModel(largeSnap);
  var emptyState = { collapsedProjects: new Set(), collapsedPhases: new Set() };
  var b = PlanUI.edgeBudget(largeVm, emptyState);
  var rawRes = PlanUI.metaEdges(largeVm, emptyState, { limit: 100000 });
  var res = PlanUI.metaEdges(largeVm, emptyState);
  var resCall2 = PlanUI.metaEdges(largeVm, emptyState);
  var res5 = PlanUI.metaEdges(largeVm, emptyState, { limit: 5 });
  var res1 = PlanUI.metaEdges(largeVm, emptyState, { limit: 1 });
  function stripEdges(eList) {
    return eList.map(function (e) {
      return { id: e.id, source: e.source, target: e.target, count: e.count, exact: e.exact, critical: e.critical, crossProject: e.crossProject };
    });
  }
  emit({
    itemsCount: items.length, budget: b, rawEdgesCount: rawRes.edges.length,
    edgesLength: res.edges.length, total: res.total, shown: res.shown,
    intraHidden: res.intraHidden, aggregated: res.aggregated, hidden: res.hidden,
    edgeIds: res.edges.map(function (e) { return e.id; }),
    selfEdges: res.edges.filter(function (e) { return e.source === e.target; }).length,
    deterministic: JSON.stringify(stripEdges(res.edges)) === JSON.stringify(stripEdges(resCall2.edges)),
    firstEdgeCritical: res.edges.length > 0 ? res.edges[0].critical : false,
    res5Length: res5.edges.length, res5Total: res5.total, res5Shown: res5.shown, res5Intra: res5.intraHidden, res5Hidden: res5.hidden,
    res1Length: res1.edges.length, res1Total: res1.total, res1Shown: res1.shown, res1Intra: res1.intraHidden, res1Hidden: res1.hidden
  });
} else if (mode === "t7_breakdown_summary") {
  var s = PlanUI.overviewState(vm), meta = PlanUI.metaEdges(vm, s), edge = meta.edges[0];
  var bd5 = PlanUI.edgeBreakdown(vm, edge, 5), bdEmpty = PlanUI.edgeBreakdown(vm, null);
  var sumOv = PlanUI.clusterSummary(vm, s);
  var sumPr = PlanUI.clusterSummary(vm, PlanUI.overviewState(vm, { level: "project" }));
  var sumZero = PlanUI.clusterSummary({ projects: [{ name: "zero", label: "Zero", color: "#000", phases: [{ name: "p0", total: 0, done: 0 }] }] }, { collapsedProjects: new Set(), collapsedPhases: new Set() });
  var expSummary = PlanUI.clusterSummary(vm, PlanUI.overviewState(vm, { expandPhases: ["atlas|P0 Foundations"] }));
  emit({
    bd5Rows: bd5.rows, bd5More: bd5.more, pairsLength: edge.pairs.length, bdEmpty: bdEmpty,
    sumOvVisible: sumOv.visible, sumOvProjsLen: sumOv.projects.length, sumOvPhasesLen: sumOv.phases.length,
    sumPrVisible: sumPr.visible, sumPrPhasesLen: sumPr.phases.length,
    sumZeroProjPct: sumZero.projects[0].percent, sumZeroPhasePct: sumZero.phases[0].percent,
    expTasks: expSummary.visible.tasks
  });
}
"""


def run_node(tmp_path: Path, mode: str, *args) -> dict:
    runner_path = tmp_path / "runner.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    cmd = [
        "node", str(runner_path), mode,
        str(WEB / "page.js"), str(WEB / "page_model.js"), str(WEB / "page_store.js"),
        str(WEB / "page_search.js"), str(WEB / "page_graph.js"), str(WEB / "page_graph_cluster.js"),
        str(FIXTURE),
    ] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_t1_overview_state(tmp_path):
    res = run_node(tmp_path, "t1_overview_state")
    with open(FIXTURE, encoding="utf-8") as f:
        snap = json.load(f)
    expected_projects = sorted(set(it["project"] for it in snap["items"]))
    expected_phases = sorted(set(f"{it['project']}|{it['phase']}" for it in snap["items"]))

    assert res["notMutated"] is True
    assert res["defProjs"] == []
    assert len(res["defPhases"]) == 20
    assert sorted(res["defPhases"]) == expected_phases
    assert sorted(res["projProjs"]) == expected_projects
    assert sorted(res["projPhases"]) == expected_phases

    first_phase = res["firstPhase"]
    first_proj = first_phase.split("|")[0]
    assert first_phase not in res["expOnePhases"]
    assert first_proj not in res["expOneProjs"]
    assert res["ignProjs"] == []
    assert sorted(res["ignPhases"]) == expected_phases
    assert res["emptyProjs"] == []
    assert res["emptyPhases"] == []


@need_node
def test_t2_toggle_cluster_and_expand_all(tmp_path):
    res = run_node(tmp_path, "t2_toggle_expand")
    assert res["projKeepsPhases"] is True
    assert res["projTwiceEqual"] is True
    assert res["phaseOpensProj"] is True
    assert res["phaseTwiceEqual"] is True
    assert res["origUnchanged"] is True
    assert res["atlasOpen"] is True
    assert res["atlasPhasesOpen"] is True


@need_node
def test_t3_rep_of(tmp_path):
    res = run_node(tmp_path, "t3_rep_of")
    assert res["repCollapsedPh"] == {"id": "phc:atlas|P0 Foundations", "level": "phase"}
    assert res["repCollapsedPr"] == {"id": "pc:atlas", "level": "project"}
    assert res["repOpen"] == {"id": "t:atlas:T-1", "level": "task"}
    assert res["repUnknown"] is None


@need_node
def test_t4_meta_edges_overview(tmp_path):
    res = run_node(tmp_path, "t4_meta_edges_overview")
    with open(FIXTURE, encoding="utf-8") as f:
        snap = json.load(f)

    by_key = {it["key"]: it for it in snap["items"]}
    expected_total = 0
    for it in snap["items"]:
        for dk in it.get("depends_on", []):
            if dk in by_key:
                dep = by_key[dk]
                if not (dep["state"] == "done" and it["state"] == "done"):
                    expected_total += 1

    edges = res["edges"]
    assert len(edges) <= 40
    assert all(e["id"].startswith("m:") for e in edges)
    assert not any(e["source"].startswith("t:") or e["target"].startswith("t:") for e in edges)
    assert not any(e["source"] == e["target"] for e in edges)
    assert res["total"] == expected_total
    assert res["intraHidden"] > 0
    assert res["shown"] == sum(e["count"] for e in edges)
    assert res["shown"] + res["intraHidden"] + res["hidden"] == res["total"]
    assert any(e["crossProject"] for e in edges)


@need_node
def test_t5_one_expanded_phase(tmp_path):
    res = run_node(tmp_path, "t5_one_expanded_phase")
    assert res["edgeBudget"] == 150
    assert res["edgesLength"] <= 150
    if res["internalDepCount"] > 0:
        assert res["taskEdgesCount"] > 0
        assert any(
            e["source"].startswith("t:atlas:") and e["target"].startswith("t:atlas:")
            for e in res["taskEdges"]
        )
    else:
        assert res["taskEdgesCount"] == 0
    assert res["shown"] + res["intraHidden"] + res["hidden"] == res["total"]


@need_node
def test_t6_budget_aggregation_large(tmp_path):
    res = run_node(tmp_path, "t6_large_synthetic")
    assert res["itemsCount"] == 432
    assert res["budget"] == 400
    assert res["rawEdgesCount"] > res["budget"]
    assert res["edgesLength"] <= res["budget"]
    assert res["aggregated"] > 0
    assert res["shown"] + res["intraHidden"] + res["hidden"] == res["total"]
    assert len(res["edgeIds"]) == len(set(res["edgeIds"]))
    assert res["selfEdges"] == 0
    assert res["deterministic"] is True
    assert res["firstEdgeCritical"] is True

    assert res["res5Length"] <= 5
    assert res["res5Shown"] + res["res5Intra"] + res["res5Hidden"] == res["res5Total"]

    assert res["res1Length"] <= 1
    assert res["res1Shown"] + res["res1Intra"] + res["res1Hidden"] == res["res1Total"]


@need_node
def test_t7_edge_breakdown_and_cluster_summary(tmp_path):
    res = run_node(tmp_path, "t7_breakdown_summary")
    assert len(res["bd5Rows"]) <= 5
    assert res["bd5More"] == res["pairsLength"] - len(res["bd5Rows"])
    assert all(r["fromTitle"] and r["toTitle"] for r in res["bd5Rows"])
    assert res["bdEmpty"] == {"rows": [], "more": 0}

    assert res["sumOvVisible"]["projects"] == 4
    assert res["sumOvVisible"]["collapsedPhases"] == 20
    assert res["sumOvVisible"]["expandedPhases"] == 0
    assert res["sumOvVisible"]["tasks"] == 0

    assert res["sumPrVisible"]["projects"] == 4
    assert res["sumPrVisible"]["collapsedPhases"] == 0
    assert res["sumPrPhasesLen"] == 0

    assert res["sumZeroProjPct"] == 0
    assert res["sumZeroPhasePct"] == 0
    assert res["expTasks"] == 13


def test_t8_registration_and_hygiene(tmp_path):
    import plangraph.render as render_mod

    assert "page_graph_cluster.js" in render_mod.JS_FILES
    idx_graph = render_mod.JS_FILES.index("page_graph.js")
    idx_cluster = render_mod.JS_FILES.index("page_graph_cluster.js")
    idx_layout = render_mod.JS_FILES.index("page_layout.js")
    assert idx_cluster > idx_graph
    assert idx_layout > idx_cluster
    assert render_mod.JS_FILES.count("page_graph_cluster.js") == 1

    cluster_js = WEB / "page_graph_cluster.js"
    assert cluster_js.is_file()
    lines = cluster_js.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 400

    content = cluster_js.read_text(encoding="utf-8")
    assert re.search(r"\bdocument\b", content) is None
    assert re.search(r"\bcytoscape\b", content, re.IGNORECASE) is None
    window_matches = [m for m in re.finditer(r"window\.", content)]
    assert len(window_matches) == 0

    res = run_node(tmp_path, "constants")
    assert res["EDGE_BUDGET"] == {
        "overview": 40,
        "phase": 150,
        "perExtraPhase": 50,
        "max": 400,
    }
