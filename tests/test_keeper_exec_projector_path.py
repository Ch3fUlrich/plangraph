"""Exec CAS against states written by the real history parser and projector."""
import base64
import json

import pytest

from keeper_util import World, seed, seed_exec_owner
from plangraph.keeper.errors import Refused
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def event(n, kind, **attrs):
    return {"eventId": str(n), "eventTime": "2026-10-22T10:00:00Z",
            "eventType": "EVENT_TYPE_" + kind,
            kind.lower().split("_")[0] + "".join(
                p.capitalize() for p in kind.lower().split("_")[1:])
            + "EventAttributes": attrs}


def history(exit_class):
    payload = {"metadata": {"encoding": base64.b64encode(b"json/plain").decode()},
               "data": base64.b64encode(json.dumps({"exit_class": exit_class}).encode()).decode()}
    return {"events": [
        event(1, "WORKFLOW_EXECUTION_STARTED", workflowType={"name": "Card"}),
        event(2, "ACTIVITY_TASK_SCHEDULED", activityId="attempt-1",
              activityType={"name": "attempt"}),
        event(3, "ACTIVITY_TASK_STARTED", scheduledEventId="2", attempt=1),
        event(4, "ACTIVITY_TASK_COMPLETED", scheduledEventId="2",
              result={"payloads": [payload]}),
        event(5, "TIMER_STARTED", timerId="wait"),
    ]}


#: The worker's live set (RT-P1-EXEC-OWNERSHIP).  ``waiting`` belongs to it:
#: the projector writes ``waiting`` after usage_limit/outage and on a timer, so
#: the cap-park must still land there.
LIVE_SET = ["open", "ready", "running", "needs_input", "waiting"]


def snapshot(w, key):
    return tuple(w.conn.execute(
        "SELECT state, exec_workflow_id, exec_run_id, exec_attempt, updated_at"
        " FROM items WHERE key=?", (key,)).fetchone())


def worker_call(w, key, verb, expected):
    identity = {"workflow_id": "card:" + key, "run_id": "run", "attempt": 1}
    if verb == "ask_question":
        return w.store.ask_question(w.p("workerA"), key, "Which option?",
                                    expected_state=expected,
                                    **{"exec_" + k: v for k, v in identity.items()})
    return w.store.worker_park(w.p("workerA"), {
        "card": key, "exit_class": "needs_input", "expected_state": expected, **identity})


@pytest.mark.parametrize("verb", ["ask_question", "park"])
def test_needs_input_timer_preserves_projected_state_and_worker_cas(w, verb):
    key, = seed(w, [("A", [])])
    projector = Projector(w.store, w.p("system"), now=w.clock)
    full = history("needs_input")
    before_timer = {"events": full["events"][:-1]}
    projection = project_history(before_timer, workflow_id="card:" + key, run_id="run")
    assert projector.apply_run(projection, run_ord=0).states_written == 2
    assert snapshot(w, key)[:4] == ("needs_input", None, None, None)
    projection = project_history(full, workflow_id="card:" + key, run_id="run")
    assert [s.state for s in projection.states] == ["running", "needs_input"]
    assert projector.apply_run(projection, run_ord=0).states_written == 0
    before = snapshot(w, key)
    with pytest.raises(Refused) as exc:
        worker_call(w, key, verb, "running")
    assert exc.value.code == "exec_stale_state"
    assert snapshot(w, key) == before
    seed_exec_owner(w, key, ("card:" + key, "run", 1))
    worker_call(w, key, verb, before[0])
    assert snapshot(w, key)[:4] == (
        "needs_input" if verb == "ask_question" else "parked", "card:" + key, "run", 1)


@pytest.mark.parametrize("expected", ["running", ["open", "ready", "running", "needs_input"]])
def test_projector_waiting_refuses_delayed_same_run_park(w, expected):
    key, = seed(w, [("A", [])])
    projector = Projector(w.store, w.p("system"), now=w.clock)
    full = history("usage_limit")
    running = project_history({"events": full["events"][:3]},
                              workflow_id="card:" + key, run_id="run")
    assert projector.apply_run(running, run_ord=0).states_written == 1
    assert snapshot(w, key)[0] == "running"
    # First run-carrying worker fact claims ownership; projector does not yet forward run ids.
    seed_exec_owner(w, key, ("card:" + key, "run", 1))
    w.store.worker_alarm(w.p("workerA"), {
        "card": key, "reason": "retry", "workflow_id": "card:" + key,
        "run_id": "run", "attempt": 1})
    waiting = project_history(full, workflow_id="card:" + key, run_id="run")
    assert projector.apply_run(waiting, run_ord=0).states_written == 1
    before = snapshot(w, key)
    assert before[:4] == ("waiting", "card:" + key, "run", 1)

    # A park from another run is stale before any state compare: exec_stale.
    with pytest.raises(Refused) as exc:
        w.store.worker_park(w.p("workerA"), {
            "card": key, "exit_class": "needs_input", "expected_state": "waiting",
            "workflow_id": "card:" + key, "run_id": "other-run", "attempt": 1})
    assert exc.value.code == "exec_stale"
    assert snapshot(w, key) == before

    # Same run, but an expectation that excludes the observed waiting state --
    # including the live set as it stood before waiting joined it -- is still
    # refused, and changes nothing.
    with pytest.raises(Refused) as exc:
        worker_call(w, key, "park", expected)
    assert exc.value.code == "exec_stale_state"
    assert snapshot(w, key) == before

    # The live set carries waiting now, so the delayed cap-park lands.
    worker_call(w, key, "park", LIVE_SET)
    assert snapshot(w, key)[:4] == ("parked", "card:" + key, "run", 1)
