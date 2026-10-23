"""RT-P1-EXEC-OWNERSHIP: only an explicit hand-back to open/ready clears the owner.

``_worker_park`` parks with no ``run``, so a parked card keeps the owner it
recorded; without a clear, ``claim`` preserves whatever it finds and the next
run was refused as ``exec_stale`` forever.  The rule: the owner is cleared only
by an EXPLICIT hand-back door -- ``set_state(..., hand_back=True)`` that
changes the state and lands on ``open``/``ready`` -- via ``_exec_clear`` in the
same transaction as the state UPDATE.  A bare run-less ``set_state`` (the
projector/planner write path) is NOT a hand-back: it keeps the owner of a live
run, so no run-less write can free that card (V1-B).  No-op moves,
run-carrying moves and moves to any other state must NOT clear either.

Every test here ends by asserting the owner is NULL after an explicit hand-back
(or asserts a live owner right before one), so the whole file fails when the
clearing is removed -- ``test_a_no_op_set_state_open_does_not_clear`` and
``test_router_moves_to_running_and_waiting_without_a_run_do_not_clear`` fail as
well if the clear leaks onto moves that must not have it, and
``test_a_bare_run_less_set_state_keeps_the_owner`` pins the bare-write side.
Portable: runs on the default backend (``World`` follows
``PG_KEEPER_BACKEND``).
"""
import pytest

from keeper_util import World, seed, to_claimed
from test_keeper_exec_ownership import owner, refused
from test_keeper_exec_verbs import complete, park, state as state_of


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
    # Explicit hand-back: the owner is freed in the same transaction as the move.
    w.store.set_state(w.p("router"), key, "open", hand_back=True)
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
    # failed -> ready is an explicit hand-back: the owner is freed.
    w.store.set_state(w.p("router"), key, "ready", hand_back=True)
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
    # V2-1 (sq5 fix 1): only a worker principal may RECORD an owner, so the
    # seeding ask comes from the worker; the router moves below only check it.
    ask(w, key, "workerA", RUN_A)
    assert owner(w, key) == RUN_A
    # Re-applying the state the card already has changes nothing: no clear.
    assert w.store.set_state(w.p("router"), key, "open") == {
        "key": key, "state": "open", "changed": False}
    assert owner(w, key) == RUN_A
    # A run-carrying move is not a hand-back either: it keeps the owner.
    w.store.set_state(w.p("router"), key, "ready", run=RUN_A)
    assert owner(w, key) == RUN_A
    # ...and the explicit hand-back door, changing and run-less, still clears.
    w.store.set_state(w.p("router"), key, "open", hand_back=True)
    assert owner(w, key) == NONE


def test_router_moves_to_running_and_waiting_without_a_run_do_not_clear(w):
    key, = seed(w, [("A", [])])
    # Seeded by the worker (V2-1); the router run-less moves below must not clear it.
    ask(w, key, "workerA", RUN_A)
    assert owner(w, key) == RUN_A
    for state in ("running", "waiting"):
        w.store.set_state(w.p("router"), key, state)
        assert owner(w, key) == RUN_A
    # The explicit hand-back to ready is what frees it -- not the move before it.
    w.store.set_state(w.p("router"), key, "ready", hand_back=True)
    assert owner(w, key) == NONE


def test_a_stale_run_still_cannot_use_the_revived_card(w):
    # The hand-back frees the owner; it never hands the card to another run.
    key, = seed(w, [("A", [])])
    assert w.store.worker_park(w.p("workerA"),
                               park(key, **ident(RUN_A)))["state"] == "parked"
    w.store.set_state(w.p("router"), key, "open", hand_back=True)
    assert owner(w, key) == NONE
    ask(w, key, "workerA", RUN_B)
    assert owner(w, key) == RUN_B
    refused("exec_stale", w.store.worker_park, w.p("workerA"),
            park(key, **ident(RUN_A)))
    refused("exec_stale", w.store.set_state, w.p("router"), key, "running",
            run=RUN_A)
    assert owner(w, key) == RUN_B


def test_a_bare_run_less_set_state_keeps_the_owner(w):
    """V1-B: a bare run-less write is not a hand-back door.

    A projector/planner ``set_state`` (no ``run``, no ``hand_back``) that moves
    an owned card FORWARD changes the state but must NOT free the owner of a
    live run -- only the explicit ``hand_back=True`` door does.  A bare BACK
    move on an owned card is refused instead (sq5 fix 3, deepseek V1), so the
    forward move is the only bare write left that can strand this way.
    """
    key, = seed(w, [("A", [])])
    worker = w.p("workerA")
    assert w.store.worker_park(worker, park(key, **ident(RUN_A)))["state"] == "parked"
    assert owner(w, key) == RUN_A
    # A bare run-less FORWARD move changes the state and keeps the owner (the
    # V1-B hazard): parked cards may still move to failed.
    w.store.set_state(w.p("system"), key, "failed")
    assert owner(w, key) == RUN_A
    # sq5 fix 3 (deepseek V1): a bare run-less BACK move on an owned card is
    # refused instead -- failed -> ready is not forward and the card is owned.
    refused("bad_transition", w.store.set_state, w.p("system"), key, "ready")
    assert owner(w, key) == RUN_A
    assert state_of(w, key) == "failed"
    # The live run is still fenced out of every exec-checked worker verb ...
    refused("exec_stale", w.store.worker_park, worker, park(key, **ident(RUN_B)))
    assert owner(w, key) == RUN_A
    # ... until the EXPLICIT hand-back door frees the card for run B.
    w.store.set_state(w.p("router"), key, "open", hand_back=True)
    assert owner(w, key) == NONE
    assert w.store.worker_park(worker, park(key, **ident(RUN_B)))["state"] == "parked"
    assert owner(w, key) == RUN_B
