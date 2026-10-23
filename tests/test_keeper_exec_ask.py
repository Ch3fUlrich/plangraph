"""RT-P1-EXEC-OWNERSHIP: ``ask_question`` is checked against the exec owner.

An ask that carries ``(workflow_id, run_id, attempt)`` — what the Rust worker
posts with every ``/v1/ask_question`` (rust/worker/src/keeper.rs ``with_exec``)
— is checked against the card's recorded exec owner: the first ask records it,
a different run or a lower attempt is refused as ``exec_stale`` and inserts no
question row, and partial fields are a validation refusal that records nothing.
A worker/spawner that omits all three on an owned card names no run and is
refused as ``exec_stale`` (HTTP 409); router/l1/system callers (the planner,
``planner/service.py``) omit them freely — the kwargs default to ``None``.

Every rule gets a test that fails if the rule is removed: the over-HTTP cases
pin ``routes._ask`` passing the three fields through, the store-level cases pin
``store_cards.ask_question`` itself.  Fixtures/helpers are the ones
``test_keeper_exec_verbs.py`` and ``test_inbox_signal.py`` use; portable: runs
on the default backend (``World`` follows ``PG_KEEPER_BACKEND``).
"""
import pytest

from door_util import Doors
from keeper_util import World, seed, seed_exec_owner
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


@pytest.fixture
def doors(w, tmp_path):
    live = Doors(w, tmp_path)
    try:
        yield live
    finally:
        live.close()


def ask(key, **extra):
    """An ``/v1/ask_question`` body; ``extra`` carries the exec fields."""
    body = {"key": key, "text": "which branch?"}
    body.update(extra)
    return body


def refused(code, fn, *args, **kw):
    with pytest.raises(Refused) as info:
        fn(*args, **kw)
    assert info.value.code == code, info.value.msg
    return info.value


def owner(w, key):
    row = w.conn.execute("SELECT exec_workflow_id, exec_run_id, exec_attempt"
                         " FROM items WHERE key=?", (key,)).fetchone()
    return (row[0], row[1], row[2])


def questions(w, key):
    return w.conn.execute("SELECT id FROM questions WHERE key=? ORDER BY id",
                          (key,)).fetchall()


RUN1 = {"workflow_id": "wf-1", "run_id": "run-1", "attempt": 1}
RUN1_A2 = {"workflow_id": "wf-1", "run_id": "run-1", "attempt": 2}
RUN2 = {"workflow_id": "wf-2", "run_id": "run-2", "attempt": 1}
OWNED = ("wf-1", "run-1", 1)


# -------------------------------------------------- over the HTTP door
def test_worker_ask_with_fields_records_the_exec_owner(doors, w):
    key, = seed(w, [("A", [])])
    status, body = doors.http("workerA", "POST", "/v1/ask_question",
                              ask(key, **RUN1))
    assert (status, body["state"], body["key"]) == (200, "open", key)
    assert owner(w, key) == OWNED
    assert len(questions(w, key)) == 1


def test_worker_ask_with_a_different_run_is_refused_and_inserts_no_question(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/ask_question",
                      ask(key, **RUN1))[0] == 200
    status, body = doors.http("workerA", "POST", "/v1/ask_question",
                              ask(key, **RUN2))
    assert (status, body["error"]) == (409, "exec_stale")
    assert len(questions(w, key)) == 1        # the stale ask inserted no row
    assert owner(w, key) == OWNED


def test_worker_ask_with_a_lower_attempt_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/ask_question",
                      ask(key, **RUN1_A2))[0] == 200
    status, body = doors.http("workerA", "POST", "/v1/ask_question",
                              ask(key, **RUN1))
    assert (status, body["error"]) == (409, "exec_stale")
    assert len(questions(w, key)) == 1
    assert owner(w, key) == ("wf-1", "run-1", 2)   # the attempt did not go back


def test_worker_ask_with_partial_fields_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    status, body = doors.http("workerA", "POST", "/v1/ask_question",
                              ask(key, run_id="run-1"))
    assert (status, body["error"]) == (400, "validation")
    assert questions(w, key) == []
    assert owner(w, key) == (None, None, None)     # nothing half-recorded


def test_worker_ask_without_fields_on_an_owned_card_is_refused(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/ask_question",
                      ask(key, **RUN1))[0] == 200
    status, body = doors.http("workerA", "POST", "/v1/ask_question", ask(key))
    assert (status, body["error"]) == (409, "exec_stale")
    assert "has an exec owner" in body["msg"]
    assert len(questions(w, key)) == 1        # the refused ask inserted no row
    assert owner(w, key) == OWNED


def test_worker_ask_without_fields_on_an_unowned_card_works_as_before(doors, w):
    key, = seed(w, [("A", [])])
    status, body = doors.http("workerA", "POST", "/v1/ask_question", ask(key))
    assert (status, body["state"]) == (200, "open")
    assert len(questions(w, key)) == 1
    assert owner(w, key) == (None, None, None)     # an omission records nothing


def test_router_ask_without_fields_on_an_owned_card_still_works(doors, w):
    key, = seed(w, [("A", [])])
    assert doors.http("workerA", "POST", "/v1/ask_question",
                      ask(key, **RUN1))[0] == 200  # the card gets an owner
    status, body = doors.http("router", "POST", "/v1/ask_question", ask(key))
    assert (status, body["state"]) == (200, "open")
    assert len(questions(w, key)) == 2
    assert owner(w, key) == OWNED                  # the owner is untouched


# ------------------------------------------------------ store-level verbs
def test_store_ask_with_fields_records_the_owner_and_refuses_a_stale_run(w):
    key, = seed(w, [("A", [])])
    worker = w.p("workerA")
    asked = w.store.ask_question(worker, key, "first?", exec_workflow_id="wf-1",
                                 exec_run_id="run-1", exec_attempt=2)
    assert asked["state"] == "open"
    assert owner(w, key) == ("wf-1", "run-1", 2)
    refused("exec_stale", w.store.ask_question, worker, key, "stale?",
            exec_workflow_id="wf-2", exec_run_id="run-2", exec_attempt=1)
    refused("exec_stale", w.store.ask_question, worker, key, "backwards?",
            exec_workflow_id="wf-1", exec_run_id="run-1", exec_attempt=1)
    assert len(questions(w, key)) == 1        # neither refusal inserted a row
    assert owner(w, key) == ("wf-1", "run-1", 2)


def test_store_worker_ask_without_fields_on_an_owned_card_is_refused(w):
    key, = seed(w, [("A", [])])
    # Only a worker may RECORD an owner now (V2-1): seed it, then let the
    # run-carrying router move only check the triple.
    seed_exec_owner(w, key, OWNED)
    w.store.set_state(w.p("router"), key, "running",
                      run=("wf-1", "run-1", 1))
    err = refused("exec_stale", w.store.ask_question, w.p("workerA"), key, "q?")
    assert "has an exec owner" in err.msg
    assert questions(w, key) == []
    assert owner(w, key) == OWNED


def test_store_router_l1_and_system_omit_the_fields_on_an_owned_card(w):
    key, = seed(w, [("A", [])])
    seed_exec_owner(w, key, OWNED)
    w.store.set_state(w.p("router"), key, "running",
                      run=("wf-1", "run-1", 1))
    asked = w.store.ask_question(w.p("router"), key, "tabs or spaces?")
    lead = w.store.ask_question(w.p("l1main"), key, "how long?")
    ops = w.store.ask_question(w.p("system"), key, "ship it?")
    assert [out["state"] for out in (asked, lead, ops)] == ["open"] * 3
    assert len(questions(w, key)) == 3
    assert owner(w, key) == OWNED              # omitting never touches the owner
