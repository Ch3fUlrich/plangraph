"""Node-run tests for PlanUI edge budget applied to built elements (GU-G4a, T1-T7)."""

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
require(process.argv[9]);
function emit(o) { process.stdout.write(JSON.stringify(o)); }
var fixturePath = process.argv[10];
var snap = fixturePath && fs.existsSync(fixturePath) ? JSON.parse(fs.readFileSync(fixturePath, "utf8")) : null;
var fixtureVm = snap ? PlanUI.buildViewModel(snap) : null;

function isStruct(e) {
  var s = (e.data && e.data.source) || e.source || "", t = (e.data && e.data.target) || e.target || "";
  return s.indexOf("g:") === 0 || t.indexOf("x:") === 0;
}
function build432() {
  var seed = 42;
  function rand() { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }
  var projects = ["p0","p1","p2","p3","p4","p5"], phases = ["ph0","ph1","ph2","ph3","ph4","ph5"];
  var items = [], states = ["done", "ready", "blocked"], taskNum = 1, phaseKeys = {};
  projects.forEach(function (p) {
    phases.forEach(function (ph) {
      var phId = p + "|" + ph; phaseKeys[phId] = [];
      for (var t = 0; t < 12; t++) {
        var key = p + ":T-" + taskNum++; phaseKeys[phId].push(key);
        var deps = [], numDeps = 1 + Math.floor(rand() * 4);
        for (var d = 0; d < numDeps; d++) {
          var isCross = rand() < 0.4;
          if (!isCross && phaseKeys[phId].length > 1) {
            var tk = phaseKeys[phId][Math.floor(rand() * (phaseKeys[phId].length - 1))]; if (deps.indexOf(tk) < 0) deps.push(tk);
          } else if (items.length > 0) {
            var tk2 = items[Math.floor(rand() * items.length)].key; if (deps.indexOf(tk2) < 0) deps.push(tk2);
          }
        }
        items.push({ project: p, id: "T-" + items.length, key: key, title: "Task " + key,
          kind: "lane", track: ph, phase: ph, owner: "Owner " + p,
          state: states[Math.floor(rand() * states.length)], estimate_h: 4,
          depends_on: deps, operator: false, priority: Math.floor(rand() * 10), epoch: 1, evidence: [] });
      }
    });
  });
  return { snap: { generated: "2026-10-01T00:00:00Z", head: "synth432", items: items }, phaseKeys: phaseKeys, projects: projects, phases: phases };
}

if (mode === "t1_overview") {
  var s = PlanUI.overviewState(fixtureVm);
  var ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
  var els = PlanUI.buildElements(fixtureVm, ui), res = PlanUI.budgetElements(fixtureVm, ui, els);
  var rawDeps = els.edges.filter(function(e) { return !isStruct(e); });
  var emDeps = res.edges.filter(function(e) { return !isStruct(e); });
  var nodeMap = {}; res.nodes.forEach(function(n) { nodeMap[n.data.id] = n; });
  var crossDiffProj = emDeps.filter(function(e) {
    if (!e.data.cross) return false;
    var sn = nodeMap[e.data.source], tn = nodeMap[e.data.target];
    return sn && tn && sn.data.project !== tn.data.project;
  }).length;
  var allEndsExist = res.edges.every(function(e) { return !!nodeMap[e.data.source] && !!nodeMap[e.data.target]; });
  var ids = res.edges.map(function(e) { return e.data.id; });
  emit({
    report: res.report, rawDepCount: rawDeps.length, emittedDepCount: emDeps.length,
    crossDiffProj: crossDiffProj, allEndsExist: allEndsExist,
    noSelf: res.edges.every(function(e) { return e.data.source !== e.data.target; }),
    uniqueIds: new Set(ids).size === ids.length,
    allMPrefix: emDeps.every(function(e) { return e.data.id.indexOf("m:") === 0; })
  });
} else if (mode === "t2_one_expanded") {
  var p0 = fixtureVm.projects[0], ph2 = p0.phases[2], ph2Id = p0.name + "|" + ph2.name;
  var s = PlanUI.overviewState(fixtureVm, { expandPhases: [ph2Id] });
  var ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
  var els = PlanUI.buildElements(fixtureVm, ui), res = PlanUI.budgetElements(fixtureVm, ui, els);
  var elsStruct = els.edges.filter(isStruct);
  var structDeepEqual = JSON.stringify(elsStruct) === JSON.stringify(res.edges.slice(0, elsStruct.length));
  var nodeMap = {}; res.nodes.forEach(function(n) { nodeMap[n.data.id] = n; });
  var ghostPairs = [];
  ph2.keys.forEach(function(tk) {
    var it = fixtureVm.items.get(tk);
    (it.deps || []).forEach(function(dk) {
      var dep = fixtureVm.items.get(dk);
      if (!dep || dep.project === it.project || (dep.state === "done" && it.state === "done")) return;
      if (nodeMap["g:" + it.key + ">" + dk]) ghostPairs.push({ from: dk, to: it.key, depRep: PlanUI.repOf(fixtureVm, s, dk).id });
    });
  });
  var noGhostDup = ghostPairs.every(function(gp) {
    return !res.edges.some(function(e) {
      return e.data.target === ("t:" + gp.to) && (e.data.source === gp.depRep || (e.data.members || []).some(function(m) { return m.from === gp.from && m.to === gp.to; }));
    });
  });
  var ph2Keys = new Set(ph2.keys), internalPairs = [];
  ph2.keys.forEach(function(tk) {
    var it = fixtureVm.items.get(tk);
    (it.deps || []).forEach(function(dk) {
      if (ph2Keys.has(dk) && !(fixtureVm.items.get(dk).state === "done" && it.state === "done")) internalPairs.push({ from: dk, to: tk });
    });
  });
  var depEdges = res.edges.filter(function(e) { return !isStruct(e); });
  var firstBlock = depEdges.slice(0, internalPairs.length);
  var internalKept = internalPairs.every(function(p) {
    var depIt = fixtureVm.items.get(p.from), expSrc = depIt.loop ? ("x:" + p.from) : ("t:" + p.from);
    return firstBlock.some(function(e) { return e.data.source === expSrc && e.data.target === ("t:" + p.to) && !e.data.meta; });
  });
  var ph4Id = p0.name + "|" + p0.phases[4].name;
  var s2 = PlanUI.overviewState(fixtureVm, { expandPhases: [ph2Id, ph4Id] });
  var els2 = PlanUI.buildElements(fixtureVm, PlanUI.defaultUI({ collapsedProjects: s2.collapsedProjects, collapsedPhases: s2.collapsedPhases }));
  var res2 = PlanUI.budgetElements(fixtureVm, PlanUI.defaultUI({ collapsedProjects: s2.collapsedProjects, collapsedPhases: s2.collapsedPhases }), els2);
  var modelInternal = PlanUI.metaEdges(fixtureVm, s2).edges.filter(function(e) {
    var sIt = e.source.indexOf("t:") === 0 ? fixtureVm.items.get(e.source.slice(2)) : null;
    var tIt = e.target.indexOf("t:") === 0 ? fixtureVm.items.get(e.target.slice(2)) : null;
    return sIt && tIt && sIt.project === tIt.project && sIt.phase === tIt.phase;
  });
  var dep2 = res2.edges.filter(function(e) { return !isStruct(e); }).slice(0, modelInternal.length);
  var orderMatch2 = (dep2.length === modelInternal.length) && modelInternal.every(function(me, idx) { return dep2[idx].data.id === me.id; });
  emit({
    report: res.report, structDeepEqual: structDeepEqual, ghostCount: ghostPairs.length,
    noGhostDup: noGhostDup, internalCount: internalPairs.length, internalKept: internalKept,
    modelInternalCount: modelInternal.length, orderMatch2: orderMatch2
  });
} else if (mode === "t2c_full") {
  var allPh = [];
  fixtureVm.projects.forEach(function(p) { (p.phases || []).forEach(function(ph) { allPh.push(p.name + "|" + ph.name); }); });
  var s = PlanUI.overviewState(fixtureVm, { expandPhases: allPh });
  var ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
  var els = PlanUI.buildElements(fixtureVm, ui), res = PlanUI.budgetElements(fixtureVm, ui, els);
  var nonStructEls = els.edges.filter(function(e) { return !isStruct(e); }), depEdges = res.edges.filter(function(e) { return !isStruct(e); });
  var mEls = {}, mRes = {}, loopSourced = 0;
  nonStructEls.forEach(function(e) { var k = e.data.source + "->" + e.data.target + ":" + e.data.count; mEls[k] = (mEls[k]||0)+1; });
  depEdges.forEach(function(e) {
    var k = e.data.source + "->" + e.data.target + ":" + e.data.count; mRes[k] = (mRes[k]||0)+1;
    if (e.data.source.indexOf("x:") === 0) loopSourced++;
  });
  var match = (Object.keys(mEls).length === Object.keys(mRes).length) && Object.keys(mEls).every(function(k) { return mEls[k] === mRes[k]; });
  emit({ match: match, elsCount: nonStructEls.length, resCount: depEdges.length, ghostRouted: res.report.ghostRouted, loopSourced: loopSourced, report: res.report });
} else if (mode === "t3_synth") {
  var b = build432(), vm = PlanUI.buildViewModel(b.snap), allPh = [];
  b.projects.forEach(function(p) { b.phases.forEach(function(ph) { allPh.push(p + "|" + ph); }); });
  var results = [];
  [0, 1, 6, 20, 36].forEach(function(n) {
    var exp = allPh.slice(0, n);
    var s = PlanUI.overviewState(vm, { expandPhases: exp });
    var ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
    var els = PlanUI.buildElements(vm, ui);
    var r1 = PlanUI.budgetElements(vm, ui, els), r2 = PlanUI.budgetElements(vm, ui, els), rep = r1.report;
    var sumCounts = r1.edges.filter(function(e) { return !isStruct(e); }).reduce(function(a, e) { return a + e.data.count; }, 0);
    var nodeMap = {}; r1.nodes.forEach(function(no) { nodeMap[no.data.id] = no; });
    var maxInt = 0;
    exp.forEach(function(phId) {
      var pSet = new Set(b.phaseKeys[phId]);
      var cnt = r1.edges.filter(function(e) {
        var sEnd = e.data.source.indexOf("t:") === 0 || e.data.source.indexOf("x:") === 0 ? e.data.source.slice(2) : null;
        var tEnd = e.data.target.indexOf("t:") === 0 ? e.data.target.slice(2) : null;
        return sEnd && tEnd && pSet.has(sEnd) && pSet.has(tEnd);
      }).length;
      if (cnt > maxInt) maxInt = cnt;
    });
    results.push({
      n: n, report: rep, edgeBudget: PlanUI.edgeBudget(vm, ui), det: JSON.stringify(r1) === JSON.stringify(r2),
      b4: (sumCounts + rep.filteredOut + rep.ghostRouted) === rep.shown, m5: (rep.shown + rep.intraHidden + rep.hidden) === rep.total,
      allNodes: r1.edges.every(function(e) { return !!nodeMap[e.data.source] && !!nodeMap[e.data.target]; }),
      maxInt: maxInt, rawDep: els.edges.filter(function(e) { return !isStruct(e); }).length
    });
  });
  emit({ results: results });
} else if (mode === "t3b_chain") {
  var items = [];
  for (var i = 1; i <= 61; i++) {
    items.push({ project: "p0", id: "T-" + i, key: "p0:T-" + i, title: "Task " + i, kind: "lane",
      track: "ph0", phase: "ph0", owner: "Owner", state: "ready", display: "ready", estimate_h: 4,
      depends_on: i > 1 ? ["p0:T-" + (i - 1)] : [], operator: false, priority: 1, epoch: 1, evidence: [] });
  }
  var vm = PlanUI.buildViewModel({ generated: "2026-10-01T00:00:00Z", head: "chain61", items: items });
  var s = PlanUI.overviewState(vm, { expandPhases: ["p0|ph0"] });
  var ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
  var res = PlanUI.budgetElements(vm, ui, PlanUI.buildElements(vm, ui)), rep = res.report;
  var sumC = res.edges.filter(function(e) { return !isStruct(e); }).reduce(function(a, e) { return a + e.data.count; }, 0);
  emit({ report: rep, b4: (sumC + rep.filteredOut + rep.ghostRouted) === rep.shown, m5: (rep.shown + rep.intraHidden + rep.hidden) === rep.total });
} else if (mode === "t4_filter") {
  var ph2Id = fixtureVm.projects[0].name + "|" + fixtureVm.projects[0].phases[2].name;
  var s = PlanUI.overviewState(fixtureVm, { expandPhases: [ph2Id] });
  var uDim = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases, query: "schema", matchMode: "dim" });
  var rDim = PlanUI.budgetElements(fixtureVm, uDim, PlanUI.buildElements(fixtureVm, uDim)), nMap = {};
  rDim.nodes.forEach(function(n) { nMap[n.data.id] = n; });
  var dimOk = rDim.edges.every(function(e) {
    var sn = nMap[e.data.source], tn = nMap[e.data.target];
    var sD = !sn || ((sn.classes||"").indexOf("dim") >= 0), tD = !tn || ((tn.classes||"").indexOf("dim") >= 0);
    return ((e.classes||"").indexOf("dim") >= 0) === (sD && tD);
  });
  var uHide = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases, query: "atlas", matchMode: "hide" });
  var rHide = PlanUI.budgetElements(fixtureVm, uHide, PlanUI.buildElements(fixtureVm, uHide));
  var sumH = rHide.edges.filter(function(e) { return !isStruct(e); }).reduce(function(a, e) { return a + e.data.count; }, 0);
  var uCrit = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases, filters: { edgeMode: "critical" }, matchMode: "dim" });
  var rCrit = PlanUI.budgetElements(fixtureVm, uCrit, PlanUI.buildElements(fixtureVm, uCrit)), nMapC = {};
  rCrit.nodes.forEach(function(n) { nMapC[n.data.id] = n; });
  var critOk = rCrit.edges.every(function(e) {
    if (isStruct(e)) return true;
    var sn = nMapC[e.data.source], tn = nMapC[e.data.target];
    var sD = !sn || ((sn.classes||"").indexOf("dim") >= 0), tD = !tn || ((tn.classes||"").indexOf("dim") >= 0), eD = (e.classes||"").indexOf("dim") >= 0;
    return !e.data.critical ? eD : (eD === (sD && tD));
  });
  var uCross = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases, filters: { edgeMode: "cross" }, matchMode: "dim" });
  var rCross = PlanUI.budgetElements(fixtureVm, uCross, PlanUI.buildElements(fixtureVm, uCross)), nMapX = {};
  rCross.nodes.forEach(function(n) { nMapX[n.data.id] = n; });
  var crossOk = rCross.edges.every(function(e) {
    if (isStruct(e)) return true;
    var sn = nMapX[e.data.source], tn = nMapX[e.data.target];
    var sD = !sn || ((sn.classes||"").indexOf("dim") >= 0), tD = !tn || ((tn.classes||"").indexOf("dim") >= 0), eD = (e.classes||"").indexOf("dim") >= 0;
    return !e.data.cross ? eD : (eD === (sD && tD));
  });
  var uCritH = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases, filters: { edgeMode: "critical" }, matchMode: "hide" });
  var rCritH = PlanUI.budgetElements(fixtureVm, uCritH, PlanUI.buildElements(fixtureVm, uCritH));
  emit({ dimOk: dimOk, hideFiltered: rHide.report.filteredOut, hideCons: (sumH + rHide.report.filteredOut + rHide.report.ghostRouted) === rHide.report.shown,
    critOk: critOk, crossOk: crossOk, critHideFiltered: rCritH.report.filteredOut });
} else if (mode === "t5_limit") {
  var s = PlanUI.overviewState(fixtureVm), ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
  var els = PlanUI.buildElements(fixtureVm, ui), r3 = PlanUI.budgetElements(fixtureVm, ui, els, { limit: 3 }), rDef = PlanUI.budgetElements(fixtureVm, ui, els);
  var sum3 = r3.edges.filter(function(e) { return !isStruct(e); }).reduce(function(a, e) { return a + e.data.count; }, 0);
  var inv = [0, -1, 2.5, "x"].every(function(lim) { return JSON.stringify(PlanUI.budgetElements(fixtureVm, ui, els, { limit: lim }).report) === JSON.stringify(rDef.report); });
  emit({ dep3: r3.report.dependency, limit3: r3.report.limit,
    b4: (sum3 + r3.report.filteredOut + r3.report.ghostRouted) === r3.report.shown,
    m5: (r3.report.shown + r3.report.intraHidden + r3.report.hidden) === r3.report.total, invPass: inv });
} else if (mode === "t6_titles") {
  var vmDummy = { items: new Map([["a", { key: "a", title: "Alpha Title That Exceeds Exactly Forty Eight Chars Limit Really" }], ["b", { key: "b", title: "Beta" }]]) };
  var mem = [{ from: "a", to: "b" }, { from: "b", to: "unknown_key" }];
  for (var i = 0; i < 12; i++) mem.push({ from: "b", to: "b" });
  emit({
    sNone: PlanUI.budgetSummary({ dependency: 12, aggregated: 0, hidden: 0 }),
    sAgg: PlanUI.budgetSummary({ dependency: 12, aggregated: 5, hidden: 0 }),
    sHid: PlanUI.budgetSummary({ dependency: 12, aggregated: 0, hidden: 7 }),
    sBoth: PlanUI.budgetSummary({ dependency: 12, aggregated: 5, hidden: 7 }),
    sNull: PlanUI.budgetSummary(null), sUndef: PlanUI.budgetSummary(undefined),
    titles: PlanUI.edgeMembersTitle(vmDummy, { members: mem }, 12),
    empty: PlanUI.edgeMembersTitle(vmDummy, { members: [] })
  });
} else if (mode === "t7_purity") {
  var s = PlanUI.overviewState(fixtureVm), ui = PlanUI.defaultUI({ collapsedProjects: s.collapsedProjects, collapsedPhases: s.collapsedPhases });
  var els = PlanUI.buildElements(fixtureVm, ui);
  function df(o) { if (o && typeof o === "object") { Object.freeze(o); Object.keys(o).forEach(function(k) { df(o[k]); }); } return o; }
  var elsBefore = JSON.stringify(els), uiProjBefore = JSON.stringify(Array.from(ui.collapsedProjects));
  PlanUI.budgetElements(fixtureVm, ui, els);
  var purity = (JSON.stringify(els) === elsBefore) && (JSON.stringify(Array.from(ui.collapsedProjects)) === uiProjBefore);
  df(fixtureVm); df(ui);
  emit({ purity: purity, frozenPass: !!PlanUI.budgetElements(fixtureVm, ui, els),
    nullSafe: PlanUI.budgetElements(null).nodes.length === 0 && PlanUI.budgetElements(fixtureVm, null, null).nodes.length === 0 });
}
"""


def run_node(tmp_path: Path, mode: str, *args) -> dict:
    runner_path = tmp_path / "runner_budget.js"
    runner_path.write_text(RUNNER_JS, encoding="utf-8")
    cmd = [
        "node", str(runner_path), mode,
        str(WEB / "page.js"), str(WEB / "page_model.js"), str(WEB / "page_store.js"),
        str(WEB / "page_search.js"), str(WEB / "page_graph.js"), str(WEB / "page_graph_cluster.js"),
        str(WEB / "page_graph_budget.js"), str(FIXTURE), *[str(a) for a in args],
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_t1_overview(tmp_path: Path):
    res = run_node(tmp_path, "t1_overview")
    rep = res["report"]
    assert rep["dependency"] <= rep["limit"]
    assert rep["limit"] == 40 and res["rawDepCount"] == 12 and res["emittedDepCount"] == 22
    assert res["noSelf"] and res["uniqueIds"] and res["allMPrefix"] and res["allEndsExist"]
    assert res["crossDiffProj"] >= 1 and rep["filteredOut"] == 0 and rep["compoundRouted"] == 0


@need_node
def test_t2_one_expanded_and_internal_kept(tmp_path: Path):
    res = run_node(tmp_path, "t2_one_expanded")
    rep = res["report"]
    assert rep["limit"] == 150 and rep["dependency"] <= 150 and res["structDeepEqual"]
    assert res["ghostCount"] >= 1 and rep["ghostRouted"] == res["ghostCount"] and res["noGhostDup"]
    assert res["internalCount"] >= 1 and res["internalKept"] and res["modelInternalCount"] == 26 and res["orderMatch2"]


@need_node
def test_t2c_full_expansion(tmp_path: Path):
    res = run_node(tmp_path, "t2c_full")
    assert res["match"] and res["elsCount"] == 263 and res["resCount"] == 263
    assert res["report"]["dependency"] <= res["report"]["limit"] and res["report"]["limit"] == 400
    assert res["report"]["aggregated"] == 0 and res["ghostRouted"] == 10 and res["loopSourced"] == 12


@need_node
def test_t3_large_synthetic_vm(tmp_path: Path):
    res = run_node(tmp_path, "t3_synth")
    results = res["results"]
    assert len(results) == 5
    for item in results:
        rep = item["report"]
        assert rep["dependency"] <= rep["limit"]
        assert item["det"] and item["b4"] and item["m5"] and item["allNodes"] and item["maxInt"] <= 50
    r0 = results[0]["report"]
    assert r0["dependency"] == 15 and r0["aggregated"] > 0 and r0["compoundRouted"] == 306


@need_node
def test_t3b_phase_cap(tmp_path: Path):
    res = run_node(tmp_path, "t3b_chain")
    rep = res["report"]
    assert rep["dependency"] == 50 and rep["intraHidden"] == 10 and rep["hidden"] == 0
    assert res["b4"] and res["m5"]


@need_node
def test_t4_filtering(tmp_path: Path):
    res = run_node(tmp_path, "t4_filter")
    assert res["dimOk"] and res["hideFiltered"] > 0 and res["hideCons"]
    assert res["critOk"] and res["crossOk"] and res["critHideFiltered"] > 0


@need_node
def test_t5_limit_option(tmp_path: Path):
    res = run_node(tmp_path, "t5_limit")
    assert res["dep3"] <= 3 and res["limit3"] == 3 and res["b4"] and res["m5"] and res["invPass"]


@need_node
def test_t6_summary_titles(tmp_path: Path):
    res = run_node(tmp_path, "t6_titles")
    assert res["sNone"] == "12 edges"
    assert res["sAgg"] == "12 edges (5 aggregated, 0 hidden)"
    assert res["sHid"] == "12 edges (0 aggregated, 7 hidden)"
    assert res["sBoth"] == "12 edges (5 aggregated, 7 hidden)"
    assert res["sNull"] == "" and res["sUndef"] == ""
    titles = res["titles"]
    assert len(titles) == 13
    assert titles[0] == "Alpha Title That Exceeds Exactly Forty Eight Cha\u2026 -> Beta"
    assert titles[1] == "Beta -> unknown_key" and titles[-1] == "+2 more" and res["empty"] == []


@need_node
def test_t7_purity_robustness(tmp_path: Path):
    res = run_node(tmp_path, "t7_purity")
    assert res["purity"] and res["frozenPass"] and res["nullSafe"]
    mod_text = (WEB / "page_graph_budget.js").read_text(encoding="utf-8")
    assert len(mod_text.splitlines()) <= 400
    for forbidden in ["document", "cytoscape", "innerHTML"]:
        assert forbidden not in mod_text
    assert "window." not in mod_text.replace("typeof window", "")
    from plangraph import render as render_mod
    idx_budget = render_mod.JS_FILES.index("page_graph_budget.js")
    idx_cluster = render_mod.JS_FILES.index("page_graph_cluster.js")
    idx_layout = render_mod.JS_FILES.index("page_layout.js")
    assert idx_cluster < idx_budget < idx_layout
