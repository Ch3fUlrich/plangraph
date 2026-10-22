"""RT-P1-EXEC-OWNERSHIP: forward-only exec ownership on set-state moves.

A set_state move that carries ``run=(workflow_id, run_id, attempt)`` is exec-owned:
an unowned card records its first owner, the same run may only move its attempt
forward, a different run is stale, and a back-move needs ``new_run=True`` with a
fresh run (which takes over ownership).  Every rule here gets a test that fails
if the rule is removed.  Moves without a ``run`` are out of scope (unchanged
behaviour, covered by test_keeper_cards.py).  Portable: runs on the default
backend (``World`` follows ``PG_KEEPER_BACKEND``).
"""
import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def refused(code, fn, *args, **kw):
    with pytest.raises(Refused) as info:
        fn(*args, **kw)
    assert info.value.code == code, info.value.msg
    return info.value


def owner(w, key):
    row = w.conn.execute("SELECT exec_workflow_id, exec_run_id, exec_attempt"
                         " FROM items WHERE key=?", (key,)).fetchone()
    return (row[0], row[1], row[2])


RUN1 = ("wf-1", "run-1", 1)
RUN1_A2 = ("wf-1", "run-1", 2)


# -------------------------------------------------------------- ownership
def test_an_unowned_card_records_its_first_owner(w):
    key, = seed(w, [("A", [])])
    assert owner(w, key) == (None, None, None)      # bare moves never record
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_a_stale_run_is_refused(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "needs_input",
            run=("wf-2", "run-2", 1))
    assert owner(w, key) == ("wf-1", "run-1", 1)    # the stale move changed nothing


def test_a_lower_attempt_is_refused(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running", run=RUN1_A2)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "running",
            run=RUN1)
    assert owner(w, key) == ("wf-1", "run-1", 2)    # the attempt stayed


def test_a_higher_attempt_is_accepted_and_recorded(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    w.store.set_state(w.p("router"), key, "needs_input", run=("wf-1", "run-1", 3))
    assert owner(w, key) == ("wf-1", "run-1", 3)


# ------------------------------------------------------------- transitions
def test_a_back_move_needs_a_new_run(w):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    w.store.set_state(w.p("router"), key, "parked", run=RUN1)     # forward: ok
    refused("bad_transition", w.store.set_state, w.p("router"), key, "open",
            run=RUN1)
    refused("bad_transition", w.store.set_state, w.p("router"), key, "open",
            run=RUN1, new_run=True)          # new_run still needs a FRESH run
    assert owner(w, key) == ("wf-1", "run-1", 1)
    w.store.set_state(w.p("router"), key, "open", run=("wf-2", "run-2", 2),
                      new_run=True)
    assert owner(w, key) == ("wf-2", "run-2", 2)  # higher attempt took over


def test_a_malformed_run_is_refused_and_records_nothing(w):
    key, = seed(w, [("A", [])])
    for bad in (("wf-1", "run-1", "one"), ("wf-1", "run-1"), (None, "run-1", 1)):
        refused("validation", w.store.set_state, w.p("router"), key, "running",
                run=bad)
    assert owner(w, key) == (None, None, None)
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    refused("validation", w.store.set_state, w.p("router"), key, "running",
            run=("wf-1", "run-1", 0))
    assert owner(w, key) == ("wf-1", "run-1", 1)


# ------------------------------------------------- untouched verbs stay bare
def test_claim_and_release_never_touch_exec_ownership(w):
    key, = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    w.store.release(w.p("l1main"), key, epoch)
    assert owner(w, key) == (None, None, None)      # only run-carrying moves record
