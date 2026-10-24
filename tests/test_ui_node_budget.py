"""UI-NODE-BUDGET: an expanded phase renders at most NODE_BUDGET task nodes plus
ONE "+K more" node (seat finding 6; edges were budgeted, nodes were not).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "plangraph" / "web"
need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

RUNNER_JS = r"""
"use strict";
var fs = require("fs");
require(process.argv[2]); var PlanUI = require(process.argv[3]);
require(process.argv[4]); require(process.argv[5]); require(process.argv[6]);
var P = JSON.parse(process.argv[7]);
function mk(n) {
  var items = [];
  for (var i = 0; i < n; i++) {
    items.push({ project: "big", id: "T-" + i, key: "big:T-" + i, title: "Task " + i, kind: "lane",
      track: "P1", phase: "P1", owner: "o", state: i % 3 ? "ready" : "done", estimate_h: 1,
      depends_on: i ? ["big:T-" + (i - 1)] : [], operator: false, priority: 0, epoch: 1, evidence: [] });
  }
  return { generated: "2026-10-08T00:00:00Z", head: "x", items: items, questions: [], decisions: [] };
}
var t0 = Date.now();
var vm = PlanUI.buildViewModel(mk(P.n));
var els = PlanUI.buildElements(vm, { settings: P.settings || {} });
var ms = Date.now() - t0;
var tasks = els.nodes.filter(function (x) { return x.data.id.indexOf("t:") === 0; });
var more = els.nodes.filter(function (x) { return (x.classes || "").indexOf("more-node") >= 0; });
var ids = {}; els.nodes.forEach(function (x) { ids[x.data.id] = 1; });
var dangling = els.edges.filter(function (e) { return !ids[e.data.source] || !ids[e.data.target]; }).length;
process.stdout.write(JSON.stringify({ budget: PlanUI.NODE_BUDGET, tasks: tasks.length, more: more.map(function (m) {
  return { id: m.data.id, label: m.data.label, hidden: m.data.hiddenCount, parent: m.data.parent }; }),
  edges: els.edges.length, dangling: dangling, ms: ms }));
"""


def run(tmp_path: Path, n: int, settings=None) -> dict:
    rp = tmp_path / "r.js"
    rp.write_text(RUNNER_JS, encoding="utf-8")
    cmd = ["node", str(rp), str(WEB / "page.js"), str(WEB / "page_model.js"), str(WEB / "page_store.js"),
           str(WEB / "page_search.js"), str(WEB / "page_graph.js"),
           json.dumps({"n": n, "settings": settings})]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@need_node
def test_1000_task_phase_is_capped_with_one_more_node(tmp_path):
    out = run(tmp_path, 1000)
    n = out["budget"]
    assert 0 < n < 1000
    assert out["tasks"] == n
    assert out["more"] == [{"id": "more:big|P1", "label": f"+{1000 - n} more",
                            "hidden": 1000 - n, "parent": "ph:big|P1"}]
    assert out["dangling"] == 0          # edges to folded tasks are dropped, never dangling
    assert out["ms"] < 5000              # perf: 1000-task fixture builds fast


@need_node
def test_under_budget_has_no_more_node(tmp_path):
    out = run(tmp_path, 50)
    assert out["tasks"] == 50 and out["more"] == []


@need_node
def test_settings_override_node_budget(tmp_path):
    out = run(tmp_path, 100, {"nodeBudget": 10})
    assert out["tasks"] == 10 and out["more"][0]["hidden"] == 90
    out = run(tmp_path, 100, {"nodeBudget": 0})   # invalid -> default
    assert out["tasks"] == min(100, out["budget"])
