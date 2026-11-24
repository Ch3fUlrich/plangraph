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
