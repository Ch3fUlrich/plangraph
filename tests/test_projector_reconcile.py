"""Reconciler: alarm kinds, dedupe, auto-clear (docs/projector.md, "Source, CLI, reconciler").

Open workflows are recorded fixtures with their terminal events cut off.  Runs on whichever
backend ``PG_KEEPER_BACKEND`` selects (``keeper_util.World``), same code path.
"""
import base64
import copy
import json
from pathlib import Path

import pytest

from plangraph.projector.apply import Projector, project_source
from plangraph.projector.reconcile import ReconcileReport, open_alarms, reconcile
from plangraph.projector.source import FileSource
from keeper_util import World, seed

FIXTURES = Path(__file__).parent / "fixtures" / "temporal"
OPEN_03 = ("03_usage_limit_wait_then_ok", 7)       # after the usage_limit attempt: waiting
OPEN_04 = ("04_needs_input_signal", 7)             # after the needs_input attempt
OPEN_01 = ("01_ok_single", 5)                      # activity scheduled: running
OPEN_11 = ("11_unmapped", 5)                       # no plan key


def _payload(value):
    enc = base64.b64encode(b"json/plain").decode()
    return {"metadata": {"encoding": enc},
            "data": base64.b64encode(json.dumps(value).encode()).decode()}


def _history(name, cut, key):
    doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    history = copy.deepcopy(doc["history"])
    history["events"] = history["events"][:cut]
    memo = history["events"][0]["workflowExecutionStartedEventAttributes"].get("memo")
    if memo and key:
        memo["fields"]["plan_key"] = _payload(key)      # fixtures carry bare DEMO-n keys
    return {"workflow_id": doc["workflow_id"], "run_id": doc["run_id"], "history": history}


def put(directory, name, cut, key=None):
    """Write fixture ``name`` cut to ``cut`` events; its plan key becomes ``demo:<DEMO-n>``."""
    stem = name.split("_", 1)[0]
    doc = _history(name, cut, key if key is not None else f"demo:DEMO-{int(stem)}")
    (directory / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    seed(w, [(f"DEMO-{i}", []) for i in range(1, 6)])
    yield w
    w.close()


@pytest.fixture
def src_dir(tmp_path):
    path = tmp_path / "src"
    path.mkdir()
    return path


@pytest.fixture
def projector(world):
    return Projector(world.store, world.p("system"), now=world.clock)


def run(world, src_dir, **kw):
    return reconcile(world.store, FileSource(src_dir), now=world.clock, **kw)


def kinds(report):
    return sorted((a["kind"], a["workflow_id"], a["key"]) for a in report.open_alarms)


def all_rows(world):
    return [dict(r) for r in world.conn.execute("SELECT * FROM projector_alarms ORDER BY id")]


def state(world, key):
    return world.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0]


def project(world, src_dir, projector):
    project_source(FileSource(src_dir), projector)


# ------------------------------------------------------------------- in sync
def test_no_alarm_when_projected_and_in_sync(world, src_dir, projector):
    for fx in (OPEN_01, OPEN_03, OPEN_04):
        put(src_dir, *fx)
    project(world, src_dir, projector)
    assert [state(world, f"demo:DEMO-{i}") for i in (1, 3, 4)] == ["running", "waiting", "needs_input"]
    report = run(world, src_dir)
    assert isinstance(report, ReconcileReport)
    assert report.raised == report.cleared == report.open_alarms == [] and report.counts == {}
    assert all_rows(world) == []


def test_no_open_workflows_and_calm_keeper_is_quiet(world, src_dir):
    assert run(world, src_dir).open_alarms == []


# ---------------------------------------------------------------- alarm kinds
def test_unmapped_workflow(world, src_dir, projector):
    put(src_dir, *OPEN_11, key="")
    project(world, src_dir, projector)
    report = run(world, src_dir)
    assert kinds(report) == [("unmapped_workflow", "misc-1", "")]
    assert report.counts == {"unmapped_workflow": 1}


def test_missing_item(world, src_dir, projector):
    put(src_dir, *OPEN_01, key="demo:NOPE")
    project(world, src_dir, projector)
    assert kinds(run(world, src_dir)) == [("missing_item", "card:DEMO-1", "demo:NOPE")]


def test_state_drift_when_keeper_diverges_after_projection(world, src_dir, projector):
    put(src_dir, *OPEN_03)
    project(world, src_dir, projector)
    world.store.set_state(world.p("system"), "demo:DEMO-3", "running", "operator")
    report = run(world, src_dir)
    assert kinds(report) == [("state_drift", "card:DEMO-3", "demo:DEMO-3")]
    assert "waiting" in report.open_alarms[0]["detail"] and "running" in report.open_alarms[0]["detail"]


def test_state_drift_when_keeper_owns_the_item_while_workflow_is_open(world, src_dir, projector):
    from keeper_util import to_claimed
    put(src_dir, *OPEN_01)
    project(world, src_dir, projector)
    world.store.set_state(world.p("system"), "demo:DEMO-1", "open", "")
    to_claimed(world, "demo:DEMO-1")
    assert state(world, "demo:DEMO-1") == "claimed"
    assert kinds(run(world, src_dir)) == [("state_drift", "card:DEMO-1", "demo:DEMO-1")]


def test_orphan_running_after_grace(world, src_dir, projector):
    put(src_dir, *OPEN_01)
    project(world, src_dir, projector)
    world.store.set_state(world.p("system"), "demo:DEMO-2", "needs_input", "")   # no workflow
    assert run(world, src_dir).open_alarms == []                                  # inside grace
    world.clock.advance(901)
    project(world, src_dir, projector)                                            # keep the watermark fresh
    report = run(world, src_dir)
    assert kinds(report) == [("orphan_running", "", "demo:DEMO-2")]
    assert "needs_input" in report.open_alarms[0]["detail"]


def test_orphan_grace_is_a_parameter(world, src_dir):
    world.store.set_state(world.p("system"), "demo:DEMO-2", "running", "")
    world.clock.advance(61)
    assert run(world, src_dir).open_alarms == []
    assert kinds(run(world, src_dir, grace_s=60)) == [("orphan_running", "", "demo:DEMO-2")]


def test_item_with_an_open_workflow_is_not_an_orphan(world, src_dir, projector):
    put(src_dir, *OPEN_03)
    project(world, src_dir, projector)
    world.clock.advance(1000)
    project(world, src_dir, projector)
    assert run(world, src_dir).open_alarms == []


def test_watermark_stale_and_missing(world, src_dir, projector):
    put(src_dir, *OPEN_01)
    assert kinds(run(world, src_dir)) == [("watermark_stale", "", "")]            # never projected
    project(world, src_dir, projector)
    assert run(world, src_dir).open_alarms == []                                  # cleared
    world.clock.advance(1801)
    report = run(world, src_dir)
    assert kinds(report) == [("watermark_stale", "", "")] and "1801" in report.open_alarms[0]["detail"]
    assert run(world, src_dir, stale_s=3600).open_alarms == []


def test_watermark_stale_needs_open_workflows(world, src_dir, projector):
    project(world, src_dir, projector)
    world.clock.advance(5000)
    assert run(world, src_dir).open_alarms == []


# ----------------------------------------------------- watermark behind history
def test_watermark_behind_the_history_suppresses_state_drift(world, src_dir, projector):
    put(src_dir, *OPEN_03[:1], 5)                         # projected only up to the activity schedule
    project(world, src_dir, projector)
    assert state(world, "demo:DEMO-3") == "running"
    put(src_dir, *OPEN_03)                                # the workflow moved on: now waiting
    report = run(world, src_dir)
    assert report.open_alarms == []                       # not drift: the projector is just behind
    project(world, src_dir, projector)
    assert state(world, "demo:DEMO-3") == "waiting" and run(world, src_dir).open_alarms == []


def test_unprojected_workflow_has_no_drift_only_a_stale_watermark(world, src_dir):
    put(src_dir, *OPEN_03)
    assert kinds(run(world, src_dir)) == [("watermark_stale", "", "")]


# ------------------------------------------------------------- dedupe and clear
def test_second_pass_does_not_duplicate_an_open_alarm(world, src_dir, projector):
    put(src_dir, *OPEN_03)
    project(world, src_dir, projector)
    world.store.set_state(world.p("system"), "demo:DEMO-3", "running", "")
    first = run(world, src_dir)
    second = run(world, src_dir)
    assert len(first.raised) == 1 and second.raised == [] and second.cleared == []
    assert len(all_rows(world)) == 1 and len(second.open_alarms) == 1
    assert second.open_alarms[0]["id"] == first.open_alarms[0]["id"]


def test_alarm_clears_when_the_condition_is_gone_and_can_reraise(world, src_dir, projector):
    put(src_dir, *OPEN_03)
    project(world, src_dir, projector)
    world.store.set_state(world.p("system"), "demo:DEMO-3", "running", "")
    run(world, src_dir)
    world.clock.advance(10)
    world.store.set_state(world.p("system"), "demo:DEMO-3", "waiting", "")
    report = run(world, src_dir)
    assert report.open_alarms == [] and [c["kind"] for c in report.cleared] == ["state_drift"]
    rows = all_rows(world)
    assert len(rows) == 1 and rows[0]["cleared_at"] == world.store._iso(world.clock())
    world.store.set_state(world.p("system"), "demo:DEMO-3", "running", "")
    again = run(world, src_dir)
    assert len(again.raised) == 1 and len(all_rows(world)) == 2        # a new row; old stays cleared


def test_orphan_clears_when_the_item_leaves_the_live_states(world, src_dir):
    world.store.set_state(world.p("system"), "demo:DEMO-2", "running", "")
    world.clock.advance(2000)
    assert kinds(run(world, src_dir)) == [("orphan_running", "", "demo:DEMO-2")]
    world.store.set_state(world.p("system"), "demo:DEMO-2", "parked", "")
    report = run(world, src_dir)
    assert report.open_alarms == [] and len(report.cleared) == 1


def test_unmapped_clears_when_the_workflow_closes(world, src_dir, projector):
    put(src_dir, *OPEN_11, key="")
    assert kinds(run(world, src_dir))[0][0] == "unmapped_workflow"
    put(src_dir, "11_unmapped", 11, key="")               # closed: full history
    report = run(world, src_dir)
    assert sorted(c["kind"] for c in report.cleared) == ["unmapped_workflow", "watermark_stale"]


def test_several_kinds_are_counted_and_listed_by_open_alarms(world, src_dir, projector):
    put(src_dir, *OPEN_11, key="")
    put(src_dir, *OPEN_01, key="demo:NOPE")
    world.store.set_state(world.p("system"), "demo:DEMO-2", "running", "")
    world.clock.advance(2000)
    report = run(world, src_dir)
    assert report.counts == {"unmapped_workflow": 1, "missing_item": 1, "orphan_running": 1,
                             "watermark_stale": 1}
    assert open_alarms(world.store) == report.open_alarms
    assert set(report.open_alarms[0]) == {"id", "kind", "workflow_id", "key", "detail", "raised_at"}


def test_reconcile_changes_no_keeper_state(world, src_dir, projector):
    put(src_dir, *OPEN_03)
    project(world, src_dir, projector)
    world.store.set_state(world.p("system"), "demo:DEMO-3", "running", "")
    world.clock.advance(5000)
    events = world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    items = [tuple(r) for r in world.conn.execute("SELECT key, state, updated_at FROM items ORDER BY key")]
    run(world, src_dir)
    assert world.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
    assert [tuple(r) for r in world.conn.execute(
        "SELECT key, state, updated_at FROM items ORDER BY key")] == items


def test_projector_argument_supplies_the_clock(world, src_dir, projector):
    world.store.set_state(world.p("system"), "demo:DEMO-2", "running", "")
    world.clock.advance(2000)
    report = reconcile(world.store, FileSource(src_dir), projector)
    assert kinds(report) == [("orphan_running", "", "demo:DEMO-2")]
