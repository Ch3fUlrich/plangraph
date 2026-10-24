"""W2 store verbs: cards, attempts, decisions, questions/answers, budget, set_state.

Portable: runs on the default backend (``World`` follows ``PG_KEEPER_BACKEND``), so
the Postgres CI job exercises the same assertions.  Rows are read back through the
store or ``conn.execute`` only.
"""
import json

import pytest
from card_path_cases import CHECK_PATH_CASES

from keeper_util import World, pass_review, seed, to_claimed
from plangraph.keeper import db_errors, events as keeper_events
from plangraph.keeper import schema
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def card(**kw):
    base = {"outcome": "add the thing", "write_set": ["src/a.py", "tests/test_a.py"],
            "read_set": ["src/b.py"], "check_cmd": "pytest -q tests/test_a.py",
            "window_class": "128k", "risk_tags": ["migration"], "estimate_tokens": 40000}
    base.update(kw)
    return base


def refused(code, fn, *args, **kw):
    with pytest.raises(Refused) as info:
        fn(*args, **kw)
    assert info.value.code == code, info.value.msg
    return info.value


def count(w, table):
    return w.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------------- cards
@pytest.mark.parametrize("path", ["/abs/path", "../a", "a/../b", "", " ",
                                  "\\root", "C:\\root", "C:relative", "."])
def test_check_reads_refuses_unsafe_paths(w, path):
    (key,) = seed(w, [("A", [])])
    refused("validation", w.store.put_card, w.p("router"), key,
            card(check_reads=[path]))


def test_check_reads_normalises_relative_paths(w):
    (key,) = seed(w, [("A", [])])
    w.store.put_card(w.p("router"), key, card(check_reads=["./tests\\test_a.py"]))
    assert w.store.get_card(w.p("reader"), key)["check_reads"] == ["tests/test_a.py"]


def test_other_keeper_path_lists_keep_existing_behavior(w):
    (key,) = seed(w, [("A", [])])
    fields = {name: ["../legacy"] for name in ("read_set", "write_set", "forbid_paths")}
    w.store.put_card(w.p("router"), key, card(**fields))
    got = w.store.get_card(w.p("reader"), key)
    for name, value in fields.items():
        assert got[name] == value


@pytest.mark.parametrize("path,valid", CHECK_PATH_CASES)
def test_planner_and_keeper_agree_on_check_paths(w, path, valid):
    from plangraph.planner.card import CardError, parse_card

    (key,) = seed(w, [("A", [])])
    value = card(check_reads=[path])
    if valid:
        parsed = parse_card(value)
        w.store.put_card(w.p("router"), key, value)
        assert w.store.get_card(w.p("reader"), key)["check_reads"] == list(parsed.check_reads)
    else:
        with pytest.raises(CardError):
            parse_card(value)
        refused("validation", w.store.put_card, w.p("router"), key, value)


def test_put_and_get_card_roundtrip(w):
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    assert w.store.put_card(router, key, card()) == {"key": key}
    got = w.store.get_card(w.p("reader"), key)
    assert got["outcome"] == "add the thing"
    assert got["write_set"] == ["src/a.py", "tests/test_a.py"]
    assert got["read_set"] == ["src/b.py"]
    assert got["exports"] == [] and got["forbid_paths"] == [] and got["smoke_checks"] == []
    assert got["risk_tags"] == ["migration"]
    assert got["window_class"] == "128k" and got["estimate_tokens"] == 40000
    assert got["heldout_check"] is None
    assert got["check_kind"] == "change"
    assert got["check_reads"] == []


def test_regression_check_roundtrip(w):
    (key,) = seed(w, [("A", [])])
    w.store.put_card(w.p("router"), key, card(
        check_kind="regression", check_reads=["tests/test_existing.py"]))
    got = w.store.get_card(w.p("reader"), key)
    assert got["check_kind"] == "regression"
    assert got["check_reads"] == ["tests/test_existing.py"]


def test_put_card_replaces(w):
    (key,) = seed(w, [("A", [])])
    w.store.put_card(w.p("router"), key, card())
    w.clock.advance(5)
    w.store.put_card(w.p("l1main"), key, card(outcome="v2", write_set=["x"], risk_tags=[],
                                              window_class="1m", check_cmd=None))
    got = w.store.get_card(w.p("router"), key)
    assert (got["outcome"], got["write_set"], got["risk_tags"]) == ("v2", ["x"], [])
    assert got["window_class"] == "1m" and got["check_cmd"] is None
    assert count(w, "cards") == 1


@pytest.mark.parametrize("bad", [
    {"window_class": "512k"}, {"risk_tags": ["vibes"]}, {"estimate_tokens": -1},
    {"estimate_tokens": True}, {"write_set": "src/a.py"}, {"write_set": [""]},
    {"outcome": "  "}, {"unknown_field": 1}, {"check_cmd": 7},
    {"check_kind": "other"}, {"check_reads": "tests/a.py"},
])
def test_put_card_validates(w, bad):
    (key,) = seed(w, [("A", [])])
    refused("validation", w.store.put_card, w.p("router"), key, card(**bad))
    assert count(w, "cards") == 0


def test_card_authorisation_and_locks(w):
    a, b = seed(w, [("A", []), ("B", [])])
    refused("forbidden", w.store.put_card, w.p("workerA"), a, card())
    refused("forbidden", w.store.put_card, w.p("l1other"), a, card())      # other project
    refused("not_found", w.store.put_card, w.p("router"), "demo:NOPE", card())
    refused("not_found", w.store.get_card, w.p("router"), a)               # no card yet
    w.store.cancel(w.p("router"), b, "dropped")
    refused("locked", w.store.put_card, w.p("router"), b, card())


# ---------------------------------------------------------------- attempts
def attempt(**kw):
    base = {"harness": "claude", "model": "m-1", "combo": "c1", "class": "256k",
            "started_at": "2026-10-15T10:00:00Z", "ended_at": "2026-10-15T10:05:00Z",
            "exit_class": "check_fail", "tokens_in": 1000, "tokens_out": 200,
            "cost": 0.25, "run_id": "run-1", "workflow_id": "wf-1", "log_ref": "logs/1"}
    base.update(kw)
    return base


def test_record_attempt_numbers_and_stores(w):
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    first = w.store.record_attempt(router, key, attempt())
    second = w.store.record_attempt(w.p("l1main"), key, attempt(exit_class="ok"))
    assert (first["n"], second["n"]) == (1, 2)
    assert second["id"] == first["id"] + 1
    rows = w.conn.execute("SELECT n, exit_class, class, cost FROM attempts WHERE key=?"
                          " ORDER BY n", (key,)).fetchall()
    assert [(r["n"], r["exit_class"], r["class"], r["cost"]) for r in rows] == [
        (1, "check_fail", "256k", 0.25), (2, "ok", "256k", 0.25)]


@pytest.mark.parametrize("exit_class", schema.EXIT_CLASSES)
def test_every_exit_class_is_accepted(w, exit_class):
    (key,) = seed(w, [("A", [])])
    assert w.store.record_attempt(w.p("router"), key, attempt(exit_class=exit_class))["n"] == 1


def test_record_attempt_rejects(w):
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    refused("validation", w.store.record_attempt, router, key, attempt(exit_class="exploded"))
    refused("validation", w.store.record_attempt, router, key, attempt(exit_class=None))
    refused("validation", w.store.record_attempt, router, key, attempt(n=0))
    refused("validation", w.store.record_attempt, router, key, attempt(tokens_in=-3))
    refused("validation", w.store.record_attempt, router, key,
            attempt(started_at="not a time"))
    refused("validation", w.store.record_attempt, router, key,
            attempt(ended_at="2026-10-15T09:00:00Z"))
    refused("validation", w.store.record_attempt, router, key, attempt(extra=1))
    refused("forbidden", w.store.record_attempt, w.p("workerA"), key, attempt())
    refused("not_found", w.store.record_attempt, router, "demo:NOPE", attempt())
    assert count(w, "attempts") == 0
    w.store.record_attempt(router, key, attempt(n=2))
    refused("conflict", w.store.record_attempt, router, key, attempt(n=2))


def test_attempts_are_append_only(w):
    (key,) = seed(w, [("A", [])])
    w.store.record_attempt(w.p("router"), key, attempt())
    with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
        w.conn.execute("UPDATE attempts SET exit_class='ok'")
    with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
        w.conn.execute("DELETE FROM attempts")
    assert count(w, "attempts") == 1


def test_attempt_exit_class_check_constraint(w):
    (key,) = seed(w, [("A", [])])
    with pytest.raises(db_errors.INTEGRITY):
        w.conn.execute(
            "INSERT INTO attempts(id,key,n,exit_class) VALUES(1,?,1,'bogus')", (key,))


# --------------------------------------------------------------- decisions
def test_add_decision_and_append_only(w):
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    out = w.store.add_decision(router, "D-1285", "operator", "go ahead", closes=[key],
                               waives=["demo:X"])
    assert out == {"id": "D-1285", "kind": "operator"}
    w.store.add_decision(router, "D-1286", "judge", "ruling")
    row = w.conn.execute("SELECT actor, kind, text, closes, waives FROM decisions"
                         " WHERE id='D-1285'").fetchone()
    assert (row["actor"], row["kind"], row["text"]) == ("router", "operator", "go ahead")
    assert json.loads(row["closes"]) == [key] and json.loads(row["waives"]) == ["demo:X"]
    refused("conflict", w.store.add_decision, router, "D-1285", "judge", "again")
    refused("validation", w.store.add_decision, router, "D-1", "owner", "x")
    refused("validation", w.store.add_decision, router, "bad id", "judge", "x")
    refused("validation", w.store.add_decision, router, "D-2", "judge", "")
    refused("forbidden", w.store.add_decision, w.p("l1main"), "D-3", "judge", "x")
    with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
        w.conn.execute("UPDATE decisions SET text='edited'")
    with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
        w.conn.execute("DELETE FROM decisions")


# --------------------------------------------------------------- questions
def test_question_answer_flow(w):
    (key,) = seed(w, [("A", [])])
    asked = w.store.ask_question(w.p("workerA"), key, "which branch?")
    assert asked["state"] == "open"
    qid = asked["id"]
    refused("forbidden", w.store.answer_question, w.p("workerA"), qid, "main")
    out = w.store.answer_question(w.p("router"), qid, "main")
    assert out == {"question_id": qid, "state": "answered", "source": "answer",
                   "text": "main"}
    ans = w.conn.execute("SELECT actor, source, text FROM answers WHERE question_id=?",
                         (qid,)).fetchone()
    assert (ans["actor"], ans["source"], ans["text"]) == ("router", "answer", "main")
    assert w.conn.execute("SELECT state FROM questions WHERE id=?",
                          (qid,)).fetchone()[0] == "answered"
    refused("conflict", w.store.answer_question, w.p("router"), qid, "again")
    with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
        w.conn.execute("UPDATE answers SET text='edited'")
    with pytest.raises(db_errors.INTEGRITY + db_errors.DENIED):
        w.conn.execute("DELETE FROM answers")


def test_question_timeout_takes_the_default_after_the_deadline(w):
    (key,) = seed(w, [("A", [])])
    due = "2026-11-14T22:13:20Z"            # three years after the fixed test clock
    soon = w.store.ask_question(w.p("router"), key, "tabs or spaces?",
                                default_answer="spaces", deadline=due)
    qid = soon["id"]
    refused("bad_state", w.store.answer_question, w.p("system"), qid,
            source="question_timeout")
    w.clock.t = w.store._parse_iso(due).timestamp()
    refused("validation", w.store.answer_question, w.p("system"), qid, "tabs",
            source="question_timeout")                       # the default, not a new text
    out = w.store.answer_question(w.p("system"), qid, source="question_timeout")
    assert (out["state"], out["text"], out["source"]) == ("timeout", "spaces",
                                                         "question_timeout")
    ans = w.conn.execute("SELECT source, text, actor FROM answers WHERE question_id=?",
                         (qid,)).fetchone()
    assert (ans["source"], ans["text"], ans["actor"]) == ("question_timeout", "spaces",
                                                          "system")


def test_question_without_default_never_times_out(w):
    (key,) = seed(w, [("A", [])])
    qid = w.store.ask_question(w.p("router"), key, "ok?",
                               deadline="2020-01-01T00:00:00Z")["id"]
    refused("validation", w.store.answer_question, w.p("system"), qid,
            source="question_timeout")
    assert w.conn.execute("SELECT state FROM questions WHERE id=?",
                          (qid,)).fetchone()[0] == "open"


@pytest.mark.parametrize("tag", ["auth", "secrets", "money", "live-step", "public-release"])
def test_risk_tagged_question_has_no_default(w, tag):
    (key,) = seed(w, [("A", [])])
    refused("validation", w.store.ask_question, w.p("router"), key, "rotate it?",
            default_answer="yes", deadline="2030-01-01T00:00:00Z", risk_tags=[tag])
    ok = w.store.ask_question(w.p("router"), key, "rotate it?", risk_tags=[tag])
    refused("validation", w.store.answer_question, w.p("router"), ok["id"],
            source="question_timeout")


def test_question_validation(w):
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    refused("validation", w.store.ask_question, router, key, "")
    refused("validation", w.store.ask_question, router, key, "q", risk_tags=["vibes"])
    refused("validation", w.store.ask_question, router, key, "q", default_answer="d")
    refused("validation", w.store.ask_question, router, key, "q", deadline="soon")
    refused("not_found", w.store.ask_question, router, "demo:NOPE", "q")
    refused("forbidden", w.store.ask_question, w.p("reader"), key, "q")
    refused("forbidden", w.store.ask_question, w.p("l1other"), key, "q")
    refused("not_found", w.store.answer_question, router, 999, "x")
    refused("validation", w.store.answer_question, router, 1, "x", source="whim")
    assert count(w, "questions") == 0


# ------------------------------------------------------------------ budget
def test_set_budget_upserts_per_provider_and_day(w):
    router = w.p("router")
    w.store.set_budget(router, "claude", "2026-10-15", 1000, 1.5, meter=0.25)
    w.store.set_budget(w.p("system"), "claude", "2026-10-16", 5, 0.0)
    w.store.set_budget(router, "claude", "2026-10-15", 2500, 3.0, meter=0.5)
    rows = w.conn.execute("SELECT provider, day, tokens, cost, meter FROM budget"
                          " ORDER BY day").fetchall()
    assert [tuple(r) for r in rows] == [("claude", "2026-10-15", 2500, 3.0, 0.5),
                                        ("claude", "2026-10-16", 5, 0.0, None)]
    refused("validation", w.store.set_budget, router, "claude", "15.10.2026", 1, 1)
    refused("validation", w.store.set_budget, router, "claude", "2026-10-15", -1, 1)
    refused("validation", w.store.set_budget, router, "claude", "2026-10-15", 1, float("nan"))
    refused("validation", w.store.set_budget, router, "", "2026-10-15", 1, 1)
    refused("forbidden", w.store.set_budget, w.p("l1main"), "claude", "2026-10-15", 1, 1)
    assert count(w, "budget") == 2


# ------------------------------------------------------------------ states
def test_states_tuple_and_event_type():
    assert schema.STATES == ("open", "ready", "claimed", "in_review", "done", "cancelled",
                             "running", "needs_input", "waiting", "failed", "parked")
    assert "state_set" in schema.EVENT_TYPES


def test_set_state_moves_item_and_folds(w):
    (key,) = seed(w, [("A", [])])
    router = w.p("router")
    head = keeper_events.read_events(w.conn)[-1]["seq"]
    assert w.store.set_state(router, key, "running", "attempt 1") == {
        "key": key, "state": "running", "changed": True}
    for state in ("needs_input", "waiting", "failed", "parked", "ready", "open"):
        w.clock.advance(1)
        w.store.set_state(w.p("l1main"), key, state)
    events = [e for e in keeper_events.read_events(w.conn) if e["seq"] > head]
    assert [e["type"] for e in events] == ["state_set"] * 7
    assert events[0]["payload"] == {"state": "running", "was": "open", "reason": "attempt 1"}
    assert w.store.set_state(router, key, "open") == {"key": key, "state": "open",
                                                      "changed": False}
    assert len(keeper_events.read_events(w.conn)) == head + 7           # no-op: no event
    assert keeper_events.verify_chain(w.conn) == []
    assert keeper_events.verify_integrity(w.conn) == []
    w.store.set_state(router, key, "parked")
    assert w.store._item(key)["state"] == "parked"
    assert keeper_events.verify_integrity(w.conn) == []


def test_set_state_refusals(w):
    a, b = seed(w, [("A", []), ("B", [])])
    router = w.p("router")
    for state in ("claimed", "in_review", "done", "cancelled", "bogus", None):
        refused("validation", w.store.set_state, router, a, state)
    refused("forbidden", w.store.set_state, w.p("workerA"), a, "running")
    refused("forbidden", w.store.set_state, w.p("l1other"), a, "running")
    refused("not_found", w.store.set_state, router, "demo:NOPE", "running")
    epoch = to_claimed(w, a)
    refused("bad_state", w.store.set_state, router, a, "running")      # claimed: use release
    w.store.cancel(router, b, "dropped")
    refused("locked", w.store.set_state, router, b, "running")
    w.store.add_evidence(w.p("l1main"), a, epoch, "decision", {"id": "D-1"})
    w.store.submit(w.p("l1main"), a, epoch)
    pass_review(w, a)
    refused("locked", w.store.set_state, router, a, "failed")


def test_new_states_do_not_break_validation_or_cancel(w):
    a, b = seed(w, [("A", []), ("B", [])])
    router = w.p("router")
    w.store.set_state(router, a, "parked")
    w.store.set_priority(router, b, 2)                                # unrelated write still fine
    w.store.edit_definition(router, b, {"title": "renamed b"})          # validates the graph
    w.store.cancel(router, a, "parked card dropped")
    assert w.store._item(a)["state"] == "cancelled"
    assert keeper_events.verify_integrity(w.conn) == []
