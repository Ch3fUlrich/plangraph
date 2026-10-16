"""Pure history parser: every recorded fixture projects to hand-verified records."""
import base64
import copy
import json
from pathlib import Path

import pytest

from plangraph.projector.history import decode_payload, project_history
from plangraph.projector.models import EXIT_CLASSES, SETTABLE_STATES, RunProjection

FIX = Path(__file__).parent / "fixtures" / "temporal"


def load(name):
    d = json.loads((FIX / f"{name}.json").read_text())
    return d["history"], d["workflow_id"], d["run_id"]


def project(name):
    history, wf, run = load(name)
    return project_history(history, workflow_id=wf, run_id=run)


def attempts(p):
    """(activity_id, attempt, scheduled, closed, exit_class) per attempt."""
    return [(a.activity_id, a.attempt, a.scheduled_event_id, a.closed_event_id, a.exit_class)
            for a in p.attempts]


def states(p):
    return [(s.event_id, s.state) for s in p.states]


def summary(p):
    return (p.plan_key, p.status, p.outcome, p.next_run_id, p.last_event_id)


def test_01_ok_single():
    p = project("01_ok_single")
    assert summary(p) == ("DEMO-1", "completed", "done", None, 11)
    assert p.workflow_id == "card:DEMO-1" and p.run_id == "01a14126-77cd-75a4-a844-3547758b01c5"
    assert p.start_time == "2026-10-15T20:00:02.509370397Z"
    assert attempts(p) == [("1", 1, 5, 7, "ok")]
    a = p.attempts[0]
    assert (a.workflow_id, a.run_id, a.plan_key) == (p.workflow_id, p.run_id, "DEMO-1")
    assert (a.harness, a.model, a.combo, a.class_) == ("demo-harness", "demo-model", "demo-combo", "coding")
    assert (a.started_at, a.ended_at) == ("2026-10-15T20:00:02.525727Z", "2026-10-15T20:00:02.727137Z")
    assert (a.tokens_in, a.tokens_out, a.cost) == (1200, 300, 0.0125)
    assert states(p) == [(5, "running")]
    assert all(s.plan_key == "DEMO-1" for s in p.states)


def test_02_check_fail_then_ok():
    p = project("02_check_fail_then_ok")
    assert summary(p) == ("DEMO-2", "completed", "done", None, 17)
    assert attempts(p) == [("1", 1, 5, 7, "check_fail"), ("2", 1, 11, 13, "ok")]
    assert [a.tokens_in for a in p.attempts] == [1200, 2400]
    assert states(p) == [(5, "running")]  # a check_fail close and the rerun add no state change


def test_03_usage_limit_wait_then_ok():
    p = project("03_usage_limit_wait_then_ok")
    assert summary(p) == ("DEMO-3", "completed", "done", None, 22)
    assert attempts(p) == [("1", 1, 5, 7, "usage_limit"), ("2", 1, 16, 18, "ok")]
    # the TimerStarted (11) repeats `waiting`, so no second record
    assert states(p) == [(5, "running"), (7, "waiting"), (16, "running")]


def test_04_needs_input_signal():
    p = project("04_needs_input_signal")
    assert summary(p) == ("DEMO-4", "completed", "done", None, 23)
    assert attempts(p) == [("1", 1, 5, 7, "needs_input"), ("2", 1, 17, 19, "ok")]
    # TimerStarted (11) and the answer Signaled (12) must not disturb needs_input
    assert states(p) == [(5, "running"), (7, "needs_input"), (17, "running")]


def test_05_app_failure():
    p = project("05_app_failure")
    assert summary(p) == ("DEMO-5", "completed", "parked", None, 11)
    assert attempts(p) == [("1", 1, 5, 7, "contract_defect")]
    assert (p.attempts[0].harness, p.attempts[0].tokens_in, p.attempts[0].cost) == (None, None, None)
    assert p.attempts[0].started_at == "2026-10-15T20:00:05.305822764Z"
    assert p.attempts[0].ended_at == "2026-10-15T20:00:05.310848949Z"
    assert states(p) == [(5, "running"), (11, "parked")]


def test_06_heartbeat_timeout():
    p = project("06_heartbeat_timeout")
    assert summary(p) == ("DEMO-6", "completed", "failed", None, 11)
    assert attempts(p) == [("1", 1, 5, 7, "lease_lost")]
    assert p.attempts[0].ended_at == "2026-10-15T20:00:06.336602957Z"
    assert states(p) == [(5, "running"), (11, "failed")]


def test_07_start_to_close_timeout():
    p = project("07_start_to_close_timeout")
    assert summary(p) == ("DEMO-7", "completed", "failed", None, 11)
    assert attempts(p) == [("1", 1, 5, 7, "timeout")]
    assert states(p) == [(5, "running"), (11, "failed")]


def test_08_cancelled():
    p = project("08_cancelled")
    assert summary(p) == ("DEMO-8", "canceled", None, None, 16)
    # Started (11) follows CancelRequested (10); the attempt closes at ActivityTaskCanceled (12)
    assert attempts(p) == [("1", 1, 5, 12, "cancelled")]
    assert p.attempts[0].started_at == "2026-10-15T20:00:07.385020675Z"
    assert states(p) == [(5, "running"), (16, "parked")]  # never `cancelled`


def test_09_continue_as_new():
    r1, r2 = project("09_continue_as_new_run1"), project("09_continue_as_new_run2")
    assert summary(r1) == ("DEMO-9", "continued_as_new", None, r2.run_id, 11)
    assert r2.run_id == "b3a55ce5-89ba-4097-8f02-88d4c1b93911"
    assert attempts(r1) == [("1", 1, 5, 7, "ok")] and states(r1) == [(5, "running")]
    assert summary(r2) == ("DEMO-9", "completed", "done", None, 11)  # plan key from the carried memo
    assert attempts(r2) == [("1", 1, 5, 7, "ok")] and states(r2) == [(5, "running")]
    assert (r1.attempts[0].tokens_in, r2.attempts[0].tokens_in) == (1200, 2400)


def test_10_workflow_failed():
    p = project("10_workflow_failed")
    assert summary(p) == ("DEMO-10", "failed", None, None, 11)
    assert attempts(p) == [("1", 1, 5, 7, "ok")]
    assert states(p) == [(5, "running"), (11, "failed")]


def test_11_unmapped():
    p = project("11_unmapped")
    assert p.workflow_id == "misc-1"
    assert summary(p) == (None, "completed", "done", None, 11)
    assert p.attempts == () and p.states == ()
    assert p.start_time == "2026-10-15T20:00:10.710879554Z"


def test_12_terminated_while_scheduled():
    p = project("12_terminated")
    assert summary(p) == ("DEMO-12", "terminated", None, None, 6)
    assert p.attempts == ()  # the activity never closed
    assert states(p) == [(5, "running"), (6, "parked")]


def test_13_result_without_exit_class():
    p = project("13_second_attempt_started")
    assert summary(p) == ("DEMO-13", "completed", "done", None, 11)
    assert attempts(p) == [("1", 1, 5, 7, "ok")]
    a = p.attempts[0]
    assert (a.harness, a.model, a.combo, a.class_) == ("demo-harness", "demo-model", None, None)
    assert (a.tokens_in, a.tokens_out, a.cost) == (None, None, None)
    # times fall back to the Started and the closing event times
    assert (a.started_at, a.ended_at) == ("2026-10-15T20:00:12.186292978Z", "2026-10-15T20:00:12.392331329Z")
    assert states(p) == [(5, "running")]


def test_every_fixture_stays_in_the_keeper_vocabulary():
    for f in sorted(FIX.glob("*.json")):
        d = json.loads(f.read_text())
        p = project_history(d["history"], workflow_id=d["workflow_id"], run_id=d["run_id"])
        assert isinstance(p, RunProjection)
        assert all(a.exit_class in EXIT_CLASSES for a in p.attempts)
        assert all(s.state in SETTABLE_STATES for s in p.states)
        assert list(p.attempts) == sorted(p.attempts, key=lambda a: a.scheduled_event_id)
        assert [s.event_id for s in p.states] == sorted(s.event_id for s in p.states)


def test_deterministic_and_never_mutates_input():
    for name in ("04_needs_input_signal", "08_cancelled", "09_continue_as_new_run1"):
        history, wf, run = load(name)
        before = copy.deepcopy(history)
        first = project_history(history, workflow_id=wf, run_id=run)
        second = project_history(history, workflow_id=wf, run_id=run)
        assert first == second
        assert history == before


def test_unknown_event_types_and_unknown_activities_are_ignored():
    history, wf, run = load("01_ok_single")
    base = project_history(history, workflow_id=wf, run_id=run)
    noisy = copy.deepcopy(history)
    noisy["events"].insert(2, {"eventId": "99", "eventTime": "2026-10-15T20:00:03Z",
                               "eventType": "EVENT_TYPE_SOMETHING_NEW",
                               "somethingNewEventAttributes": {"x": 1}})
    other = {"eventId": "100", "eventTime": "2026-10-15T20:00:03Z",
             "eventType": "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED",
             "activityTaskScheduledEventAttributes": {"activityId": "z", "activityType": {"name": "other"}}}
    noisy["events"].insert(3, other)
    noisy["events"].insert(4, {"eventId": "101", "eventTime": "2026-10-15T20:00:03Z",
                               "eventType": "EVENT_TYPE_ACTIVITY_TASK_COMPLETED",
                               "activityTaskCompletedEventAttributes": {"scheduledEventId": "100"}})
    got = project_history(noisy, workflow_id=wf, run_id=run)
    assert got.attempts == base.attempts and got.states == base.states
    assert got.last_event_id == 101


def test_events_are_taken_in_event_id_order():
    history, wf, run = load("02_check_fail_then_ok")
    shuffled = copy.deepcopy(history)
    first, rest = shuffled["events"][0], shuffled["events"][1:]
    shuffled["events"] = [first] + rest[::-1]
    assert project_history(shuffled, workflow_id=wf, run_id=run) == project_history(
        history, workflow_id=wf, run_id=run)


@pytest.mark.parametrize("bad", [
    {}, {"events": []}, {"events": None}, None,
    {"events": [{"eventId": "1", "eventType": "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED"}]},
])
def test_structurally_broken_history_raises(bad):
    with pytest.raises(ValueError):
        project_history(bad, workflow_id="card:X", run_id="r")


def test_first_event_must_be_workflow_started():
    history, wf, run = load("01_ok_single")
    broken = copy.deepcopy(history)
    broken["events"] = broken["events"][1:]
    with pytest.raises(ValueError):
        project_history(broken, workflow_id=wf, run_id=run)


def test_plan_key_falls_back_to_workflow_id():
    history, _, run = load("11_unmapped")
    assert project_history(history, workflow_id="card:K-1", run_id=run).plan_key == "K-1"
    assert project_history(history, workflow_id="card/K-2", run_id=run).plan_key == "K-2"
    assert project_history(history, workflow_id="card:", run_id=run).plan_key is None
    assert project_history(history, workflow_id="misc", run_id=run).plan_key is None


def test_memo_plan_key_wins_over_workflow_id():
    history, _, run = load("01_ok_single")
    assert project_history(history, workflow_id="card:OTHER", run_id=run).plan_key == "DEMO-1"


def test_payload_decoding():
    def enc(text):
        return base64.b64encode(text.encode()).decode()

    ok = {"metadata": {"encoding": enc("json/plain")}, "data": enc('{"a": 1}')}
    assert decode_payload(ok) == {"a": 1}
    assert decode_payload({"metadata": {"encoding": enc("binary/plain")}, "data": enc("x")}) is None
    assert decode_payload({"metadata": {"encoding": enc("json/plain")}, "data": enc("{oops")}) is None
    assert decode_payload({"metadata": {"encoding": enc("json/plain")}, "data": "!!"}) is None
    assert decode_payload({"metadata": {}, "data": enc("1")}) is None
    assert decode_payload("proceed") is None
    assert decode_payload(None) is None
