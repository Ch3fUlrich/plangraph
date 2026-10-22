"""RT-P1-EXEC-OWNERSHIP: forward-only exec ownership on set-state moves.

A set_state move that carries ``run=(workflow_id, run_id, attempt)`` is exec-owned:
an unowned card records its first owner, the same run may only move its attempt
forward, a different run is stale, and run-carrying back-moves are refused.
Every rule here gets a test that fails
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
@pytest.mark.parametrize("terminal", ["failed", "parked"])
@pytest.mark.parametrize("target", ["open", "ready"])
def test_a_back_move_is_refused_even_with_a_new_run(w, terminal, target):
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    w.store.set_state(w.p("router"), key, terminal, run=RUN1)
    refused("bad_transition", w.store.set_state, w.p("router"), key, target,
            run=RUN1)
    refused("bad_transition", w.store.set_state, w.p("router"), key, target,
            run=RUN1, new_run=True)
    refused("bad_transition", w.store.set_state, w.p("router"), key, target,
            run=("wf-2", "run-2", 1), new_run=True)
    assert owner(w, key) == ("wf-1", "run-1", 1)


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


@pytest.mark.parametrize("door", ["release", "reap"])
def test_claim_reopen_uses_owner_clear_helper(w, monkeypatch, door):
    key, = seed(w, [("A", [])])
    epoch = to_claimed(w, key)
    with w.store._write():
        w.store._exec_record(w.conn, key, *RUN1)
    calls = []
    clear = w.store._exec_clear

    def tracked_clear(conn, item_key):
        calls.append((conn, item_key))
        clear(conn, item_key)

    monkeypatch.setattr(w.store, "_exec_clear", tracked_clear)
    if door == "release":
        w.store.release(w.p("l1main"), key, epoch)
    else:
        lease = w.conn.execute("SELECT lease_until FROM items WHERE key=?",
                               (key,)).fetchone()[0]
        w.store.reap(w.p("system"),
                     now=w.store._parse_iso(lease).timestamp() + 1)
    assert calls == [(w.conn, key)]
    assert owner(w, key) == (None, None, None)
