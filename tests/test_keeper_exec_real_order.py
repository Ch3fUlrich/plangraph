"""Worker reports precede projection in production; do not pre-project needs_input."""
import pytest

from keeper_util import seed, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.keeper.store_cards import TRANSITIONS, _forward
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from test_keeper_exec_projector_path import w, history, snapshot, worker_call


LIVE_STATES = ["open", "ready", "running", "needs_input"]


def seed_in_state(w, initial):
    key, = seed(w, [("A", [])])
    # create_items always creates open cards; seed ready directly, not via a
    # synthetic running/projector transition before the worker report.
    if initial == "ready":
        w.conn.execute("UPDATE items SET state=? WHERE key=?", (initial, key))
        w.conn.commit()
    assert snapshot(w, key)[:4] == (initial, None, None, None)
    return key


@pytest.mark.parametrize("initial", ["open", "ready"])
def test_report_then_ask_before_projection(w, initial):
    key = seed_in_state(w, initial)
    identity = {"workflow_id": "card:" + key, "run_id": "run", "attempt": 1}
    w.store.worker_attempt(w.p("workerA"), {
        "card": key, "n": 1, "exit_class": "needs_input", **identity})
    assert snapshot(w, key)[:4] == (initial, None, None, None)
    worker_call(w, key, "ask_question", LIVE_STATES)
    assert snapshot(w, key)[0] == initial
    worker_call(w, key, "park", LIVE_STATES)
    assert snapshot(w, key)[:4] == ("parked", "card:" + key, "run", 1)
    projector = Projector(w.store, w.p("system"), now=w.clock)
    projector.apply_run(project_history(history("needs_input"),
                        workflow_id="card:" + key, run_id="run"), run_ord=0)
    assert snapshot(w, key)[0] == "needs_input"
    worker_call(w, key, "ask_question", LIVE_STATES)
    worker_call(w, key, "park", LIVE_STATES)
    assert snapshot(w, key)[0] == "parked"


@pytest.mark.parametrize("verb", ["ask_question", "park"])
@pytest.mark.parametrize("initial", ["open", "ready"])
def test_projected_waiting_refuses_delayed_any_of(w, verb, initial):
    key = seed_in_state(w, initial)
    worker_call(w, key, "ask_question", LIVE_STATES)
    projector = Projector(w.store, w.p("system"), now=w.clock)
    projector.apply_run(project_history(history("usage_limit"),
                        workflow_id="card:" + key, run_id="run"), run_ord=0)
    before = snapshot(w, key)
    assert before[0] == "waiting"
    with pytest.raises(Refused) as err:
        worker_call(w, key, verb, LIVE_STATES)
    assert err.value.code == "exec_stale_state"
    assert snapshot(w, key) == before


@pytest.mark.parametrize("verb", ["ask_question", "park"])
def test_claimed_holder_live_set_is_stale(w, verb):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    before = dict(w.store._item(key))
    with pytest.raises(Refused) as err:
        worker_call(w, key, verb, LIVE_STATES)
    assert err.value.code == "exec_stale_state"
    assert dict(w.store._item(key)) == before


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("triple", [False, True])
def test_claimed_holder_park_is_exempt_from_forward_table(w, owned, triple):
    key, = seed(w, [("A", [])])
    if owned:
        w.store.set_state(w.p("system"), key, "running",
                          run=("card:" + key, "run", 1))
        w.store.set_state(w.p("system"), key, "open")
    to_claimed(w, key, "workerA")
    body = {"card": key, "exit_class": "needs_input"}
    if triple:
        body.update(workflow_id="card:" + key, run_id="run", attempt=1)
    before = dict(w.store._item(key))
    if owned and not triple:
        with pytest.raises(Refused) as err:
            w.store.worker_park(w.p("workerA"), body)
        assert err.value.code == "exec_stale"
        assert dict(w.store._item(key)) == before
    else:
        result = w.store.worker_park(w.p("workerA"), body)
        assert result == {"card": key, "state": "parked", "changed": True}
        item = w.store._item(key)
        assert item["claimed_by"] is None
        assert item["lease_until"] is None
        assert (item["exec_workflow_id"], item["exec_run_id"], item["exec_attempt"]) == (
            ("card:" + key, "run", 1) if triple else (None, None, None))


def test_forward_closure_reaches_parked_from_every_table_state():
    # The park forward-table guard is dead: CAS supplies the state protection.
    assert all(_forward(state, "parked") for state in TRANSITIONS)
    assert not _forward("claimed", "parked")
    assert not _forward("parked", "open")


@pytest.mark.parametrize("state", ["open", "ready", "running", "needs_input",
                                   "waiting", "failed", "parked"])
def test_exec_park_forward_reachability_and_noop(w, state):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("system"), key, state)
    before = dict(w.store._item(key))
    result = worker_call(w, key, "park", [state])
    assert result["changed"] is (state != "parked")
    assert snapshot(w, key)[0] == "parked"
    if state == "parked":
        assert dict(w.store._item(key)) == before
