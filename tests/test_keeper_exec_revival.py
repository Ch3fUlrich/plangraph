"""RT-P1-EXEC-OWNERSHIP: a run-less hand-back to open/ready revives the card.

``_worker_park`` parks with no ``run``, so a parked card keeps the owner it
recorded; a router/operator ``set_state`` back to ``open``/``ready`` used to
keep it too, and ``claim`` preserves whatever it finds -- the next run was then
refused as ``exec_stale`` forever.  The rule: every non-worker hand-back clears
the owner via ``_exec_clear`` in the same transaction as the state UPDATE,
whenever the move changes the state, lands on ``open``/``ready`` and carries no
``run``.  No-op moves, run-carrying moves and moves to any other state must NOT
clear.

Every test here ends by asserting the owner is NULL after a real hand-back (or
asserts a live owner right before one), so the whole file fails when the
clearing is removed -- and tests 3 and 4 fail as well if the clear leaks onto
moves that must not have it.  Portable: runs on the default backend (``World``
follows ``PG_KEEPER_BACKEND``).
"""
import pytest

from keeper_util import World, seed, to_claimed
from test_keeper_exec_ownership import owner, refused
from test_keeper_exec_verbs import complete, park


RUN_A = ("wf-1", "run-1", 1)
RUN_B = ("wf-2", "run-2", 1)
NONE = (None, None, None)


def ident(run):
    """The ``workflow_id``/``run_id``/``attempt`` triple of *run* as body fields."""
    return dict(zip(("workflow_id", "run_id", "attempt"), run))


def ask(w, key, who, run):
    return w.store.ask_question(w.p(who), key, "Which path?", **{
        "exec_" + name: value for name, value in ident(run).items()})


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def test_open_hand_back_clears_the_owner_and_run_b_is_accepted(w):
    key, = seed(w, [("A", [])])
    worker = w.p("workerA")
    # run A parks the card and owns it; the park carries no run of its own.
    assert w.store.worker_park(worker, park(key, **ident(RUN_A)))["state"] == "parked"
    assert owner(w, key) == RUN_A
    # Router hand-back: the owner is freed in the same transaction as the move.
    w.store.set_state(w.p("router"), key, "open")
    assert owner(w, key) == NONE
    to_claimed(w, key, "workerA")
    assert owner(w, key) == NONE
    # run B is now accepted by every exec-checked worker verb.
    ask(w, key, "workerA", RUN_B)
    assert owner(w, key) == RUN_B
    w.store.worker_complete(worker, complete(key, **ident(RUN_B)))
    assert owner(w, key) == RUN_B
    assert w.store.worker_park(worker, park(key, **ident(RUN_B))) == {
        "card": key, "state": "parked", "changed": True}
    assert owner(w, key) == RUN_B


def test_failed_to_ready_hand_back_clears_and_run_b_is_accepted(w):
    key, = seed(w, [("A", [])])
    worker = w.p("workerA")
    assert w.store.worker_park(worker, park(key, **ident(RUN_A)))["state"] == "parked"
    assert owner(w, key) == RUN_A
    # Any other target is not a hand-back: the owner survives.
    w.store.set_state(w.p("router"), key, "failed")
    assert owner(w, key) == RUN_A
    # failed -> ready is a hand-back: the owner is freed.
    w.store.set_state(w.p("router"), key, "ready")
    assert owner(w, key) == NONE
    ask(w, key, "workerA", RUN_B)
    assert owner(w, key) == RUN_B
    w.store.worker_complete(worker, complete(key, **ident(RUN_B)))
    assert owner(w, key) == RUN_B
    assert w.store.worker_park(worker, park(key, **ident(RUN_B))) == {
        "card": key, "state": "parked", "changed": True}
    assert owner(w, key) == RUN_B


def test_a_no_op_set_state_open_does_not_clear(w):
    key, = seed(w, [("A", [])])
    ask(w, key, "router", RUN_A)
    assert owner(w, key) == RUN_A
    # Re-applying the state the card already has changes nothing: no clear.
    assert w.store.set_state(w.p("router"), key, "open") == {
        "key": key, "state": "open", "changed": False}
    assert owner(w, key) == RUN_A
    # A run-carrying move is not a hand-back either: it keeps the owner.
    w.store.set_state(w.p("router"), key, "ready", run=RUN_A)
    assert owner(w, key) == RUN_A
    # ...and the real, changing, run-less hand-back still clears.
    w.store.set_state(w.p("router"), key, "open")
    assert owner(w, key) == NONE


def test_router_moves_to_running_and_waiting_without_a_run_do_not_clear(w):
    key, = seed(w, [("A", [])])
    ask(w, key, "router", RUN_A)
    assert owner(w, key) == RUN_A
    for state in ("running", "waiting"):
        w.store.set_state(w.p("router"), key, state)
        assert owner(w, key) == RUN_A
    # The hand-back to ready is what frees it -- not the move before it.
    w.store.set_state(w.p("router"), key, "ready")
    assert owner(w, key) == NONE


def test_a_stale_run_still_cannot_use_the_revived_card(w):
    # The hand-back frees the owner; it never hands the card to another run.
    key, = seed(w, [("A", [])])
    assert w.store.worker_park(w.p("workerA"),
                               park(key, **ident(RUN_A)))["state"] == "parked"
    w.store.set_state(w.p("router"), key, "open")
    assert owner(w, key) == NONE
    ask(w, key, "workerA", RUN_B)
    assert owner(w, key) == RUN_B
    refused("exec_stale", w.store.worker_park, w.p("workerA"),
            park(key, **ident(RUN_A)))
    refused("exec_stale", w.store.set_state, w.p("router"), key, "running",
            run=RUN_A)
    assert owner(w, key) == RUN_B
