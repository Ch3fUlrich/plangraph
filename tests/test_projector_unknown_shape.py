"""Unknown-shape alarm (RT-P1-PROJECTOR gap P10): a history shape outside contract v1 raises one
``unknown_shape`` alarm per (workflow, run, activity); every recorded fixture raises none."""
import copy
import json
from pathlib import Path

import pytest

from keeper_util import World
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from plangraph.projector.reconcile import reconcile
from plangraph.projector.source import FileSource

ROOT = Path(__file__).parents[1]
FIXTURES = sorted((ROOT / "rust/worker/tests/fixtures/projector").glob("*.json")) + \
    sorted((ROOT / "tests/fixtures/temporal").glob("*.json"))


def alarms(world):
    return [dict(r) for r in world.conn.execute(
        "SELECT * FROM projector_alarms WHERE kind='unknown_shape' ORDER BY id")]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        w.close()


@pytest.fixture
def projector(world):
    return Projector(world.store, world.p("system"), now=world.clock)


def load(path):
    data = json.loads(path.read_text())
    return data.get("history", data)


def test_fixture_set_is_not_empty():
    assert len(FIXTURES) >= 18


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_known_fixtures_raise_no_alarm(path, world, projector):
    proj = project_history(load(path), workflow_id="card:demo:A", run_id="r1")
    assert proj.shape_issues == ()
    projector.apply_run(proj, run_ord=0)
    assert alarms(world) == []


def events(history, etype):
    return [e for e in history["events"] if e["eventType"] == f"EVENT_TYPE_{etype}"]


def attempt_history():
    return load(ROOT / "rust/worker/tests/fixtures/projector/happy.json")


def test_unknown_activity_type_raises_once(world, projector):
    h = attempt_history()
    events(h, "ACTIVITY_TASK_SCHEDULED")[0]["activityTaskScheduledEventAttributes"][
        "activityType"]["name"] = "mystery"
    proj = project_history(h, workflow_id="card:demo:A", run_id="r1")
    assert [i.detail for i in proj.shape_issues] == ["unknown activity type 'mystery'"]
    projector.apply_run(proj, run_ord=0)
    projector.apply_run(proj, run_ord=0)                       # idempotent
    rows = alarms(world)
    assert len(rows) == 1 and rows[0]["workflow_id"] == "card:demo:A"
    assert rows[0]["key"].startswith("r1:") and "mystery" in rows[0]["detail"]


def attempt_completed(h):
    """The ActivityTaskCompleted attributes of the ``attempt`` activity."""
    sched = next(e["eventId"] for e in events(h, "ACTIVITY_TASK_SCHEDULED")
                 if e["activityTaskScheduledEventAttributes"]["activityType"]["name"] == "attempt")
    return next(e["activityTaskCompletedEventAttributes"] for e in events(h, "ACTIVITY_TASK_COMPLETED")
                if e["activityTaskCompletedEventAttributes"]["scheduledEventId"] == sched)


def test_missing_exit_class_raises(world, projector):
    h = attempt_history()
    attempt_completed(h)["result"] = {}                        # void result: no exit_class at all
    proj = project_history(h, workflow_id="card:demo:A", run_id="r1")
    assert [i.detail for i in proj.shape_issues] == ["attempt result lacks exit_class"]
    projector.apply_run(proj, run_ord=0)
    assert len(alarms(world)) == 1


def test_unparsable_payload_raises(world, projector):
    h = attempt_history()
    attempt_completed(h)["result"]["payloads"][0]["data"] = "!!notb64"
    proj = project_history(h, workflow_id="card:demo:A", run_id="r1")
    assert [i.detail for i in proj.shape_issues] == ["unparsable attempt result"]
    projector.apply_run(proj, run_ord=0)
    assert len(alarms(world)) == 1


def test_alarm_is_per_run_and_activity_and_survives_reconcile(world, projector, tmp_path):
    h = attempt_history()
    events(h, "ACTIVITY_TASK_SCHEDULED")[0]["activityTaskScheduledEventAttributes"][
        "activityType"]["name"] = "mystery"
    for run in ("r1", "r2"):
        projector.apply_run(project_history(copy.deepcopy(h), workflow_id="card:demo:A",
                                            run_id=run), run_ord=0)
    assert len(alarms(world)) == 2
    (tmp_path / "empty").mkdir()
    reconcile(world.store, FileSource(tmp_path / "empty"), projector)
    assert len(alarms(world)) == 2                             # a reconcile pass never clears them


def test_every_worker_activity_is_a_known_type():
    """Drift guard: each ``#[activity]`` fn of the Rust worker (the question ones too) is known."""
    import re
    from plangraph.projector.models import KNOWN_ACTIVITY_TYPES
    src = (ROOT / "rust/worker/src/activities.rs").read_text()
    names = re.findall(r"#\[activity\]\s+pub async fn (\w+)", src)
    assert {"ask_question", "record_question_timeout", "janitor"} <= set(names), names
    for name in names:
        assert name == "attempt" or f"CardActivities::{name}" in KNOWN_ACTIVITY_TYPES, name


def test_rebuild_from_history_yields_identical_alarm_rows_twice(world, projector):
    h = attempt_history()
    events(h, "ACTIVITY_TASK_SCHEDULED")[0]["activityTaskScheduledEventAttributes"][
        "activityType"]["name"] = "mystery"
    projector.apply_run(project_history(copy.deepcopy(h), workflow_id="card:demo:A", run_id="r1"),
                        run_ord=0)
    first = alarms(world)
    projector.apply_run(project_history(copy.deepcopy(h), workflow_id="card:demo:A", run_id="r1"),
                        run_ord=0)
    assert len(first) == 1 and alarms(world) == first


def with_mystery(h, wf_type=None):
    h = copy.deepcopy(h)
    events(h, "ACTIVITY_TASK_SCHEDULED")[0]["activityTaskScheduledEventAttributes"][
        "activityType"]["name"] = "mystery"
    if wf_type is not None:
        h["events"][0]["workflowExecutionStartedEventAttributes"]["workflowType"] = {"name": wf_type}
    return h


def test_foreign_workflow_with_unknown_activities_raises_no_alarm(world, projector):
    h = with_mystery(attempt_history(), "SomethingElse")
    proj = project_history(h, workflow_id="other:X", run_id="r1")
    assert proj.shape_issues == ()
    projector.apply_run(proj, run_ord=0)
    assert alarms(world) == []


def test_card_workflow_with_one_unknown_activity_raises_exactly_one(world, projector):
    for wf_type in ("Card", "CardWorkflow"):
        proj = project_history(with_mystery(attempt_history(), wf_type), workflow_id="card:demo:A",
                               run_id="r1")
        assert len(proj.shape_issues) == 1
        projector.apply_run(proj, run_ord=0)
        projector.apply_run(proj, run_ord=0)
    assert len(alarms(world)) == 1


def test_plan_key_memo_marks_a_card_workflow_whatever_its_type(world, projector):
    h = with_mystery(load(ROOT / "tests/fixtures/temporal/01_ok_single.json"), "Renamed")
    assert len(project_history(h, workflow_id="x", run_id="r1").shape_issues) == 1
