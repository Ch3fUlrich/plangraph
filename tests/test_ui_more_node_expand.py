"""UI-MORE-NODE-EXPAND: tasks folded behind the "+K more" node stay reachable.

Tap = ``ui.moreShown[phId] += budget``; jump/search reveal widens the window via
``revealPatch``; search hits sort ahead of non-matches and folded hits still count.
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
require(process.argv[2]); var PlanUI = require(process.argv[3]);
require(process.argv[4]); require(process.argv[5]); require(process.argv[6]);
var P = JSON.parse(process.argv[7]);
var items = [];
for (var i = 0; i < 1000; i++) {
  items.push({ project: "big", id: "T-" + i, key: "big:T-" + i, title: "Task " + i + (i === 900 ? " needle" : ""),
    kind: "lane", track: "P1", phase: "P1", owner: "o", state: "ready", estimate_h: 1, depends_on: [],
    operator: false, priority: 0, epoch: 1, evidence: [] });
}
var vm = PlanUI.buildViewModel({ generated: "2026-10-08T00:00:00Z", head: "x", items: items, questions: [], decisions: [] });
function summary(ui) {
  var els = PlanUI.buildElements(vm, ui);
  var ids = {}; els.nodes.forEach(function (x) { ids[x.data.id] = 1; });
  var more = els.nodes.filter(function (x) { return (x.classes || "").indexOf("more-node") >= 0; })[0] || null;
  return { tasks: els.nodes.filter(function (x) { return x.data.id.indexOf("t:") === 0; }).length,
           has900: !!ids["t:big:T-900"], has700: !!ids["t:big:T-700"],
           more: more && { hidden: more.data.hiddenCount, phId: more.data.phId, keys: more.data.keys.length,
                           hiddenMatches: more.data.hiddenMatches },
           counts: PlanUI.matchCounts(els) };
}
var out = {};
out.base = summary({ settings: {} });
out.tapped = summary({ settings: {}, moreShown: { "big|P1": PlanUI.NODE_BUDGET } });
out.junk = summary({ settings: {}, moreShown: { "big|P1": -5, "x": "y" } });
var patch = PlanUI.revealPatch(vm, { settings: {}, moreShown: {} }, "big:T-700");
out.patch = patch;
out.revealed = summary({ settings: {}, moreShown: patch.moreShown });
out.noPatchInBudget = PlanUI.revealPatch(vm, { settings: {}, moreShown: {} }, "big:T-5");
out.search = summary({ settings: {}, query: "needle" });
out.searchSmall = summary({ settings: { nodeBudget: 1 }, query: "Task 9" });
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("more")
    rp = tmp / "r.js"
    rp.write_text(RUNNER_JS, encoding="utf-8")
    cmd = ["node", str(rp), str(WEB / "page.js"), str(WEB / "page_model.js"), str(WEB / "page_store.js"),
           str(WEB / "page_search.js"), str(WEB / "page_graph.js"), json.dumps({})]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@need_node
def test_more_node_carries_its_phase_and_folded_keys(out):
    assert out["base"]["tasks"] == 200 and not out["base"]["has700"]
    assert out["base"]["more"] == {"hidden": 800, "phId": "big|P1", "keys": 800, "hiddenMatches": 0}


@need_node
def test_tap_widens_the_window_by_one_budget(out):
    assert out["tapped"]["tasks"] == 400
    assert out["tapped"]["more"]["hidden"] == 600


@need_node
def test_bad_more_shown_values_are_ignored(out):
    assert out["junk"]["tasks"] == 200


@need_node
def test_jump_to_a_folded_task_reveals_it(out):
    assert out["patch"]["moreShown"] == {"big|P1": 701}
    assert out["revealed"]["has700"]
    assert out["noPatchInBudget"] == {}


@need_node
def test_search_hits_come_first_and_folded_hits_still_count(out):
    # "needle" matches only T-900; it is drawn although it sits past the budget.
    assert out["search"]["has900"]
    # Budget 1, "Task 9" matches many tasks: one drawn (a hit), the other hits folded but counted.
    small = out["searchSmall"]
    assert small["tasks"] == 1 and small["more"]["hiddenMatches"] > 0
    assert small["counts"]["nodes"] == 1 + small["more"]["hiddenMatches"]


TAP_JS = r"""
"use strict";
require(process.argv[2]); var PlanUI = require(process.argv[3]);
require(process.argv[4]); require(process.argv[5]); require(process.argv[6]); require(process.argv[7]);
var handler = null, sets = [];
var cy = { on: function (ev, sel, fn) { if (ev === "tap" && sel === "node.more-node") handler = fn; } };
var state = { settings: {}, moreShown: { "big|P1": 200 } };
var ui = { get: function (k) { return k ? state[k] : state; }, set: function (k, v) { sets.push([k, v]); state[k] = v; } };
PlanUI.GraphExt.call("bind", null, { cy: cy, ui: function () { return ui; } });
var ok = !!handler;
handler({ target: { data: function () { return { phId: "big|P1" }; } } });
handler({ target: { data: function () { return { phId: "other|X" }; } } });
handler({ target: { data: function () { return {}; } } });
process.stdout.write(JSON.stringify({ bound: ok, sets: sets }));
"""


@need_node
def test_tap_on_more_node_adds_one_budget_per_phase(tmp_path):
    rp = tmp_path / "t.js"
    rp.write_text(TAP_JS, encoding="utf-8")
    cmd = ["node", str(rp), str(WEB / "page.js"), str(WEB / "page_model.js"), str(WEB / "page_store.js"),
           str(WEB / "page_search.js"), str(WEB / "page_graph.js"), str(WEB / "page_graph_ext.js")]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout)
    assert got["bound"]
    assert got["sets"] == [["moreShown", {"big|P1": 400}],
                           ["moreShown", {"big|P1": 400, "other|X": 200}]]
