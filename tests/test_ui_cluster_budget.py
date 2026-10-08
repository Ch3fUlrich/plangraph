"""Node-run tests for PlanUI cluster edge budget and internal edge preservation (GU-G1fix, T1-T7)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = ROOT / "src" / "plangraph" / "web"
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
var snap = fixturePath && fs.existsSync(fixturePath) ? JSON.parse(fs.readFileSync(fixturePath, "utf8")) : null;
var fixtureVm = snap ? PlanUI.buildViewModel(snap) : null;

function build432() {
  var seed = 42;
  function rand() { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }
  var projects = ["p0", "p1", "p2", "p3", "p4", "p5"], phases = ["ph0", "ph1", "ph2", "ph3", "ph4", "ph5"];
  var items = [], states = ["done", "ready", "blocked"], taskNum = 1, phaseKeys = {};
  projects.forEach(function (p) {
    phases.forEach(function (ph) {
      var phId = p + "|" + ph;
      phaseKeys[phId] = [];
      for (var t = 0; t < 12; t++) {
        var key = p + ":T-" + taskNum++;
        phaseKeys[phId].push(key);
        var deps = [], numDeps = 1 + Math.floor(rand() * 4);
        for (var d = 0; d < numDeps; d++) {
          var isCross = rand() < 0.4;
          if (!isCross && phaseKeys[phId].length > 1) {
            var tk = phaseKeys[phId][Math.floor(rand() * (phaseKeys[phId].length - 1))];
            if (deps.indexOf(tk) < 0) deps.push(tk);
          } else if (items.length > 0) {
            var tk2 = items[Math.floor(rand() * items.length)].key;
            if (deps.indexOf(tk2) < 0) deps.push(tk2);
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
  return { snap: { generated: "2026-10-01T00:00:00Z", head: "synth432", items: items }, phaseKeys: phaseKeys, projects: projects, phases: phases };
}

if (mode === "t1_dense_regression") {
  var b = build432(), vm = PlanUI.buildViewModel(b.snap);
  var phaseResults = [];
  b.projects.forEach(function (p) {
    b.phases.forEach(function (ph) {
      var phId = p + "|" + ph;
      var s = PlanUI.overviewState(vm, { expandPhases: [phId] });
      var res = PlanUI.metaEdges(vm, s);
      var phaseKeySet = new Set(b.phaseKeys[phId]);
      var rawInternalMap = {};
      vm.items.forEach(function (it) {
        if (!phaseKeySet.has(it.key)) return;
        (it.deps || []).forEach(function (dk) {
          if (!phaseKeySet.has(dk)) return;
          var dep = vm.items.get(dk);
          if (dep.state === "done" && it.state === "done") return;
          var edgeId = "m:t:" + dk + "->t:" + it.key;
          if (!rawInternalMap[edgeId]) {
            rawInternalMap[edgeId] = {
              id: edgeId, source: "t:" + dk, target: "t:" + it.key,
              count: 0, critical: !!(dep.critical && it.critical)
            };
          }
          rawInternalMap[edgeId].count++;
        });
      });
      var expectedInternal = Object.keys(rawInternalMap).map(function (k) { return rawInternalMap[k]; });
      expectedInternal.sort(function (e1, e2) {
        if (e1.critical !== e2.critical) return e1.critical ? -1 : 1;
        if (e1.count !== e2.count) return e2.count - e1.count;
        if (e1.id < e2.id) return -1;
        if (e1.id > e2.id) return 1;
        return 0;
      });
      expectedInternal = expectedInternal.slice(0, 50);
      var actualTaskEdges = res.edges.filter(function (e) {
        return e.source.indexOf("t:") === 0 && e.target.indexOf("t:") === 0;
      });
      var matched = (actualTaskEdges.length === expectedInternal.length) &&
        actualTaskEdges.every(function (ae, i) { return ae.id === expectedInternal[i].id; });
      var conserved = (res.shown + res.intraHidden + res.hidden === res.total);
      var withinLimit = (res.edges.length <= res.limit);
      phaseResults.push({
        phaseId: phId, expectedCount: expectedInternal.length, actualCount: actualTaskEdges.length,
        matched: matched, conserved: conserved, withinLimit: withinLimit
      });
    });
  });
  emit({ phaseResults: phaseResults });
} else if (mode === "t2_all_expanded") {
  var b2 = build432(), vm2 = PlanUI.buildViewModel(b2.snap), allPhases = [];
  b2.projects.forEach(function (p) {
    b2.phases.forEach(function (ph) { allPhases.push(p + "|" + ph); });
  });
  var sAll = PlanUI.overviewState(vm2, { expandPhases: allPhases });
  var resAll = PlanUI.metaEdges(vm2, sAll);
  emit({
    limit: resAll.limit, edgesLength: resAll.edges.length,
    withinLimit: resAll.edges.length <= 400,
    conserved: (resAll.shown + resAll.intraHidden + resAll.hidden === resAll.total),
    total: resAll.total, shown: resAll.shown, intraHidden: resAll.intraHidden, hidden: resAll.hidden
  });
} else if (mode === "t3_fixture_results") {
  var ov = PlanUI.metaEdges(fixtureVm, PlanUI.overviewState(fixtureVm));
  var allPh = [];
  fixtureVm.projects.forEach(function (p) {
    (p.phases || []).forEach(function (ph) { allPh.push(p.name + "|" + ph.name); });
  });
  var oneLengths = allPh.map(function (phId) {
    var s = PlanUI.overviewState(fixtureVm, { expandPhases: [phId] });
    return PlanUI.metaEdges(fixtureVm, s).edges.length;
  });
  var allRes = PlanUI.metaEdges(fixtureVm, PlanUI.overviewState(fixtureVm, { expandPhases: allPh }));
  var projRes = PlanUI.metaEdges(fixtureVm, PlanUI.overviewState(fixtureVm, { level: "project" }));
  emit({
    overviewCount: ov.edges.length,
    onePhaseMin: Math.min.apply(null, oneLengths),
    onePhaseMax: Math.max.apply(null, oneLengths),
    onePhaseLengths: oneLengths,
    allExpandedCount: allRes.edges.length,
    projectLevelCount: projRes.edges.length
  });
} else if (mode === "t4_cap_50") {
  var t4Items = [];
  for (var i = 1; i <= 61; i++) {
    t4Items.push({
      project: "p0", id: "T-" + i, key: "p0:T-" + i, title: "Task " + i,
      kind: "lane", track: "ph0", phase: "ph0", owner: "Owner", state: "ready", estimate_h: 4,
      depends_on: i > 1 ? ["p0:T-" + (i - 1)] : [], operator: false, priority: 5, epoch: 1, evidence: []
    });
  }
  var vmT4 = PlanUI.buildViewModel({ generated: "2026-10-01T00:00:00Z", head: "t4", items: t4Items });
  var sT4 = PlanUI.overviewState(vmT4, { expandPhases: ["p0|ph0"] });
  var resT4 = PlanUI.metaEdges(vmT4, sT4);
  var internalEdges = resT4.edges.filter(function (e) {
    return e.source.indexOf("t:p0:T-") === 0 && e.target.indexOf("t:p0:T-") === 0;
  });
  emit({
    totalEdges: resT4.edges.length, internalCount: internalEdges.length,
    total: resT4.total, shown: resT4.shown, intraHidden: resT4.intraHidden, hidden: resT4.hidden,
    conserved: (resT4.shown + resT4.intraHidden + resT4.hidden === resT4.total)
  });
} else if (mode === "t5_priority") {
  var t5Items = [
    { project: "p0", id: "T-1", key: "p0:T-1", title: "T1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 4, depends_on: [], operator: false, priority: 5, epoch: 1, evidence: [] },
    { project: "p0", id: "T-2", key: "p0:T-2", title: "T2", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 4, depends_on: ["p0:T-1"], operator: false, priority: 5, epoch: 1, evidence: [] },
    { project: "p0", id: "T-3", key: "p0:T-3", title: "T3", kind: "lane", track: "ph1", phase: "ph1", owner: "O", state: "ready", estimate_h: 4, depends_on: ["p0:T-1", "p1:T-4"], operator: false, priority: 5, epoch: 1, evidence: [] },
    { project: "p1", id: "T-4", key: "p1:T-4", title: "T4", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 4, depends_on: [], operator: false, priority: 5, epoch: 1, evidence: [] }
  ];
  var vmT5 = PlanUI.buildViewModel({ generated: "2026-10-01T00:00:00Z", head: "t5", items: t5Items });
  var sT5 = PlanUI.overviewState(vmT5, { expandPhases: ["p0|ph0"] });
  var resT5 = PlanUI.metaEdges(vmT5, sT5);
  emit({
    edges: resT5.edges.map(function (e) {
      return { id: e.id, source: e.source, target: e.target, level: e.level };
    })
  });
} else if (mode === "t6_purity_determinism") {
  function deepFreeze(obj) {
    if (!obj || typeof obj !== "object") return obj;
    Object.freeze(obj);
    Object.getOwnPropertyNames(obj).forEach(function (p) {
      if (obj[p] && (typeof obj[p] === "object" || typeof obj[p] === "function") && !Object.isFrozen(obj[p])) {
        deepFreeze(obj[p]);
      }
    });
    return obj;
  }
  var t6Items = [
    { project: "p0", id: "T-1", key: "p0:T-1", title: "T1", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 4, depends_on: [], operator: false, priority: 5, epoch: 1, evidence: [] },
    { project: "p0", id: "T-2", key: "p0:T-2", title: "T2", kind: "lane", track: "ph0", phase: "ph0", owner: "O", state: "ready", estimate_h: 4, depends_on: ["p0:T-1"], operator: false, priority: 5, epoch: 1, evidence: [] }
  ];
  var vmT6 = PlanUI.buildViewModel({ generated: "2026-10-01T00:00:00Z", head: "t6", items: t6Items });
  var sT6 = PlanUI.overviewState(vmT6, { expandPhases: ["p0|ph0"] });
  deepFreeze(vmT6);
  deepFreeze(sT6);
  var res1 = PlanUI.metaEdges(vmT6, sT6);
  var res2 = PlanUI.metaEdges(vmT6, sT6);
  emit({ identical: JSON.stringify(res1) === JSON.stringify(res2) });
} else if (mode === "t7_opts_limit") {
  var t7Items = [];
  for (var j = 1; j <= 25; j++) {
    t7Items.push({
      project: "p0", id: "T-" + j, key: "p0:T-" + j, title: "Task " + j,
      kind: "lane", track: "ph0", phase: "ph0", owner: "Owner", state: "ready", estimate_h: 4,
      depends_on: j > 1 ? ["p0:T-" + (j - 1)] : [], operator: false, priority: 5, epoch: 1, evidence: []
    });
  }
  var vmT7 = PlanUI.buildViewModel({ generated: "2026-10-01T00:00:00Z", head: "t7", items: t7Items });
  var sT7 = PlanUI.overviewState(vmT7, { expandPhases: ["p0|ph0"] });
  var res5 = PlanUI.metaEdges(vmT7, sT7, { limit: 5 });
  var res1 = PlanUI.metaEdges(vmT7, sT7, { limit: 1 });
  emit({
    res5Length: res5.edges.length, res5Within: res5.edges.length <= 5,
    res5Conserved: (res5.shown + res5.intraHidden + res5.hidden === res5.total),
    res1Length: res1.edges.length, res1Within: res1.edges.length <= 1,
    res1Conserved: (res1.shown + res1.intraHidden + res1.hidden === res1.total)
  });
}
"""


def run_node(tmp_path: Path, mode: str, *args) -> dict:
    runner_path = tmp_path / "runner_budget.js"
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
def test_t1_dense_vm_regression(tmp_path: Path):
    res = run_node(tmp_path, "t1_dense_regression")
    phase_results = res["phaseResults"]
    assert len(phase_results) == 36
    for pr in phase_results:
        assert pr["matched"] is True, f"Phase {pr['phaseId']} internal edges did not match independent calculation"
        assert pr["withinLimit"] is True, f"Phase {pr['phaseId']} exceeded edge limit"
        assert pr["conserved"] is True, f"Phase {pr['phaseId']} conservation broken"


@need_node
def test_t2_all_phases_expanded_budget(tmp_path: Path):
    res = run_node(tmp_path, "t2_all_expanded")
    assert res["withinLimit"] is True
    assert res["edgesLength"] <= 400
    assert res["conserved"] is True
    assert res["shown"] + res["intraHidden"] + res["hidden"] == res["total"]


@need_node
def test_t3_fixture_results_unchanged(tmp_path: Path):
    res = run_node(tmp_path, "t3_fixture_results")
    assert res["overviewCount"] == 22
    assert res["onePhaseMin"] >= 22
    assert res["onePhaseMax"] <= 47
    assert len(res["onePhaseLengths"]) == 20
    assert res["allExpandedCount"] == 273
    assert res["projectLevelCount"] == 9


@need_node
def test_t4_internal_edges_capped_at_50(tmp_path: Path):
    res = run_node(tmp_path, "t4_cap_50")
    assert res["total"] == 60
    assert res["internalCount"] == 50
    assert res["totalEdges"] == 50
    assert res["intraHidden"] == 10
    assert res["conserved"] is True


@need_node
def test_t5_priority_task_ends_and_internal_first(tmp_path: Path):
    res = run_node(tmp_path, "t5_priority")
    edges = res["edges"]
    assert len(edges) == 3
    # 1st: internal edge (both ends are task reps in expanded phase)
    assert edges[0]["source"].startswith("t:") and edges[0]["target"].startswith("t:")
    # 2nd: task-ended edge (touches expanded phase)
    assert edges[1]["source"].startswith("t:") and edges[1]["target"].startswith("phc:")
    # 3rd: cluster-to-cluster edge (no task-level ends)
    assert edges[2]["source"].startswith("phc:") and edges[2]["target"].startswith("phc:")


@need_node
def test_t6_purity_and_determinism(tmp_path: Path):
    res = run_node(tmp_path, "t6_purity_determinism")
    assert res["identical"] is True


@need_node
def test_t7_opts_limit_smaller_than_internal(tmp_path: Path):
    res = run_node(tmp_path, "t7_opts_limit")
    assert res["res5Length"] <= 5
    assert res["res5Conserved"] is True
    assert res["res1Length"] <= 1
    assert res["res1Conserved"] is True
