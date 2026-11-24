"""PRIORITY-INHERIT-FROM-BLOCKED / ITEM-CATEGORY-RANKED in the web table model:
the priority column sorts by the effective value, the row carries both."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_table_util import FIXTURE, RUNNER_BOOT, need_node, run_node  # noqa: E402

RUNNER = RUNNER_BOOT + r"""
if (mode === "rows") {
  var vm = vmOf(IN.snapshot);
  var rec = vm.items.get(IN.key);
  emit({ priority: rec.priority, effective: rec.effective_priority, via: rec.priority_via });
} else if (mode === "sort") {
  var rows = IN.rows, ctx = { rows: rows, projIdx: {}, rowIdx: new Map() };
  rows.forEach(function (r, i) { ctx.rowIdx.set(r, i); });
  var out = PlanUI.tableSort(rows, { sort: IN.sort }, ctx);
  emit(out.map(function (r) { return r.key; }));
} else if (mode === "reorder") {
  var order = ["speed", "efficiency-tokens", "cleanup"];
  var moved = PlanUI.moveCategory(order, "cleanup", -2);
  var ranks = IN.rows.map(function (r) { return PlanUI.categoryRank(r); });
  PlanUI.categoryOrder = moved;
  emit({ moved: moved, clamped: PlanUI.moveCategory(order, "speed", -5), unknown: PlanUI.moveCategory(order, "zzz", 1),
         before: ranks, after: IN.rows.map(function (r) { return PlanUI.categoryRank(r); }) });
} else if (mode === "defaults") {
  var rows = IN.rows, ctx = { rows: rows, projIdx: {}, rowIdx: new Map() };
  rows.forEach(function (r, i) { ctx.rowIdx.set(r, i); });
  var st = PlanUI.tableDefaults();
  var sorted = PlanUI.tableSort(rows, st, ctx).map(function (r) { return r.key; });
  st.filters.category = ["cleanup"];
  var kept = PlanUI.tableFilter(PlanUI.tableContext({ items: rows, projects: [] }), st).map(function (r) { return r.key; });
  emit({ sorted: sorted, kept: kept, sort: st.sort, cats: PlanUI.tableContext({ items: rows }).categories });
}
"""


@need_node
def test_the_record_carries_both_priorities_and_the_source(tmp_path):
    snap = json.loads(Path(FIXTURE).read_text(encoding="utf-8"))
    item = snap["items"][0]
    item.update(priority=8, effective_priority=10, priority_via="demo:TOP")
    other = snap["items"][1]
    other["priority"] = 3
    other.pop("effective_priority", None)
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    got = run_node(tmp_path, RUNNER, "rows", {"snapshot": str(path), "key": item["key"]})
    assert got == {"priority": 8, "effective": 10, "via": "demo:TOP"}
    got = run_node(tmp_path, RUNNER, "rows", {"snapshot": str(path), "key": other["key"]})
    assert got == {"priority": 3, "effective": 3, "via": None}


@need_node
def test_the_priority_column_sorts_by_the_effective_value(tmp_path):
    rows = [{"key": "a", "priority": 9, "effective_priority": 9},
            {"key": "b", "priority": 8, "effective_priority": 10, "priority_via": "x"},
            {"key": "c", "priority": 5}]
    got = run_node(tmp_path, RUNNER, "sort",
                   {"rows": rows, "sort": [{"col": "priority", "dir": "desc"}]})
    assert got == ["b", "a", "c"]


@need_node
def test_default_sort_is_category_rank_before_priority_and_filters_by_category(tmp_path):
    rows = [{"key": "eff", "priority": 10, "category": "efficiency-tokens", "category_rank": 1},
            {"key": "none", "priority": 12},
            {"key": "speed", "priority": 8, "category": "speed", "category_rank": 0},
            {"key": "clean", "priority": 1, "category": "cleanup", "category_rank": 8}]
    got = run_node(tmp_path, RUNNER, "defaults", {"rows": rows})
    assert got["sorted"] == ["speed", "eff", "clean", "none"]
    assert got["sort"] == [{"col": "category", "dir": "asc"}, {"col": "priority", "dir": "desc"}]
    assert got["kept"] == ["clean"]
    assert got["cats"] == ["cleanup", "efficiency-tokens", "speed"]


@need_node
def test_the_record_carries_the_category_and_its_rank(tmp_path):
    snap = json.loads(Path(FIXTURE).read_text(encoding="utf-8"))
    snap["items"][0].update(category="speed", category_rank=0)
    snap["categories"] = [{"name": "speed", "rank": 0}]
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    runner = RUNNER_BOOT + """
var vm = vmOf(IN.snapshot), rec = vm.items.get(IN.key);
emit({ category: rec.category, rank: rec.category_rank, list: vm.categories });
"""
    got = run_node(tmp_path, runner, "x", {"snapshot": str(path), "key": snap["items"][0]["key"]})
    assert got == {"category": "speed", "rank": 0, "list": [{"name": "speed", "rank": 0}]}


@need_node
def test_equal_rank_and_priority_rows_sort_oldest_first_in_the_default_order(tmp_path):
    rows = [{"key": "new", "priority": 5, "category": "speed", "category_rank": 0, "createdAt": "2026-06-01T00:00:00Z"},
            {"key": "undated", "priority": 5, "category": "speed", "category_rank": 0},
            {"key": "old", "priority": 5, "category": "speed", "category_rank": 0, "createdAt": "2026-01-01T00:00:00Z"}]
    got = run_node(tmp_path, RUNNER, "defaults", {"rows": rows})
    assert got["sorted"] == ["old", "new", "undated"]


@need_node
def test_the_reorder_control_moves_a_category_and_the_rank_follows_the_working_order(tmp_path):
    rows = [{"key": "a", "category": "speed", "category_rank": 0},
            {"key": "b", "category": "cleanup", "category_rank": 2},
            {"key": "c"}]
    got = run_node(tmp_path, RUNNER, "reorder", {"rows": rows})
    assert got["moved"] == ["cleanup", "speed", "efficiency-tokens"]
    assert got["clamped"] == ["speed", "efficiency-tokens", "cleanup"] and got["unknown"] == got["clamped"]
    assert got["before"] == [0, 2, 9999] and got["after"] == [1, 0, 9999]
