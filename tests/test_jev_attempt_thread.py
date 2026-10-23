from contextlib import closing
import threading
import time
from pathlib import Path

from jev_util import card, jev_on, seed_jev_rows
from keeper_util import World, seed, to_in_review
from plangraph.keeper import fmt
from plangraph.planner import jev_questions, routing, shadow


def wait_rows(w, key, count):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = w.store.jev_rows(key)
        if len(rows) == count and all(r["outcome"] for r in rows if r["kind"] == "qc_accept"):
            return rows
        time.sleep(0.01)
    return w.store.jev_rows(key)


def test_hanging_jev_does_not_delay_production_decisions(tmp_path, monkeypatch):
    release = threading.Event()
    jev_on(monkeypatch, lambda *a, **kw: release.wait(2))
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        to_in_review(w, key)
        try:
            started = time.monotonic()
            shadow.route_with_shadow(card(key=key), store=w.store, principal=w.p("router"))
            w.store.verdict(w.p("reviewer"), key, True, "clean")
            assert time.monotonic() - started < 0.2
        finally:
            release.set()
            from plangraph.planner.jev_shadow import drain
            drain()


def test_qc_production_outcome(tmp_path, monkeypatch):
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        to_in_review(w, key)
        jev_on(monkeypatch)
        w.store.verdict(w.p("reviewer"), key, True, "clean")
        assert wait_rows(w, key, 1)[0]["outcome"] == "accept"


def test_attempt_and_kind_scoped_fill(tmp_path, monkeypatch):
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        seed_jev_rows(w, key, monkeypatch, attempt=1)
        seed_jev_rows(w, key, monkeypatch, attempt=2)
        wait_rows(w, key, 8)
        w.store._fill_jev_outcome(w.p("router"), key, {"outcome": "complete"}, attempt=2)
        rows = w.store.jev_rows(key)
        assert [r["outcome"] for r in rows if r["attempt"] == 1] == [None] * 4
        assert [r["outcome"] for r in rows if r["attempt"] == 2] == ["complete"] * 4


def test_fill_does_not_touch_qc(tmp_path, monkeypatch):
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        seed_jev_rows(w, key, monkeypatch, attempt=1)
        wait_rows(w, key, 4)
        w.store.jev_record(w.p("router"), {"card": key, "attempt": 1,
            "kind": "qc_accept", "rule_answer": "accept", "status": "error"})
        w.store._fill_jev_outcome(w.p("router"), key, {"outcome": "complete"}, attempt=1)
        assert w.store.jev_rows(key)[-1]["outcome"] is None


def test_worker_attempt_fills_its_number(tmp_path, monkeypatch):
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        seed_jev_rows(w, key, monkeypatch, attempt=1)
        wait_rows(w, key, 4)
        w.store.worker_attempt(w.p("workerA"), {"card": key, "n": 1, "exit_class": "stall"})
        assert {r["outcome"] for r in w.store.jev_rows(key)} == {"stall"}


def test_late_routing_answer_inherits_recorded_attempt_outcome(tmp_path, monkeypatch):
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        w.store.record_attempt(w.p("router"), key, {"n": 1, "exit_class": "stall"})
        seed_jev_rows(w, key, monkeypatch, attempt=1)
        assert {r["outcome"] for r in w.store.jev_rows(key)} == {"stall"}


def test_auto_numbered_attempt_fill(tmp_path, monkeypatch):
    with closing(World(tmp_path)) as w:
        key, = seed(w, [("A", [])])
        seed_jev_rows(w, key, monkeypatch, attempt=1)
        w.store.record_attempt(w.p("router"), key, {"exit_class": "stall"})
        assert {r["outcome"] for r in w.store.jev_rows(key)} == {"stall"}


def test_pass_allowed_is_honoured():
    assert jev_questions.rule_qc_answer({"verdict": "pass", "pass_allowed": False}) == "reject"


def test_routing_state_is_independent_of_rule_route():
    route = routing.route_card(card())
    assert jev_questions.routing_state(card(), route) == jev_questions.routing_state(card(), None)


def test_unused_formatter_removed():
    assert not hasattr(fmt, "jev_axis_text")


def test_single_axis_scope_documented():
    text = Path("docs/decisions/0001-jev-qc-scope.md").read_text()
    assert "Q1-Q5" in text and "single-axis" in text
