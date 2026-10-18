"""Contract tests for the keeper's Jev shadow verbs (``store_jev.py``).

Jev (a free model) answers the rules-router's routing / QC-accept questions in
SHADOW; its rows land in ``jev_shadow`` and gain exactly one later column,
``outcome`` -- first write wins.  These tests drive the store verbs directly and
read rows back with ``store.jev_rows``; all SQL is portable, so the file runs on
SQLite and Postgres.
"""
import json

import pytest

from keeper_util import World, seed
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    try:
        yield world
    finally:
        world.close()


def row(key, **over):
    """A valid ``jev_record`` body for *key* with every *over* applied."""
    body = {"card": key, "kind": "harness", "rule_answer": "rules",
            "jev_answer": "jev", "status": "ok"}
    body.update(over)
    return body


def add_spawner(w, name="spawner1", projects=("demo",)):
    """Add a spawner principal (there is none in ``keeper_util.PRINCIPALS``)."""
    token = auth.add_principal(w.conn, name, "spawner", list(projects))
    w.tokens[name] = token
    w.principals[name] = auth.authenticate(w.conn, token)
    return name


# --------------------------------------------------------------- roundtrip
def test_roundtrip_two_rows_ids_increment_and_null_outcome(w):
    key, = seed(w, [("A", [])])
    first = w.store.jev_record(w.p("router"), row(
        key, kind="harness", attempt=1, rule_answer="rules-1", jev_answer="jev-1",
        jev_probabilities={"x": 0.5, "y": 0.25}, jev_confidence=0.75,
        reason="why", jev_model="free", latency_ms=12))
    second = w.store.jev_record(w.p("router"), row(
        key, kind="window", attempt=2, rule_answer="rules-2", jev_answer="jev-2"))
    assert first == {"id": 1, "card": key, "kind": "harness"}
    assert second == {"id": 2, "card": key, "kind": "window"}
    rows = w.store.jev_rows(key)
    assert [r["id"] for r in rows] == [1, 2]
    assert [r["kind"] for r in rows] == ["harness", "window"]
    assert rows[0]["created_at"] == "2023-11-14T22:13:20Z"
    assert rows[0]["outcome"] is None and rows[0]["outcome_at"] is None
    assert json.loads(rows[0]["jev_probabilities"]) == {"x": 0.5, "y": 0.25}
    assert rows[0]["jev_confidence"] == 0.75
    assert rows[0]["attempt"] == 1 and rows[0]["jev_model"] == "free"
    assert rows[0]["latency_ms"] == 12
    assert rows[1]["jev_probabilities"] is None
    assert rows[1]["outcome"] is None


# -------------------------------------------------------------- validation
def test_record_validation_refusals(w):
    key, = seed(w, [("A", [])])
    bad = [
        ({"kind": "nope"}, "validation"),
        ({"status": "nope"}, "validation"),
        ({"jev_answer": None}, "validation"),         # status ok needs an answer
        ({"attempt": 0}, "validation"),
        ({"attempt": True}, "validation"),
        ({"attempt": "1"}, "validation"),
        ({"jev_probabilities": {"a": 2}}, "validation"),
        ({"jev_probabilities": [1]}, "validation"),
        ({"jev_probabilities": {"a": "x"}}, "validation"),
        ({"jev_confidence": True}, "validation"),
        ({"latency_ms": -1}, "validation"),
        ({"bogus": 1}, "validation"),                 # unknown field
    ]
    for over, code in bad:
        with pytest.raises(Refused) as exc:
            w.store.jev_record(w.p("router"), row(key, **over))
        assert exc.value.code == code, over
    with pytest.raises(Refused) as exc:
        w.store.jev_record(w.p("router"), row("demo:ZZZ"))    # unknown card
    assert exc.value.code == "not_found"
    assert w.conn.execute("SELECT COUNT(*) FROM jev_shadow").fetchone()[0] == 0


def test_non_ok_status_drops_a_passed_jev_answer(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, status="timeout", jev_answer="late"))
    entry = w.store.jev_rows(key)[0]
    assert entry["status"] == "timeout"
    assert entry["jev_answer"] is None


def test_over_long_answers_and_reason_are_clipped_not_refused(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, rule_answer="r" * 600,
                                          reason="x" * 600,
                                          jev_model="m" * 300))
    entry = w.store.jev_rows(key)[0]
    assert entry["rule_answer"] == "r" * 200
    assert entry["reason"] == "x" * 200
    assert entry["jev_model"] == "m" * 100


# ------------------------------------------------------- roles and scope
def test_principal_scope_and_refusals(w):
    demo, = seed(w, [("A", [])], project="demo")
    other, = seed(w, [("B", [])], project="other")
    body_demo = {"card": demo, "kind": "harness", "rule_answer": "r",
                 "jev_answer": "j", "status": "ok"}
    body_other = dict(body_demo, card=other)
    # router and system may write anywhere
    w.store.jev_record(w.p("router"), body_demo)
    w.store.jev_record(w.p("router"), body_other)
    w.store.jev_record(w.p("system"), dict(body_demo, jev_answer="j2"))
    # an l1 may write only inside its own project
    w.store.jev_record(w.p("l1main"), body_demo)
    w.store.jev_record(w.p("l1other"), body_other)
    for who, body in (("l1main", body_other), ("l1other", body_demo)):
        with pytest.raises(Refused) as exc:
            w.store.jev_record(w.p(who), body)
        assert exc.value.code == "forbidden"
        with pytest.raises(Refused) as exc:
            w.store.jev_outcome(w.p(who), {"card": body["card"], "outcome": "x"})
        assert exc.value.code == "forbidden"
    # every other role is refused for BOTH writes
    add_spawner(w)
    for who in ("workerA", "spawner1", "reviewer", "reader"):
        with pytest.raises(Refused) as exc:
            w.store.jev_record(w.p(who), body_demo)
        assert exc.value.code == "forbidden"
        with pytest.raises(Refused) as exc:
            w.store.jev_outcome(w.p(who), {"card": demo, "outcome": "x"})
        assert exc.value.code == "forbidden"
    # a reader may still read
    assert w.store.jev_report(w.p("reader"))["overall"]["total"] == 5
    # refusals wrote nothing: only the five successful records exist
    assert w.conn.execute("SELECT COUNT(*) FROM jev_shadow").fetchone()[0] == 5


# ----------------------------------------------------------------- outcomes
def test_outcome_fills_by_card_and_attempt(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, attempt=1))
    w.store.jev_record(w.p("router"), row(key, attempt=2))
    result = w.store.jev_outcome(w.p("router"),
                                 {"card": key, "attempt": 1, "outcome": "good"})
    assert result == {"card": key, "updated": 1}
    rows = w.store.jev_rows(key)
    assert [r["outcome"] for r in rows] == ["good", None]
    assert rows[0]["outcome_at"] is not None


def test_outcome_by_card_alone_fills_all_null_rows(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, attempt=1))
    w.store.jev_record(w.p("router"), row(key, attempt=2))
    result = w.store.jev_outcome(w.p("router"), {"card": key, "outcome": "all"})
    assert result["updated"] == 2
    assert [r["outcome"] for r in w.store.jev_rows(key)] == ["all", "all"]


def test_outcome_attempt_none_matches_only_null_attempt_rows(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, attempt=1))
    w.store.jev_record(w.p("router"), row(key))
    result = w.store.jev_outcome(w.p("router"),
                                 {"card": key, "attempt": None, "outcome": "none"})
    assert result["updated"] == 1
    rows = w.store.jev_rows(key)
    assert rows[0]["attempt"] == 1 and rows[0]["outcome"] is None
    assert rows[1]["attempt"] is None and rows[1]["outcome"] == "none"


def test_outcome_kind_filter_matches_only_that_kind(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, kind="harness", attempt=1))
    w.store.jev_record(w.p("router"), row(key, kind="window", attempt=1))
    result = w.store.jev_outcome(w.p("router"),
                                 {"card": key, "kind": "window", "outcome": "win"})
    assert result["updated"] == 1
    rows = w.store.jev_rows(key)
    assert rows[0]["outcome"] is None and rows[1]["outcome"] == "win"


def test_outcome_first_write_wins_and_leaves_the_rest_of_the_row(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key, attempt=1,
                                          jev_probabilities={"a": 0.5},
                                          reason="why"))
    before = w.store.jev_rows(key)[0]
    assert w.store.jev_outcome(
        w.p("router"), {"card": key, "attempt": 1, "outcome": "one"})["updated"] == 1
    assert w.store.jev_outcome(
        w.p("router"), {"card": key, "attempt": 1, "outcome": "two"})["updated"] == 0
    after = w.store.jev_rows(key)[0]
    assert after["outcome"] == "one"
    assert after["outcome_at"] is not None
    others = ["id", "key", "attempt", "kind", "rule_answer", "jev_answer",
              "jev_probabilities", "jev_confidence", "status", "reason",
              "jev_model", "latency_ms", "created_at"]
    assert {c: after[c] for c in others} == {c: before[c] for c in others}


def test_outcome_validation_and_unknown_card(w):
    key, = seed(w, [("A", [])])
    with pytest.raises(Refused) as exc:
        w.store.jev_outcome(w.p("router"), {"card": "demo:ZZZ", "outcome": "x"})
    assert exc.value.code == "not_found"
    for over in ({"outcome": ""}, {"outcome": "x", "kind": "nope"},
                 {"outcome": "x", "attempt": 0}, {"outcome": "x", "bogus": 1}):
        with pytest.raises(Refused) as exc:
            w.store.jev_outcome(w.p("router"), dict({"card": key}, **over))
        assert exc.value.code == "validation", over


# ------------------------------------------------------------------- report
def test_jev_report_counts_rate_and_project_filter(w):
    demo, = seed(w, [("A", [])], project="demo")
    other, = seed(w, [("B", [])], project="other")
    record = w.store.jev_record
    router = w.p("router")
    record(router, row(demo, kind="harness", attempt=1, rule_answer="a",
                       jev_answer="a"))                    # agrees
    record(router, row(demo, kind="harness", attempt=2, rule_answer="a",
                       jev_answer="b"))                    # disagrees
    record(router, row(demo, kind="harness", attempt=3, rule_answer="a",
                       status="timeout", jev_answer=None))  # timeout, no verdict
    record(router, row(other, kind="window", attempt=1, rule_answer="x",
                       jev_answer="x"))                    # agrees
    # one outcome, on the timeout row only (its own attempt number)
    w.store.jev_outcome(router, {"card": demo, "attempt": 3, "outcome": "timed"})

    report = w.store.jev_report(router, project="demo")
    harness = report["kinds"]["harness"]
    assert (harness["total"], harness["ok"], harness["agree"],
            harness["disagree"], harness["timeout"]) == (3, 2, 1, 1, 1)
    assert harness["with_outcome"] == 1
    # a timeout is excluded from the rate: agree / ok, not agree / total
    assert harness["agreement_rate"] == 0.5
    assert report["kinds"]["window"]["total"] == 0
    assert (report["overall"]["agree"], report["overall"]["ok"]) == (1, 2)

    full = w.store.jev_report(router)
    assert full["kinds"]["harness"]["total"] == 3
    assert full["kinds"]["window"]["total"] == 1
    assert (full["overall"]["total"], full["overall"]["ok"],
            full["overall"]["agree"], full["overall"]["timeout"]) == (4, 3, 2, 1)
    assert full["overall"]["agreement_rate"] == 0.6667


# -------------------------------------------------------------------- audit
def test_audit_has_a_row_per_jev_write(w):
    key, = seed(w, [("A", [])])
    w.store.jev_record(w.p("router"), row(key))
    w.store.jev_outcome(w.p("router"), {"card": key, "outcome": "done"})
    actions = [r[0] for r in w.conn.execute(
        "SELECT action FROM audit_log WHERE action IN ('jev_record','jev_outcome')")]
    assert sorted(actions) == ["jev_outcome", "jev_record"]


def test_the_audit_never_holds_answers_or_probabilities(w):
    key, = seed(w, [("A", [])])
    marker = "jev-secret-answer"
    w.store.jev_record(w.p("router"), row(key, rule_answer="rules-secret",
                                          jev_answer=marker,
                                          jev_probabilities={"choice": 0.5}))
    rows = w.conn.execute(
        "SELECT ts,actor,role,action,key,ok,code,msg FROM audit_log").fetchall()
    blob = " ".join(str(value) for entry in rows for value in entry
                    if value is not None)
    assert marker not in blob
    assert "rules-secret" not in blob
    assert "choice" not in blob
