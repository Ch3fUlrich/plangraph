"""Idempotent apply into the keeper, and the file source (docs/projector.md).

Runs on whichever backend ``PG_KEEPER_BACKEND`` selects (``keeper_util.World``), same code path.
"""
import json
import shutil
from pathlib import Path

import pytest

from plangraph.keeper.errors import Refused
from plangraph.projector import apply as papply
from plangraph.projector.apply import Projector, project_source
from plangraph.projector.models import AttemptRec, RunProjection, StateRec
from plangraph.projector.source import FileSource, WorkflowRef
from keeper_util import World, seed, to_claimed, to_in_review

FIXTURES = Path(__file__).parent / "fixtures" / "temporal"
KEY = "demo:A"
WF = "card:A"
T0 = "2026-10-15T10:00:00Z"
T1 = "2026-10-15T10:05:00Z"


def attempt(run="r1", act="5", att=1, sched=5, closed=8, key=KEY, exit_class="ok"):
    return AttemptRec(WF, run, act, att, sched, closed, key, exit_class, "h", "m", "h/m", "256k",
                      T0, T1, 10, 5, 0.5)


def state(event_id, st="running", run="r1", key=KEY, reason=None):
    return StateRec(WF, run, event_id, key, st, reason)


def proj(run="r1", attempts=(), states=(), key=KEY, last=20, status="running", wf=WF):
    return RunProjection(wf, run, key, status, T0, last, tuple(attempts), tuple(states))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    seed(w, [("A", []), ("B", [])])
    yield w
    w.close()


@pytest.fixture
def projector(world):
    return Projector(world.store, world.p("system"), now=world.clock)


def rows(world, table, order):
    return [dict(r) for r in world.conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def attempts(world, key=KEY):
    out = rows(world, "attempts", "n")
    return [{k: v for k, v in r.items() if k != "id"} for r in out if r["key"] == key]


def applied(world):
    return {tuple(r[c] for c in ("workflow_id", "run_id", "activity_id", "attempt", "kind", "key", "n"))
            for r in rows(world, "projector_applied", "workflow_id, run_id, activity_id, attempt, kind")}


def item_state(world, key=KEY):
    return world.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0]


def wm(world, workflow_id=WF):
    row = world.conn.execute("SELECT * FROM projector_watermark WHERE workflow_id=?",
                             (workflow_id,)).fetchone()
    return None if row is None else dict(row)


# ------------------------------------------------------------------- attempts
def test_attempt_written_once_and_reapply_is_a_noop(world, projector):
    p = proj(attempts=[attempt()], states=[state(3), state(9, "parked")])
    first = projector.apply_run(p, run_ord=0)
    assert (first.attempts_written, first.states_written) == (1, 2)
    rows_before, ledger = attempts(world), applied(world)
    assert len(rows_before) == 1 and rows_before[0]["n"] == 1
    row = rows_before[0]
    assert (row["workflow_id"], row["run_id"], row["log_ref"]) == (WF, "r1", "temporal:5:1")
    assert (row["harness"], row["model"], row["class"], row["exit_class"]) == ("h", "m", "256k", "ok")
    assert item_state(world) == "parked"
    events = world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    again = projector.apply_run(p, run_ord=0)
    assert again.as_dict() == {**dict.fromkeys(again.as_dict(), 0), "attempts_existing": 1,
                               "states_existing": 2}
    assert attempts(world) == rows_before and applied(world) == ledger
    assert world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events


def test_attempt_numbers_continue_across_two_runs_of_one_key(world, projector):
    projector.apply_run(proj("r1", [attempt("r1")]), run_ord=0)
    projector.apply_run(proj("r2", [attempt("r2"), attempt("r2", act="9", att=2, sched=9)]),
                        run_ord=1)
    assert [(a["n"], a["run_id"], a["log_ref"]) for a in attempts(world)] == [
        (1, "r1", "temporal:5:1"), (2, "r2", "temporal:5:1"), (3, "r2", "temporal:9:2")]
    assert {r[:5] for r in applied(world) if r[4] == "attempt"} == {
        (WF, "r1", "5", 1, "attempt"), (WF, "r2", "5", 1, "attempt"), (WF, "r2", "9", 2, "attempt")}


def test_missing_item_is_counted_and_not_recorded(world, projector):
    p = proj(key="demo:NOPE", attempts=[attempt(key="demo:NOPE")], states=[state(3, key="demo:NOPE")])
    res = projector.apply_run(p, run_ord=0)
    assert (res.missing_item, res.attempts_written, res.states_written) == (2, 0, 0)
    assert applied(world) == set() and wm(world) is None


def test_unmapped_run_is_counted(world, projector):
    p = RunProjection("misc-1", "r1", None, "completed", T0, 9, (), ())
    assert projector.apply_run(p, run_ord=0).unmapped == 1
    assert applied(world) == set() and wm(world, "misc-1") is None


# --------------------------------------------------------------------- states
def test_forward_only_older_event_after_newer_is_stale(world, projector):
    projector.apply_run(proj(states=[state(10, "waiting")], last=12), run_ord=0)
    assert item_state(world) == "waiting"
    res = projector.apply_run(proj(states=[state(6, "running")], last=12), run_ord=0)
    assert (res.states_stale, res.states_written) == (1, 0)
    assert item_state(world) == "waiting"
    assert projector.apply_run(proj(states=[state(6, "running")], last=12),
                               run_ord=0).states_existing == 1          # stale is recorded too


def test_older_run_does_not_overwrite_a_failed_state_of_a_newer_run(world, projector):
    projector.apply_run(proj("r2", states=[state(3, "running", "r2"), state(8, "failed", "r2")]),
                        run_ord=1)
    assert item_state(world) == "failed"
    res = projector.apply_run(proj("r1", states=[state(30, "running", "r1")], last=31), run_ord=0)
    assert (res.states_stale, res.states_written) == (1, 0)
    assert item_state(world) == "failed"
    assert wm(world)["run_id"] == "r2" and wm(world)["run_ord"] == 1       # watermark never regresses


def test_newer_run_may_restart_a_parked_item(world, projector):
    projector.apply_run(proj("r1", states=[state(8, "parked")]), run_ord=0)
    res = projector.apply_run(proj("r2", states=[state(3, "running", "r2")]), run_ord=1)
    assert res.states_written == 1 and item_state(world) == "running"


@pytest.mark.parametrize("to", [to_claimed, to_in_review])
def test_items_the_keeper_owns_are_never_overwritten(world, projector, to):
    to(world, KEY)
    held = item_state(world)
    res = projector.apply_run(proj(states=[state(3, "running")]), run_ord=0)
    assert (res.states_skipped_state, res.states_written) == (1, 0)
    assert item_state(world) == held
    assert projector.apply_run(proj(states=[state(3, "running")]), run_ord=0).states_existing == 1


def test_done_item_is_not_overwritten(world, projector):
    from keeper_util import pass_review
    to_in_review(world, KEY)
    pass_review(world, KEY)
    assert item_state(world) == "done"
    res = projector.apply_run(proj(states=[state(3, "failed")]), run_ord=0)
    assert res.states_skipped_state == 1 and item_state(world) == "done"


def test_state_equal_to_current_is_not_a_write(world, projector):
    world.store.set_state(world.p("system"), KEY, "running", "")
    res = projector.apply_run(proj(states=[state(3, "running")]), run_ord=0)
    assert (res.states_written, res.states_existing) == (0, 1)


# ---------------------------------------------------------------- crash window
def test_crash_window_attempt_row_without_applied_row(world, projector):
    p = proj(attempts=[attempt()])
    projector.apply_run(p, run_ord=0)
    world.conn.execute("DELETE FROM projector_applied")             # the ledger write was lost
    res = projector.apply_run(p, run_ord=0)
    assert (res.attempts_written, res.attempts_existing) == (0, 1)
    assert len(attempts(world)) == 1
    assert applied(world) == {(WF, "r1", "5", 1, "attempt", KEY, 1)}


def test_crash_window_via_explicit_conflict(world, projector, monkeypatch):
    # a racing writer lands the same attempt between the projector's MAX(n)+1 and its write
    original = world.store.record_attempt
    raced = []

    def racing(principal, key, body):
        if not raced:
            raced.append(1)
            original(principal, key, body)
        return original(principal, key, body)

    monkeypatch.setattr(world.store, "record_attempt", racing)
    res = projector.apply_run(proj(attempts=[attempt()]), run_ord=0)
    assert (res.attempts_written, res.attempts_existing) == (0, 1)
    assert len(attempts(world)) == 1
    assert applied(world) == {(WF, "r1", "5", 1, "attempt", KEY, 1)}


def test_conflict_with_a_foreign_row_is_reraised(world, projector, monkeypatch):
    world.store.record_attempt(world.p("router"), KEY, {"exit_class": "ok", "n": 1, "log_ref": "x"})

    def conflict(principal, key, body):
        raise Refused("conflict", "taken")

    monkeypatch.setattr(world.store, "record_attempt", conflict)
    monkeypatch.setattr(projector, "_attempt_row_identity", lambda key, n: (WF, "r1", "other"))
    with pytest.raises(Refused):
        projector.apply_run(proj(attempts=[attempt()]), run_ord=0)
    assert applied(world) == set()


# ------------------------------------------------------------------ watermarks
def test_watermark_row_per_workflow_after_a_run(world, projector):
    assert wm(world) is None
    projector.apply_run(proj("r1", states=[state(3), state(25, "parked")], last=26), run_ord=0)
    row = wm(world)
    assert (row["run_id"], row["run_ord"], row["event_id"], row["status"], row["plan_key"]) == (
        "r1", 0, 26, "running", KEY)
    assert row["updated_at"] == world.store._iso(world.clock())
    projector.apply_run(proj("r2", states=[state(3, "running", "r2")], last=9), run_ord=1)
    row = wm(world)
    assert (row["run_id"], row["run_ord"], row["event_id"]) == ("r2", 1, 9)


# ----------------------------------------------------------------- project_source
class FakeSource:
    """Refs plus projections by (workflow_id, run_id); ``fetch_history`` returns the key itself."""

    def __init__(self, projections, status="COMPLETED"):
        self.projections = {(p.workflow_id, p.run_id): p for p in projections}
        self.status = status
        self.listed = []

    def list_workflows(self, *, open_only=False):
        self.listed.append(open_only)
        refs = [WorkflowRef(p.workflow_id, p.run_id, p.start_time, self.status.get(p.run_id, "COMPLETED")
                            if isinstance(self.status, dict) else self.status)
                for p in self.projections.values()]
        return sorted(refs, key=lambda r: (r.start_time, r.workflow_id, r.run_id))

    def fetch_history(self, workflow_id, run_id):
        return {"wf": workflow_id, "run": run_id}


@pytest.fixture
def fake_projection(monkeypatch):
    def run(source):
        def fake(history, *, workflow_id, run_id):
            assert history == {"wf": workflow_id, "run": run_id}
            return source.projections[(workflow_id, run_id)]
        monkeypatch.setattr(papply, "project_history", fake, raising=False)
        return source
    return run


def two_run_source():
    p1 = RunProjection(WF, "r1", KEY, "continued_as_new", "2026-10-15T10:00:00Z", 12,
                       (attempt("r1"),), (state(3, "running", "r1"), state(9, "waiting", "r1")))
    p2 = RunProjection(WF, "r2", KEY, "completed", "2026-10-15T10:10:00Z", 14,
                       (attempt("r2"),), (state(4, "running", "r2"), state(12, "parked", "r2")))
    pb = RunProjection("card:B", "rb", "demo:B", "failed", "2026-10-15T10:02:00Z", 6,
                       (), (StateRec("card:B", "rb", 5, "demo:B", "failed", "boom"),))
    unm = RunProjection("misc", "rm", None, "completed", "2026-10-15T10:03:00Z", 4, (), ())
    return [p1, p2, pb, unm]


def test_project_source_counters_and_global_watermark(world, projector, fake_projection):
    source = fake_projection(FakeSource(two_run_source()))
    world.clock.advance(100)
    out = project_source(source, projector)
    assert out["processed"] == 4 and out["unmapped"] == 1
    assert (out["attempts_written"], out["states_written"]) == (2, 5)
    assert item_state(world) == "parked" and item_state(world, "demo:B") == "failed"
    assert wm(world)["run_id"] == "r2" and wm(world)["run_ord"] == 1
    glob = wm(world, "*")
    assert glob["updated_at"] == world.store._iso(world.clock())
    world.clock.advance(60)
    again = project_source(source, projector)
    assert again["attempts_written"] == again["states_written"] == 0
    assert (again["attempts_existing"], again["states_existing"]) == (2, 5)
    assert wm(world, "*")["updated_at"] == world.store._iso(world.clock())


def test_project_source_open_only_keeps_run_ordinals(world, projector, fake_projection):
    projs = two_run_source()
    source = fake_projection(FakeSource(projs, {"r1": "CONTINUED_AS_NEW", "r2": "RUNNING"}))
    out = project_source(source, projector, open_only=True)
    assert out["processed"] == 1
    assert wm(world)["run_id"] == "r2" and wm(world)["run_ord"] == 1       # ordinal of the full list


def test_rebuild_twice_and_on_a_fresh_keeper_gives_identical_rows(world, projector, fake_projection,
                                                                  tmp_path):
    source = fake_projection(FakeSource(two_run_source()))
    project_source(source, projector)
    before = (attempts(world), applied(world), attempts(world, "demo:B"))
    project_source(source, projector)                        # rebuild on an existing keeper
    assert (attempts(world), applied(world), attempts(world, "demo:B")) == before

    other = World(tmp_path / "fresh")
    try:
        seed(other, [("A", []), ("B", [])])
        project_source(source, Projector(other.store, other.p("system"), now=other.clock))
        project_source(source, Projector(other.store, other.p("system"), now=other.clock))
        assert (attempts(other), applied(other), attempts(other, "demo:B")) == before
        assert item_state(other) == item_state(world) == "parked"
    finally:
        other.close()


def test_project_source_without_history_module_is_a_clear_error(world, projector, monkeypatch):
    monkeypatch.setattr(papply, "project_history", None, raising=False)
    with pytest.raises(RuntimeError):
        project_source(FakeSource([]), projector)


# ------------------------------------------------------------------- FileSource
def test_filesource_lists_recorded_fixtures_sorted_with_status():
    refs = FileSource(FIXTURES).list_workflows()
    assert len(refs) == 14
    assert refs == sorted(refs, key=lambda r: (r.start_time, r.workflow_id, r.run_id))
    by = {(r.workflow_id, r.status) for r in refs}
    assert ("card:DEMO-1", "COMPLETED") in by and ("card:DEMO-8", "CANCELED") in by
    assert ("card:DEMO-9", "CONTINUED_AS_NEW") in by and ("card:DEMO-10", "FAILED") in by
    assert ("card:DEMO-12", "TERMINATED") in by
    assert refs[0].start_time == "2026-10-15T20:00:02.509370397Z"
    assert FileSource(FIXTURES).list_workflows(open_only=True) == []


def test_filesource_fetch_history_and_open_runs(tmp_path):
    for name in ("01_ok_single.json", "09_continue_as_new_run1.json"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    doc = json.loads((FIXTURES / "01_ok_single.json").read_text())
    events = doc["history"]["events"][:-3]                    # cut before the end: still open
    (tmp_path / "open.json").write_text(json.dumps(
        {"workflow_id": "card:OPEN", "run_id": "ro", "history": {"events": events}}))
    src = FileSource(tmp_path)
    assert [r.workflow_id for r in src.list_workflows(open_only=True)] == ["card:OPEN"]
    assert src.list_workflows(open_only=True)[0].status == "RUNNING"
    assert src.fetch_history(doc["workflow_id"], doc["run_id"]) == doc["history"]
    with pytest.raises(KeyError):
        src.fetch_history("card:none", "x")
