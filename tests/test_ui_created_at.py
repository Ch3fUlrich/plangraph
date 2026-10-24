"""UI-CREATED-AT: the table's created column shows the snapshot `created_at`
(keeper item_created ts), falling back to the earliest evidence date, else "".
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ui_table_util import FIXTURE, RUNNER_BOOT, need_node, run_node  # noqa: E402

RUNNER = RUNNER_BOOT + r"""
var snap = JSON.parse(fs.readFileSync(IN.snap, "utf8"));
var vm = PlanUI.buildViewModel(snap);
var ctx = PlanUI.tableContext(vm);
var out = {};
IN.keys.forEach(function (k) {
  out[k] = { model: vm.items.get(k).createdAt, row: ctx.byKey[k].createdAt };
});
emit(out);
"""


@need_node
def test_created_at_precedence(tmp_path):
    snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items = snap["items"][:3]
    for it in items:
        it.pop("created_at", None)
    a, b, c = items
    a["created_at"] = "2026-09-01T00:00:00Z"        # wins over evidence
    a["evidence"] = [{"kind": "run-record", "ref": "x", "at": "2026-10-01T00:00:00Z"}]
    b["evidence"] = [{"kind": "run-record", "ref": "x", "at": "2026-10-03T00:00:00Z"},
                     {"kind": "run-record", "ref": "y", "at": "2026-10-02T00:00:00Z"}]
    c["evidence"] = []                               # neither -> ""
    sp = tmp_path / "snap.json"
    sp.write_text(json.dumps(copy.deepcopy(snap)), encoding="utf-8")
    out = run_node(tmp_path, RUNNER, "x", {"snap": str(sp), "keys": [a["key"], b["key"], c["key"]]})
    assert out[a["key"]] == {"model": "2026-09-01T00:00:00Z", "row": "2026-09-01T00:00:00Z"}
    assert out[b["key"]] == {"model": "2026-10-02T00:00:00Z", "row": "2026-10-02T00:00:00Z"}
    assert out[c["key"]] == {"model": "", "row": ""}
