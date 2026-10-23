"""Non-worker handbacks must free the exec owner for the next run."""
import ast
from pathlib import Path
import re
import textwrap
from types import SimpleNamespace

import pytest

from keeper_util import World, seed
from test_keeper_exec_ownership import owner, refused, retired_runs
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
                w.store._exec_record(w.conn, key, *RUN1, principal=worker)
            w.store.submit(worker, key, epoch)
        assert owner(w, key) == RUN1
        if transition in ("changes", "second_changes"):
            w.store.verdict(w.p("reviewer"), key, False, "redo")
        elif transition == "reopen":
            w.store.reopen(w.p("router"), key, "redo", "D-900")
        else:
            w.store.withdraw_review(w.p("router"), key, "D-900")
        assert owner(w, key) == (None, None, None)
        # RT-P1 sq5 fix 2 (V1-1/V5-1): EVERY clear door keeps the retired-run
        # fence, so a late call from the run it retired is refused -- it cannot
        # be the first writer at the cleared card any more ...
        assert (RUN1[0], RUN1[1], RUN1[2]) in retired_runs(w, key)
        refused("exec_stale", w.store.worker_park, worker,
                park(key, workflow_id=RUN1[0], run_id=RUN1[1], attempt=RUN1[2]))
        assert owner(w, key) == (None, None, None)
        # ... while a NEW run still takes the card.
        assert w.store.worker_park(worker, park(key, **RUN2))["state"] == "parked"
        assert owner(w, key) == ("wf-2", "run-2", 1)
    finally:
        w.close()


# Explicit exceptions are function-scoped so moving line numbers is harmless.
# ``set_state``/``_apply_state`` is NOT allow-listed: since RT-P1-EXEC-OWNERSHIP
# every hand-back it writes (explicit ``hand_back=True``, open/ready, changing)
# calls ``_exec_clear``.
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


def _holds_clear(stmt):
    """True when *stmt*'s subtree calls ``_exec_clear`` (wherever it is nested)."""
    return any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
               and call.func.attr == "_exec_clear" for call in ast.walk(stmt))


def _block_containing(parent, child):
    """The statement list of *parent* that *child* sits in (None if there is none)."""
    for attr in ("body", "orelse", "finalbody"):
        block = getattr(parent, attr, None)
        if isinstance(block, list) and any(stmt is child for stmt in block):
            return block
    if isinstance(parent, ast.ExceptHandler) and any(stmt is child
                                                     for stmt in parent.body):
        return parent.body
    return None


def _clears_on_the_path(node, parents):
    """Is there an ``_exec_clear`` in a block that actually encloses this UPDATE?

    Checked per UPDATE site, not once per function: the walk starts at the
    statement holding the UPDATE and climbs only through the blocks that
    statement is written in (an ``if``/``with``/``try`` it sits inside), so
    ``_exec_clear`` tucked into a sibling branch of the same function -- or
    sitting outside the write block the UPDATE runs in -- no longer covers it.
    The function body counts only for an UPDATE written directly in it (the
    clear is then its sibling in the same block); a nested UPDATE must find the
    clear inside the same write block, never at function top level.
    """
    current = node
    site_block = True
    while current is not None:
        parent = parents.get(current)
        if parent is None:
            return False
        block = _block_containing(parent, current)
        at_function = isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
        if block is not None:
            # ``site_block`` (the statement's own block) always counts; an
            # enclosing one counts only while it is still inside the function,
            # i.e. never the function body of a nested UPDATE.
            if (site_block or not at_function) and any(_holds_clear(stmt)
                                                       for stmt in block):
                return True
            site_block = False
        if at_function:
            return False
        current = parent
    return False


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
                clears = _clears_on_the_path(node, parents)
                if not clears and (path.name, name) not in STATE_WRITER_EXCEPTIONS:
                    failures.append(f"{path.relative_to(root)}:{node.lineno} ({name})")
    assert sites, "No items.state UPDATE sites found; scanner is broken"
    assert not failures, "State writers without _exec_clear or an explicit reason:\n" + "\n".join(failures)


@pytest.mark.parametrize("source, cleared", [
    # A clear in the SAME write block as the UPDATE, wherever it is nested
    # inside that block, covers the site (this is ``_apply_state``'s shape) ...
    ("""
     def move(self, key):
         with self._write():
             if a:
                 self.conn.execute("UPDATE items SET state='open' WHERE key=?",
                                   (key,))
             if hand_back:
                 self._exec_clear(self.conn, key)
     """, True),
    # ... a top-level UPDATE finds its sibling clear in the same block ...
    ("""
     def move(self, key):
         self.conn.execute("UPDATE items SET state='open' WHERE key=?", (key,))
         if hand_back:
             self._exec_clear(self.conn, key)
     """, True),
    # ... but a clear OUTSIDE the write block, in a sibling branch of the
    # function, inside a nested helper, or in a branch the UPDATE never runs
    # in, does not cover that UPDATE site (the old function-level check said
    # yes to all three).
    ("""
     def move(self, key):
         with self._write():
             self.conn.execute("UPDATE items SET state='open' WHERE key=?",
                               (key,))
         if other:
             self._exec_clear(self.conn, key)
     """, False),
    ("""
     def move(self, key):
         with self._write():
             if a:
                 self.conn.execute("UPDATE items SET state='open' WHERE key=?",
                                   (key,))
         self._exec_clear(self.conn, key)
     """, False),
    ("""
     def move(self, key):
         with self._write():
             self.conn.execute("UPDATE items SET state='open' WHERE key=?",
                               (key,))
         def helper():
             self._exec_clear(self.conn, key)
     """, False),
])
def test_owner_clear_guard_checks_each_state_update_site(source, cleared):
    tree = ast.parse(textwrap.dedent(source))
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}
    site, = [node for node in ast.walk(tree)
             if isinstance(node, ast.Constant)
             and isinstance(node.value, str)
             and node.value.startswith("UPDATE items SET")]
    assert _clears_on_the_path(site, parents) is cleared
