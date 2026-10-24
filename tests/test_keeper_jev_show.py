"""Contract tests for the Jev shadow rows on the ``show`` read (RT-P1-JEV-SHADOW).

``show`` carries the card's ``jev_shadow`` row views and the overall agreement
counters, but ONLY when the card has rows: a card without them keeps exactly its
previous output, so the frozen ``Show`` schema stays valid and the text format is
unchanged.  ``fmt`` renders the summary plus one line per row, and
``fmt.jev_report_text`` is the text door for the whole-corpus report.
"""
import pytest

from keeper_util import World, seed
from plangraph.keeper import fmt


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


# ---------------------------------------------------------------- no rows
def test_show_without_jev_rows_keeps_its_exact_shape(w):
    key, = seed(w, [("A", [])])
    info = w.store.show(w.p("router"), key)
    assert "jev_shadow" not in info and "jev_agreement" not in info
    # a second card that also has no shadow rows has the same key set
    baseline, = seed(w, [("B", [])], project="other")
    other = w.store.show(w.p("router"), baseline)
    assert set(info) == set(other)


def test_show_without_jev_rows_renders_the_same_text(w):
    key, = seed(w, [("A", [])])
    info = w.store.show(w.p("router"), key)
    base = fmt.show_text(info)
    assert "jev agree" not in base
    # the same info plus the (optional) jev keys renders the old lines first
    info["jev_shadow"] = [{"kind": "harness", "attempt": 1, "rule_answer": "a",
                           "jev_answer": "a", "agree": True, "status": "ok",
                           "outcome": None, "reason": ""}]
    info["jev_agreement"] = {"ok": 1, "agree": 1, "disagree": 0, "timeout": 0,
                             "unavailable": 0, "error": 0, "agreement_rate": 1.0}
    after = fmt.show_text(info)
    assert after.splitlines()[:len(base.splitlines())] == base.splitlines()
    assert after.startswith(base + "\n")


# ------------------------------------------------------------------- rows
def test_show_carries_the_row_views_and_overall_agreement(w):
    key, = seed(w, [("A", [])])
    record, router = w.store.jev_record, w.p("router")
    record(router, row(key, kind="harness", attempt=1, rule_answer="a",
                       jev_answer="a", jev_probabilities={"x": 0.5},
                       jev_model="free", latency_ms=7))
    record(router, row(key, kind="window", attempt=2, rule_answer="a",
                       jev_answer="b"))
    record(router, row(key, kind="budget", attempt=3, rule_answer="a",
                       status="timeout", jev_answer=None, reason="slow"))
    record(router, row(key, kind="review", rule_answer="a",
                       status="unavailable", jev_answer=None))

    info = w.store.show(w.p("router"), key)
    shadow = info["jev_shadow"]
    assert [r["id"] for r in shadow] == [1, 2, 3, 4]
    assert [r["kind"] for r in shadow] == ["harness", "window", "budget",
                                           "review"]
    assert [r["agree"] for r in shadow] == [True, False, None, None]
    assert shadow[0]["jev_probabilities"] == {"x": 0.5}
    assert shadow[1]["jev_probabilities"] is None
    assert "key" not in shadow[0]     # internal routing bookkeeping is dropped
    overall = info["jev_agreement"]
    assert (overall["ok"], overall["agree"], overall["disagree"],
            overall["timeout"]) == (2, 1, 1, 1)
    assert overall["agreement_rate"] == 0.5
    assert overall["kappa"] == 0.0
    assert overall["no_answer"] == 2
    assert overall["labels"] == ["a", "b"]
    assert overall["confusion"] == {"a": {"a": 1, "b": 1}, "b": {"a": 0, "b": 0}}

    text = fmt.show_text(info)
    assert "jev agree 1/2 (rate 0.50) timeout 1 unavailable 1 error 0" in text
    assert "rule=" in text and "jev=" in text
    assert "agree" in text and "differ" in text and "timeout" in text
    assert "outcome=" in text
    assert "jev harness [a1] rule=a jev=a agree ok outcome=-" in text
    assert "jev window [a2] rule=a jev=b differ ok outcome=-" in text
    assert "jev budget [a3] rule=a jev=- - timeout outcome=- (slow)" in text
    assert "jev review rule=a jev=- - unavailable outcome=-" in text

    # the outcome is filled later and then shows up in the row and its line
    assert w.store.jev_outcome(
        router, {"card": key, "attempt": 3, "outcome": "timed"})["updated"] == 1
    info2 = w.store.show(w.p("router"), key)
    assert info2["jev_shadow"][2]["outcome"] == "timed"
    assert "outcome=timed" in fmt.show_text(info2)


# --------------------------------------------------------------- report text
def test_jev_report_text_renders_empty_and_populated_reports(w):
    assert fmt.jev_report_text(w.store.jev_report(w.p("reader"))) \
        == "no jev shadow rows"
    key, = seed(w, [("A", [])])
    router = w.p("router")
    w.store.jev_record(router, row(key, kind="harness", attempt=1,
                                   rule_answer="a", jev_answer="a"))
    w.store.jev_record(router, row(key, kind="harness", attempt=2,
                                   rule_answer="a", jev_answer="b"))
    text = fmt.jev_report_text(w.store.jev_report(w.p("reader")))
    assert "harness" in text
    assert "rate 0.5000" in text


def test_jev_axis_text():
    # Axis reporting has one renderer; the unused parallel formatter is gone.
    assert not hasattr(fmt, "jev_axis_text")
