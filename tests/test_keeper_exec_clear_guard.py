"""Non-worker handbacks must free the exec owner for the next run."""
import ast
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from keeper_util import World, seed
from test_keeper_exec_ownership import owner
from test_keeper_exec_reopen import RUN1, RUN2, claimed_owner
from test_keeper_exec_verbs import park
from test_keeper_withdraw_review import _decisions


@pytest.mark.parametrize("transition", ["changes", "second_changes", "reopen",
                                         "withdraw_decision", "withdraw_non_code"])
def test_handback_clears_owner_and_accepts_new_run(tmp_path, transition):
    w = World(tmp_path)
    try:
        kind = "semaphore-task" if transition == "withdraw_non_code" else "decision"
        key, = seed(w, [("A", [])], evidence_kinds=(kind,))
        w.store.ctx = SimpleNamespace(decisions=_decisions(
            tmp_path, "| D-900 | redo | Closes: demo:A |\n"))
        epoch = claimed_owner(w, key)
        worker = w.p("workerA")
        w.store.add_evidence(worker, key, epoch, kind, {"id": "D-900"})
        w.store.submit(worker, key, epoch)
        if transition == "second_changes":
            w.store.verdict(w.p("reviewer"), key, False, "redo")
            # Give the second handback an owner of its own to clear.
            with w.store._write():
                w.store._exec_record(w.conn, key, *RUN1)
            w.store.submit(worker, key, epoch)
        assert owner(w, key) == RUN1
        if transition in ("changes", "second_changes"):
            w.store.verdict(w.p("reviewer"), key, False, "redo")
        elif transition == "reopen":
            w.store.reopen(w.p("router"), key, "redo", "D-900")
        else:
            w.store.withdraw_review(w.p("router"), key, "D-900")
        assert owner(w, key) == (None, None, None)
        assert w.store.worker_park(worker, park(key, **RUN2))["state"] == "parked"
        assert owner(w, key) == ("wf-2", "run-2", 1)
    finally:
        w.close()


# Explicit exceptions are function-scoped so moving line numbers is harmless.
# ``set_state``/``_apply_state`` is NOT allow-listed: since RT-P1-EXEC-OWNERSHIP
# every hand-back it writes (open/ready, changing, run-less) calls ``_exec_clear``.
STATE_WRITER_EXCEPTIONS = {
    ("store_legacy.py", "_set_legacy_state"): "Legacy set_state compatibility path, not a work handback.",
    ("store_verbs.py", "_claim"): "A worker may claim before running; claim must preserve ownership.",
    ("store_verbs.py", "_submit"): "Submits to review, not to new work.",
    ("store_verbs.py", "_cancel"): "Terminal cancellation is not new work.",
    ("store_review.py", "_pass"): "Passing review writes terminal done.",
    ("store_answer.py", "_answer"): "Answer closes a question as done.",
    ("store_close.py", "_close_on_decision"): "Decision closure writes terminal done.",
    ("store_rehome.py", "_rehome"): "Rehome cancels the old card; the new card has no owner.",
}


def test_items_state_writers_clear_owner_or_have_explicit_exception():
    root = Path(__file__).resolve().parents[1] / "src/plangraph/keeper"
    failures = []
    sites = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Constant, ast.JoinedStr)):
                continue
            if isinstance(node, ast.Constant):
                if not isinstance(node.value, str) or isinstance(parents.get(node), ast.JoinedStr):
                    continue
                sql = node.value
            else:
                sql = "".join(part.value if isinstance(part, ast.Constant) else "{dynamic}"
                              for part in node.values)
            for update in re.finditer(r"\bUPDATE\s+items\s+SET\s+(.*?)(?:\bWHERE\b|;|$)",
                                      sql, re.I | re.S):
                if not re.search(r"(?:^|,)\s*state\s*=", update[1], re.I):
                    continue
                sites += 1
                function = parents.get(node)
                while function is not None and not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function = parents.get(function)
                name = function.name if function else "<module>"
                clears = function is not None and any(
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_exec_clear" for call in ast.walk(function))
                if not clears and (path.name, name) not in STATE_WRITER_EXCEPTIONS:
                    failures.append(f"{path.relative_to(root)}:{node.lineno} ({name})")
    assert sites, "No items.state UPDATE sites found; scanner is broken"
    assert not failures, "State writers without _exec_clear or an explicit reason:\n" + "\n".join(failures)
