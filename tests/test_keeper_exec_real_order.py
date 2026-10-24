"""Worker reports precede projection in production; do not pre-project needs_input."""
import pytest

from keeper_util import seed, seed_exec_owner, to_claimed
from plangraph.keeper.errors import Refused
from plangraph.keeper.store_cards import TRANSITIONS, _forward
from plangraph.projector.apply import Projector
from plangraph.projector.history import project_history
from test_keeper_exec_projector_path import LIVE_SET, w, history, snapshot, worker_call

# The live set the worker pins (waiting joined it: the projector writes
# waiting after usage_limit/outage and on a timer, so the cap-park must land).
LIVE_STATES = LIVE_SET
#: The live set as it stood before ``waiting`` joined it: any expectation that
#: excludes the observed state is refused with ``exec_stale_state``.
STALE_SET = ["open", "ready", "running", "needs_input"]


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
    seed_exec_owner(w, key, ("card:" + key, "run", 1))
    worker_call(w, key, "ask_question", LIVE_STATES)
    assert snapshot(w, key)[0] == initial
    worker_call(w, key, "park", LIVE_STATES)
    assert snapshot(w, key)[:4] == ("parked", "card:" + key, "run", 1)
    projector = Projector(w.store, w.p("system"), now=w.clock)
    result = projector.apply_run(project_history(history("needs_input"),
                                 workflow_id="card:" + key, run_id="run"), run_ord=0)
    # RT-P1 sq5 fix 3: the late projection would walk an OWNED parked card
    # backwards (parked -> running, parked -> needs_input), so the state
    # machine refuses both writes and the projector skips them: the worker's
    # park and its owner stand instead of being clobbered by stale events.
    assert (result.states_written, result.states_skipped_state) == (0, 2)
    assert snapshot(w, key)[:4] == ("parked", "card:" + key, "run", 1)
    # The recorded owner retrying its park is still a success no-op, decided
    # before the observed-state set the projection made stale ...
    assert worker_call(w, key, "park", LIVE_STATES) == {
        "card": key, "state": "parked", "changed": False}
    # ... while an ask that still expects the pre-park live set is fenced out.
    with pytest.raises(Refused) as err:
        worker_call(w, key, "ask_question", LIVE_STATES)
    assert err.value.code == "exec_stale_state"
    assert snapshot(w, key)[:4] == ("parked", "card:" + key, "run", 1)


@pytest.mark.parametrize("verb", ["ask_question", "park"])
@pytest.mark.parametrize("initial", ["open", "ready"])
def test_projected_waiting_refuses_delayed_any_of(w, verb, initial):
    key = seed_in_state(w, initial)
    seed_exec_owner(w, key, ("card:" + key, "run", 1))
    worker_call(w, key, "ask_question", LIVE_STATES)
    projector = Projector(w.store, w.p("system"), now=w.clock)
    projector.apply_run(project_history(history("usage_limit"),
                        workflow_id="card:" + key, run_id="run"), run_ord=0)
    before = snapshot(w, key)
    assert before[0] == "waiting"
    # A delayed verb whose expectation excludes the observed waiting state
    # (the pre-waiting live set) is still refused, and writes nothing ...
    with pytest.raises(Refused) as err:
        worker_call(w, key, verb, STALE_SET)
    assert err.value.code == "exec_stale_state"
    assert snapshot(w, key) == before
    # ... while the live set, which carries waiting now, is accepted.
    worker_call(w, key, verb, LIVE_STATES)
    assert snapshot(w, key)[0] == ("waiting" if verb == "ask_question" else "parked")


@pytest.mark.parametrize("verb", ["ask_question", "park"])
def test_claimed_holder_live_set_is_stale(w, verb):
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")
    worker_call(w, key, verb, LIVE_STATES)
    assert snapshot(w, key)[:4] == (
        "parked" if verb == "park" else "claimed", "card:" + key, "run", 1)


@pytest.mark.parametrize("verb", ["ask_question", "park"])
@pytest.mark.parametrize("left", ["parked", "failed", "done"])
def test_live_set_refuses_a_card_that_left_the_live_states(w, verb, left):
    key, = seed(w, [("A", [])])
    if left == "done":                       # done has its own verb, not set_state
        w.conn.execute("UPDATE items SET state=? WHERE key=?", (left, key))
        w.conn.commit()
    else:
        w.store.set_state(w.p("system"), key, left)
    before = snapshot(w, key)
    with pytest.raises(Refused) as err:
        worker_call(w, key, verb, LIVE_STATES)
    assert err.value.code == "exec_stale_state"
    assert snapshot(w, key) == before


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("triple", [False, True])
def test_claimed_holder_park_is_exempt_from_forward_table(w, owned, triple):
    key, revive = seed(w, [("A", []), ("B", [])])
    if owned:
        # (a) The explicit hand-back to open clears the exec owner (W19), so a
        # parked card revives with a new run: the revived card takes its claim
        # holder's triple-less park.  Only a worker may RECORD an owner (V2-1),
        # so it is seeded before the system run-carrying move that checks it.
        seed_exec_owner(w, revive, ("card:" + revive, "run", 1))
        w.store.set_state(w.p("system"), revive, "running",
                          run=("card:" + revive, "run", 1))
        w.store.set_state(w.p("system"), revive, "open", hand_back=True)
        assert snapshot(w, revive)[1:4] == (None, None, None)
        to_claimed(w, revive, "workerA")
        result = w.store.worker_park(w.p("workerA"),
                                     {"card": revive, "exit_class": "needs_input"})
        assert result == {"card": revive, "state": "parked", "changed": True}
        assert snapshot(w, revive)[0] == "parked"
        # (b) The owner is still recorded while the card is claimed: a
        # run-carrying worker fact (complete) records it after the claim and
        # neither verb clears it.
        to_claimed(w, key, "workerA")
        w.store.worker_complete(w.p("workerA"), {
            "card": key, "merged_sha": "deadbeef",
            "workflow_id": "card:" + key, "run_id": "run", "attempt": 1})
        assert snapshot(w, key)[1:4] == ("card:" + key, "run", 1)
    else:
        to_claimed(w, key, "workerA")
    body = {"card": key, "exit_class": "needs_input"}
    if triple:
        body.update(workflow_id="card:" + key, run_id="run", attempt=1)
    before = dict(w.store._item(key))
    if owned and not triple:
        # The recorded owner names no triple here: refused before any write.
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
    seed_exec_owner(w, key, ("card:" + key, "run", 1))
    before = dict(w.store._item(key))
    result = worker_call(w, key, "park", [state])
    assert result["changed"] is (state != "parked")
    assert snapshot(w, key)[0] == "parked"
    if state == "parked":
        assert dict(w.store._item(key)) == before
