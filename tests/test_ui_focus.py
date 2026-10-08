"""T3a pure core — node-run tests for PlanUI graph focus model (GU-G3a)."""

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

if (mode === "t1_small_graph") {
  var nodes = [
    { data: { id: "p:A" } }, { data: { id: "p:B" } },
    { data: { id: "ph:A|1", parent: "p:A" } }, { data: { id: "ph:A|2", parent: "p:A" } },
    { data: { id: "ph:B|1", parent: "p:B" } },
    { data: { id: "t:A:1", parent: "ph:A|1" } }, { data: { id: "t:A:2", parent: "ph:A|1" } },
    { data: { id: "t:A:3", parent: "ph:A|2" } }, { data: { id: "t:A:4", parent: "ph:A|2" } },
    { data: { id: "t:B:1", parent: "ph:B|1" } }, { data: { id: "t:B:2", parent: "ph:B|1" } },
    { data: { id: "g:B:1>A:1", parent: "ph:B|1", dep: "A:1" } }
  ];
  var edges = [
    { data: { id: "e:1->2", source: "t:A:1", target: "t:A:2" } },
    { data: { id: "e:1->3", source: "t:A:1", target: "t:A:3" } },
    { data: { id: "e:2->3", source: "t:A:2", target: "t:A:3" } },
    { data: { id: "e:3->4", source: "t:A:3", target: "t:A:4" } },
    { data: { id: "e:g->B1", source: "g:B:1>A:1", target: "t:B:1" } },
    { data: { id: "e:B1->B2", source: "t:B:1", target: "t:B:2" } }
  ];
  var g = { nodes: nodes, edges: edges };
  var sets = PlanUI.focusSets(g, "t:A:1", { hops: 1 });
  emit({
    sets: sets, classes: PlanUI.focusClasses(sets),
    allNodes: nodes.map(function(n) { return n.data.id; }),
    allEdges: edges.map(function(e) { return e.data.id; })
  });
} else if (mode === "t2_hops") {
  var nodes = [1,2,3,4,5].map(function(i) { return { data: { id: "t:" + i } }; });
  var edges = [
    { data: { id: "e:1-2", source: "t:1", target: "t:2" } },
    { data: { id: "e:2-3", source: "t:2", target: "t:3" } },
    { data: { id: "e:3-4", source: "t:3", target: "t:4" } },
    { data: { id: "e:4-5", source: "t:4", target: "t:5" } }
  ];
  var g = { nodes: nodes, edges: edges };
  emit({
    h2: PlanUI.focusSets(g, "t:1", { hops: 2 }),
    h0: PlanUI.focusSets(g, "t:1", { hops: 0 }),
    hNeg: PlanUI.focusSets(g, "t:1", { hops: -1 }),
    hNonInt: PlanUI.focusSets(g, "t:1", { hops: 1.5 }),
    h9: PlanUI.focusSets(g, "t:1", { hops: 9 })
  });
} else if (mode === "t3_compound") {
  var nodes = [
    { data: { id: "p:A" } },
    { data: { id: "ph:A|1", parent: "p:A" } },
    { data: { id: "t:A:1", parent: "ph:A|1" } },
    { data: { id: "t:A:2", parent: "ph:A|1" } },
    { data: { id: "ph:A|2", parent: "p:A" } },
    { data: { id: "t:A:3", parent: "ph:A|2" } },
    { data: { id: "phc:A|3", parent: "p:A", keys: ["A:4"] } },
    { data: { id: "t:A:5", parent: "ph:A|2" } }
  ];
  var edges = [
    { data: { id: "e:1-3", source: "t:A:1", target: "t:A:3" } },
    { data: { id: "e:phc-5", source: "phc:A|3", target: "t:A:5" } }
  ];
  var g = { nodes: nodes, edges: edges };
  emit({ ph: PlanUI.focusSets(g, "ph:A|1"), phc: PlanUI.focusSets(g, "phc:A|3") });
} else if (mode === "t4_ghost") {
  var nodes = [
    { data: { id: "p:A" } }, { data: { id: "t:A:1" } },
    { data: { id: "g:B:1>A:1", dep: "A:1" } },
    { data: { id: "g:B:2>A:99", dep: "A:99" } }
  ];
  var g = { nodes: nodes, edges: [{ data: { id: "e:g1", source: "g:B:1>A:1", target: "t:A:1" } }] };
  emit({ withDep: PlanUI.focusSets(g, "g:B:1>A:1"), withoutDep: PlanUI.focusSets(g, "g:B:2>A:99") });
} else if (mode === "t5_synthetic") {
  var snap = JSON.parse(fs.readFileSync(process.argv[9], "utf8"));
  var vm = PlanUI.buildViewModel(snap);
  var els = PlanUI.buildElements(vm, PlanUI.defaultUI());
  var edgeCount = {};
  els.edges.forEach(function (e) {
    edgeCount[e.data.source] = (edgeCount[e.data.source] || 0) + 1;
    edgeCount[e.data.target] = (edgeCount[e.data.target] || 0) + 1;
  });
  var pickedTask = null;
  els.nodes.forEach(function (n) {
    if (!pickedTask && n.data.id.indexOf("t:") === 0 && (edgeCount[n.data.id] || 0) >= 3) {
      pickedTask = n.data.id;
    }
  });
  var expectedPartners = new Set();
  els.edges.forEach(function (e) {
    if (e.data.source === pickedTask) expectedPartners.add(e.data.target);
    if (e.data.target === pickedTask) expectedPartners.add(e.data.source);
  });
  var pickedPh = null;
  els.nodes.forEach(function (n) {
    if (!pickedPh && n.data.id.indexOf("ph:") === 0) pickedPh = n.data.id;
  });
  emit({
    pickedTask: pickedTask,
    expectedPartners: Array.from(expectedPartners).sort(),
    totalNodes: els.nodes.length,
    totalEdges: els.edges.length,
    taskSets: PlanUI.focusSets(els, pickedTask),
    unknownRes: PlanUI.focusSets(els, "unknown-id-xyz"),
    nonGraphRes: PlanUI.focusSets(null, pickedTask),
    pickedPh: pickedPh,
    phaseSets: PlanUI.focusSets(els, pickedPh)
  });
} else if (mode === "t6_reduce") {
  var t = [];
  var s = PlanUI.focusReduce(undefined, { type: "tap", id: "t:1" });
  t.push({ act: "tap", s: s });
  t.push({ act: "tap same", s: PlanUI.focusReduce(s, { type: "tap", id: "t:1" }) });
  t.push({ act: "tap empty", s: PlanUI.focusReduce(s, { type: "tap", id: "" }) });
  t.push({ act: "tapEmpty", s: PlanUI.focusReduce(s, { type: "tapEmpty" }) });
  t.push({ act: "esc", s: PlanUI.focusReduce({ focusId: "t:2", expand: null }, { type: "esc" }) });
  var sPhc = PlanUI.focusReduce(null, { type: "dbltap", id: "phc:proj1|phaseA" });
  t.push({ act: "dbltap phc", s: sPhc });
  t.push({ act: "reset expand", s: PlanUI.focusReduce(sPhc, { type: "tap", id: "t:3" }) });
  t.push({ act: "dbltap pc", s: PlanUI.focusReduce(null, { type: "dbltap", id: "pc:proj1" }) });
  t.push({ act: "dbltap task", s: PlanUI.focusReduce(null, { type: "dbltap", id: "t:4" }) });
  var fr = Object.freeze({ focusId: "t:5", expand: { kind: "project", id: "p1" } });
  t.push({ act: "unknown", s: PlanUI.focusReduce(fr, { type: "other_act" }) });
  t.push({ act: "undefined", s: PlanUI.focusReduce(undefined, { type: "other_act" }) });
  emit(t);
} else if (mode === "t7_rebuild") {
  var g = {
    nodes: [
      { data: { id: "p:A" } },
      { data: { id: "phc:A|1", keys: ["A:1", "A:2"] } },
      { data: { id: "pc:B", keys: ["B:1"] } }
    ],
    edges: []
  };
  emit({
    exact: PlanUI.focusAfterRebuild(g, "p:A"),
    phc_to_ph: PlanUI.focusAfterRebuild(g, "ph:A|1"),
    p_to_pc: PlanUI.focusAfterRebuild(g, "p:B"),
    task_in_phc: PlanUI.focusAfterRebuild(g, "t:A:1"),
    task_in_pc: PlanUI.focusAfterRebuild(g, "t:B:1"),
    task_not_found: PlanUI.focusAfterRebuild(g, "t:C:99"),
    empty_graph: PlanUI.focusAfterRebuild(null, "t:A:1")
  });
} else if (mode === "t8_label") {
  var items = new Map();
  items.set("A-1", { key: "A-1", title: "Short title" });
  items.set("A-2", { key: "A-2", title: "A".repeat(80) });
  var vm = { items: items };
  emit({
    taskShort: PlanUI.focusLabel(vm, "t:A-1"),
    taskLong: PlanUI.focusLabel(vm, "t:A-2"),
    phCompound: PlanUI.focusLabel(vm, "ph:proj|phase1"),
    phCollapsed: PlanUI.focusLabel(vm, "phc:proj|phase1"),
    pCompound: PlanUI.focusLabel(vm, "p:proj"),
    pCollapsed: PlanUI.focusLabel(vm, "pc:proj"),
    unknown: PlanUI.focusLabel(vm, "x:unknown"),
    empty: PlanUI.focusLabel(vm, "")
  });
}
"""


def run_focus_node(tmp_path: Path, mode: str, *args) -> dict | list:
    runner_path = tmp_path / "runner_focus.js"
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
        str(WEB / "page_graph_focus.js"),
        str(FIXTURE),
    ] + [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@need_node
def test_t1_small_graph(tmp_path: Path):
    res = run_focus_node(tmp_path, "t1_small_graph")
    sets, classes = res["sets"], res["classes"]
    all_nodes, all_edges = set(res["allNodes"]), set(res["allEdges"])

    assert sets["core"] == ["t:A:1"]
    assert sets["near"] == ["t:A:2", "t:A:3"]
    assert sets["context"] == ["p:A", "ph:A|1", "ph:A|2"]
    assert sets["keepEdges"] == ["e:1->2", "e:1->3"]
    assert "e:2->3" in sets["fadeEdges"]

    c_s, n_s, x_s, f_s = set(sets["core"]), set(sets["near"]), set(sets["context"]), set(sets["faded"])
    assert c_s | n_s | x_s | f_s == all_nodes
    assert len(c_s) + len(n_s) + len(x_s) + len(f_s) == len(all_nodes)

    k_e, f_e = set(sets["keepEdges"]), set(sets["fadeEdges"])
    assert k_e | f_e == all_edges
    assert k_e & f_e == set()

    for nid in sets["core"]:
        assert classes["nodes"][nid] == "core"
    for nid in sets["near"]:
        assert classes["nodes"][nid] == "near"
    for nid in sets["context"]:
        assert classes["nodes"][nid] == "context"
    for nid in sets["faded"]:
        assert classes["nodes"][nid] == "faded"
    for eid in sets["keepEdges"]:
        assert classes["edges"][eid] == "keep"
    for eid in sets["fadeEdges"]:
        assert classes["edges"][eid] == "fade"


@need_node
def test_t2_hops(tmp_path: Path):
    res = run_focus_node(tmp_path, "t2_hops")
    assert res["h2"]["core"] == ["t:1"]
    assert res["h2"]["near"] == ["t:2", "t:3"]
    assert "e:1-2" in res["h2"]["keepEdges"]
    assert "e:2-3" in res["h2"]["keepEdges"]
    assert "e:3-4" in res["h2"]["fadeEdges"]

    assert res["h0"]["near"] == ["t:2"]
    assert res["hNeg"]["near"] == ["t:2"]
    assert res["hNonInt"]["near"] == ["t:2"]
    assert res["h9"]["near"] == ["t:2", "t:3", "t:4"]
    assert "t:5" in res["h9"]["faded"]


@need_node
def test_t3_compound_focus(tmp_path: Path):
    res = run_focus_node(tmp_path, "t3_compound")
    ph = res["ph"]
    assert {"ph:A|1", "t:A:1", "t:A:2"}.issubset(set(ph["core"]))
    assert "t:A:3" in ph["near"]
    assert "e:1-3" in ph["keepEdges"]
    assert "p:A" in ph["context"]

    phc = res["phc"]
    assert phc["core"] == ["phc:A|3"]
    assert "t:A:5" in phc["near"]
    assert "e:phc-5" in phc["keepEdges"]


@need_node
def test_t4_ghost(tmp_path: Path):
    res = run_focus_node(tmp_path, "t4_ghost")
    assert res["withDep"]["core"] == ["g:B:1>A:1", "t:A:1"]
    assert res["withoutDep"]["core"] == ["g:B:2>A:99"]


@need_node
def test_t5_synthetic(tmp_path: Path):
    res = run_focus_node(tmp_path, "t5_synthetic")
    assert res["unknownRes"] is None
    assert res["nonGraphRes"] is None

    ts = res["taskSets"]
    assert ts is not None and ts["core"] == [res["pickedTask"]]
    assert ts["near"] == res["expectedPartners"]
    assert len(ts["faded"]) > 0

    tot_n, tot_e = res["totalNodes"], res["totalEdges"]
    assert len(ts["core"]) + len(ts["near"]) + len(ts["context"]) + len(ts["faded"]) == tot_n
    assert len(ts["keepEdges"]) + len(ts["fadeEdges"]) == tot_e
    assert len(ts["core"]) + len(ts["near"]) + len(ts["context"]) < tot_n
    assert len(ts["fadeEdges"]) > len(ts["keepEdges"])


@need_node
def test_t6_reduce(tmp_path: Path):
    t = run_focus_node(tmp_path, "t6_reduce")
    assert len(t) >= 10
    assert t[0]["s"]["focusId"] == "t:1"
    assert t[1]["s"]["focusId"] == "t:1"
    assert t[2]["s"]["focusId"] == "t:1"
    assert t[3]["s"]["focusId"] is None
    assert t[4]["s"]["focusId"] is None
    assert t[5]["s"]["focusId"] == "ph:proj1|phaseA"
    assert t[5]["s"]["expand"] == {"kind": "phase", "id": "proj1|phaseA"}
    assert t[6]["s"]["focusId"] == "t:3" and t[6]["s"]["expand"] is None
    assert t[7]["s"]["focusId"] == "p:proj1"
    assert t[7]["s"]["expand"] == {"kind": "project", "id": "proj1"}
    assert t[8]["s"]["focusId"] == "t:4" and t[8]["s"]["expand"] is None
    assert t[9]["s"]["focusId"] == "t:5" and t[9]["s"]["expand"] is None
    assert t[10]["s"]["focusId"] is None and t[10]["s"]["expand"] is None


@need_node
def test_t7_rebuild(tmp_path: Path):
    res = run_focus_node(tmp_path, "t7_rebuild")
    assert res["exact"] == "p:A"
    assert res["phc_to_ph"] == "phc:A|1"
    assert res["p_to_pc"] == "pc:B"
    assert res["task_in_phc"] == "phc:A|1"
    assert res["task_in_pc"] == "pc:B"
    assert res["task_not_found"] is None
    assert res["empty_graph"] is None


@need_node
def test_t8_label(tmp_path: Path):
    res = run_focus_node(tmp_path, "t8_label")
    assert res["taskShort"] == "A-1 - Short title"
    assert res["taskLong"] == "A-2 - " + "A" * 60 + "…"
    assert res["phCompound"] == "proj > phase1"
    assert res["phCollapsed"] == "proj > phase1"
    assert res["pCompound"] == "proj"
    assert res["pCollapsed"] == "proj"
    assert res["unknown"] == ""
    assert res["empty"] == ""


def test_t9_registration():
    from plangraph import render

    assert "page_graph_focus.js" in render.JS_FILES
    assert render.JS_FILES.count("page_graph_focus.js") == 1
    g_idx = render.JS_FILES.index("page_graph.js")
    f_idx = render.JS_FILES.index("page_graph_focus.js")
    l_idx = render.JS_FILES.index("page_layout.js")
    assert g_idx < f_idx < l_idx

    focus_js = (WEB / "page_graph_focus.js").read_text(encoding="utf-8")
    assert len(focus_js.splitlines()) <= 400
    assert "document" not in focus_js
    assert "cytoscape" not in focus_js
    assert "innerHTML" not in focus_js
    assert re.findall(r"window\.[a-zA-Z0-9_$]+", focus_js) == []
