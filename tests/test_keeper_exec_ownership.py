"""RT-P1-EXEC-OWNERSHIP: forward-only exec ownership on set-state moves.

A set_state move that carries ``run=(workflow_id, run_id, attempt)`` is exec-owned:
an unowned card records its first owner -- only from the worker principal that
holds the card's Temporal workflow, bound to the principal (sq5 fix 1, V2-1) --
the same run may only move its attempt forward, a different run is stale, and
run-carrying back-moves are refused.  Bare run-less moves forward-check an OWNED
card too (sq5 fix 3): a back move on an owned card without ``run``/``hand_back``
is ``bad_transition``, while an unowned card keeps the historical run-less
behaviour.  Every rule here gets a test that fails
if the rule is removed.  Portable: runs on the default backend (``World``
follows ``PG_KEEPER_BACKEND``).
"""
import json

import pytest

from keeper_util import World, seed, to_claimed
from plangraph.keeper.errors import Refused
from test_keeper_exec_verbs import park


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


def owner_principal(w, key):
    """The principal bound to the recorded exec owner (RT-P1 V2-1)."""
    row = w.conn.execute("SELECT exec_principal FROM items WHERE key=?",
                         (key,)).fetchone()
    return row[0]


def retired_runs(w, key):
    """The retired-run fence kept across owner clears (RT-P1 V1-1)."""
    row = w.conn.execute("SELECT exec_retired_runs FROM items WHERE key=?",
                         (key,)).fetchone()
    return [(entry["workflow_id"], entry["run_id"], entry["attempt"])
            for entry in json.loads(row[0] or "[]")]


def seed_owner(w, key, run, who="workerA"):
    """Record *run* as the owner the way production does: a worker's ask.

    Only the worker principal may record an owner now (V2-1), so tests seed
    ownership through an exec-carrying ask from the worker principal instead
    of a router ``set_state(run=...)``.
    """
    if w.store._item(key)["claimed_by"] is None:
        to_claimed(w, key, who)
    w.store.ask_question(w.p(who), key, "Which path?",
                         exec_workflow_id=run[0], exec_run_id=run[1],
                         exec_attempt=run[2])
    assert owner(w, key) == run
    assert owner_principal(w, key) == who
    w.conn.execute("UPDATE items SET state='open' WHERE key=?", (key,))
    w.conn.commit()


RUN1 = ("wf-1", "run-1", 1)
RUN1_A2 = ("wf-1", "run-1", 2)


# -------------------------------------------------------------- ownership
def test_an_unowned_card_records_its_first_owner(w):
    key, = seed(w, [("A", [])])
    from keeper_util import to_claimed
    to_claimed(w, key, "workerA")
    assert owner(w, key) == (None, None, None)      # bare moves never record
    w.store.ask_question(w.p("workerA"), key, "Which path?",
                         exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
                         exec_attempt=RUN1[2])
    assert owner(w, key) == ("wf-1", "run-1", 1)
    assert owner_principal(w, key) == "workerA"      # bound to the recording worker


def test_a_stale_run_is_refused(w):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-1", "run-1", 1))
    seed_owner(w, key, RUN1)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "needs_input",
            run=("wf-2", "run-2", 1))
    assert owner(w, key) == ("wf-1", "run-1", 1)    # the stale move changed nothing


def test_a_lower_attempt_is_refused(w):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-1", "run-1", 1))
    seed_owner(w, key, RUN1_A2)
    refused("exec_stale", w.store.set_state, w.p("router"), key, "running",
            run=RUN1)
    assert owner(w, key) == ("wf-1", "run-1", 2)    # the attempt stayed


def test_a_higher_attempt_is_accepted_and_recorded(w):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-1", "run-1", 1))
    seed_owner(w, key, RUN1)
    w.store.set_state(w.p("router"), key, "needs_input", run=("wf-1", "run-1", 3))
    assert owner(w, key) == ("wf-1", "run-1", 3)


# ------------------------------------------------------------- transitions
@pytest.mark.parametrize("terminal", ["failed", "parked"])
@pytest.mark.parametrize("target", ["open", "ready"])
def test_a_back_move_is_refused_even_with_a_new_run(w, terminal, target):
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-1", "run-1", 1))
    seed_owner(w, key, RUN1)
    w.store.set_state(w.p("router"), key, "running", run=RUN1)
    w.store.set_state(w.p("router"), key, terminal, run=RUN1)
    refused("bad_transition", w.store.set_state, w.p("router"), key, target,
            run=RUN1)
    refused("validation", w.store.set_state, w.p("router"), key, target,
            run=RUN1, new_run=True)
    refused("validation", w.store.set_state, w.p("router"), key, target,
            run=("wf-2", "run-2", 1), new_run=True)
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_a_malformed_run_is_refused_and_records_nothing(w):
    key, = seed(w, [("A", [])])
    for bad in (("wf-1", "run-1", "one"), ("wf-1", "run-1"), (None, "run-1", 1)):
        refused("validation", w.store.set_state, w.p("router"), key, "running",
                run=bad)
    assert owner(w, key) == (None, None, None)
    seed_owner(w, key, RUN1)
    refused("validation", w.store.set_state, w.p("router"), key, "running",
            run=("wf-1", "run-1", 0))
    assert owner(w, key) == ("wf-1", "run-1", 1)


def test_a_bare_back_move_on_an_owned_card_is_refused(w):
    """RT-P1 sq5 fix 3: the forward table also guards bare set_state on OWNED cards.

    A router/system run-less move that walks an owned card backwards (no
    ``run``, no ``hand_back``) is ``bad_transition`` -- it used to succeed and
    strand the card open but still owned.  The explicit ``hand_back=True``
    door remains the way back.
    """
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-1", "run-1", 1))
    assert w.store.worker_park(w.p("workerA"),
                               park(key, workflow_id=RUN1[0],
                                   run_id=RUN1[1],
                                   attempt=RUN1[2]))["state"] == "parked"
    assert owner(w, key) == RUN1
    refused("bad_transition", w.store.set_state, w.p("router"), key, "open")
    assert w.store._item(key)["state"] == "parked"   # nothing moved
    assert owner(w, key) == RUN1                     # nothing stranded either
    # the explicit hand-back door still frees the card
    w.store.set_state(w.p("router"), key, "open", hand_back=True)
    assert owner(w, key) == (None, None, None)


def test_a_bare_back_move_on_an_unowned_card_keeps_history(w):
    """The bare-move forward table is scoped to OWNED cards (sq5 fix 3 scope).

    An unowned card keeps the historical run-less behaviour: projector/planner
    writes may land anywhere without naming a run.
    """
    key, = seed(w, [("A", [])])
    w.store.set_state(w.p("router"), key, "parked")   # open -> parked, no run
    w.store.set_state(w.p("router"), key, "open")     # back move, no run
    assert owner(w, key) == (None, None, None)


# ------------------------------------------------------- principal binding
def test_only_a_worker_principal_may_record_an_owner(w):
    """RT-P1 sq5 fix 1 (V2-1): recording is the Temporal worker's alone."""
    key, = seed(w, [("A", [])])
    for who in ("router", "l1main", "system"):
        refused("forbidden", w.store.set_state, w.p(who), key, "running",
                run=RUN1)
        assert owner(w, key) == (None, None, None)
        refused("forbidden", w.store.ask_question, w.p(who), key, "Which path?",
                exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
                exec_attempt=RUN1[2])
        assert owner(w, key) == (None, None, None)
    assert owner_principal(w, key) is None


def test_a_spawner_cannot_record_an_owner(w):
    """V2-1 named spawner tokens as planters; they are refused too."""
    from plangraph.keeper import auth
    key, = seed(w, [("A", [])])
    token = auth.add_principal(w.conn, "spawner1", "spawner", ["demo"])
    spawner = auth.authenticate(w.conn, token)
    refused("forbidden", w.store.ask_question, spawner, key, "Which path?",
            exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
            exec_attempt=RUN1[2])
    assert owner(w, key) == (None, None, None)
    assert owner_principal(w, key) is None


def test_a_different_worker_cannot_record_on_an_unowned_card(w):
    """V2-1's hole: a worker that does not hold the card's workflow plants nothing."""
    key, = seed(w, [("A", [])])
    to_claimed(w, key, "workerA")          # workerA holds the card's workflow
    refused("forbidden", w.store.ask_question, w.p("workerB"), key,
            "Which path?", exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
            exec_attempt=RUN1[2])
    assert owner(w, key) == (None, None, None)     # refused, NOT recorded
    # the holder itself records the owner, bound to its principal
    w.store.ask_question(w.p("workerA"), key, "Which path?",
                         exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
                         exec_attempt=RUN1[2])
    assert owner(w, key) == RUN1
    assert owner_principal(w, key) == "workerA"


def test_the_owner_is_bound_to_its_recording_principal(w):
    """A different worker presenting the recorded run on an owned card is refused."""
    key, = seed(w, [("A", [])])
    from keeper_util import seed_exec_owner
    seed_exec_owner(w, key, ("wf-1", "run-1", 1))
    seed_owner(w, key, RUN1)                # workerA records
    # workerB naming the recorded run: refused, the owner principal differs
    refused("forbidden", w.store.ask_question, w.p("workerB"), key,
            "Again?", exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
            exec_attempt=RUN1[2])
    # workerB naming ANOTHER run: stale decides first, as everywhere
    refused("exec_stale", w.store.ask_question, w.p("workerB"), key,
            "Other?", exec_workflow_id="wf-2", exec_run_id="run-2",
            exec_attempt=1)
    # the owner principal keeps working its own card
    w.store.ask_question(w.p("workerA"), key, "Again?",
                         exec_workflow_id=RUN1[0], exec_run_id=RUN1[1],
                         exec_attempt=RUN1[2])
    # router/system admin checks still run against the owner (they cannot record)
    w.store.set_state(w.p("router"), key, "ready", run=RUN1)
    assert owner(w, key) == RUN1
    assert owner_principal(w, key) == "workerA"


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
        w.store._exec_record(w.conn, key, *RUN1, w.p("workerA"))
    calls = []
    clear = w.store._exec_clear

    def tracked_clear(conn, item_key, principal, now):
        calls.append((conn, item_key))
        clear(conn, item_key, principal, now)

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


# ------------------------------------------------- the fence never forgets
def check_retired_fence_remembers_every_run(w):
    """RT-P1 sq12 fix 1: the retired-run fence has no bound (V1-1/V5-1).

    ``_exec_clear`` used to keep only ``retired[-16:]``, so after 17 clears a
    run retired earliest was accepted again and could reacquire the card as a
    first writer.  20 clear cycles later the FIRST retired run must still be
    refused -- through ``_exec_check`` and end to end through a worker park.
    Backend-agnostic body: ``tests/test_keeper_pg_exec_fence.py`` runs the same
    checks against Postgres (the sq10/sq11 ``check_*`` pattern).
    """
    key, = seed(w, [("A", [])])
    w.store.claim(w.p("workerA"), key)
    for n in range(20):
        with w.store._write():
            w.store._exec_check(w.conn, key, "wf", f"run-{n}", 1,
                                w.p("workerA"), now=w.clock())
            w.store._exec_clear(w.conn, key, w.p("router"), w.clock())
    assert len(retired_runs(w, key)) == 20        # the fence keeps every run
    with w.store._write():
        refused("exec_stale", w.store._exec_check, w.conn, key, "wf", "run-0",
                1, w.p("workerA"))
    refused("exec_stale", w.store.worker_park, w.p("workerA"),
            park(key, workflow_id="wf", run_id="run-0", attempt=2))


def test_retired_fence_remembers_every_run_after_twenty_clears(w):
    # Portable: runs on the default backend (``World`` follows
    # ``PG_KEEPER_BACKEND``); the PG variant lives in
    # ``tests/test_keeper_pg_exec_fence.py`` and calls the same helper.
    check_retired_fence_remembers_every_run(w)
