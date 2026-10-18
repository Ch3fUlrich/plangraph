"""Tests for the pure Jev shadow agreement report (RT-P1-JEV-SHADOW).

Pattern: hermetic pure-module tests -- no store, no HTTP, no fixtures beyond
plain dicts.  The module under test is ``plangraph.keeper.jev_report``; each
case builds raw ``jev_shadow`` rows and asserts the four public functions.
"""
from __future__ import annotations

from plangraph.keeper import jev_report as jr


def _row(**over):
    """One jev_shadow row with sane defaults; override what a case cares about."""
    row = {"id": 1, "key": "demo:A", "attempt": 1, "kind": "harness",
           "rule_answer": "hold", "jev_answer": "hold",
           "jev_probabilities": '{"hold": 0.9}', "jev_confidence": 0.9,
           "status": "ok", "reason": None, "outcome": None,
           "jev_model": "jev-free", "latency_ms": 12,
           "created_at": "2026-01-01T00:00:00Z", "outcome_at": None}
    row.update(over)
    return row


def _mixed_rows():
    """harness: 3 ok (2 agree), 1 timeout, 1 unavailable; qc_accept: 1 ok, 1 error."""
    return [
        _row(id=1, kind="harness", status="ok", rule_answer="hold",
             jev_answer="hold", outcome="agreed"),
        _row(id=2, kind="harness", status="ok", rule_answer="hold",
             jev_answer="hold"),
        _row(id=3, kind="harness", status="ok", rule_answer="hold",
             jev_answer="run"),
        _row(id=4, kind="harness", status="timeout", jev_answer=None),
        _row(id=5, kind="harness", status="unavailable", jev_answer=None),
        _row(id=6, kind="qc_accept", status="ok", rule_answer="accept",
             jev_answer="accept", outcome="accepted"),
        _row(id=7, kind="qc_accept", status="error", jev_answer=None),
    ]


# --- agrees ---------------------------------------------------------------

def test_agrees_ok_when_answers_match():
    assert jr.agrees(_row(rule_answer="hold", jev_answer="hold")) is True


def test_agrees_ok_when_answers_differ():
    assert jr.agrees(_row(rule_answer="hold", jev_answer="run")) is False


def test_agrees_compares_after_str():
    assert jr.agrees(_row(rule_answer=1, jev_answer="1")) is True


def test_agrees_none_for_every_non_ok_status():
    for status in ("timeout", "unavailable", "error"):
        assert jr.agrees(_row(status=status, jev_answer="hold")) is None


def test_agrees_none_when_jev_answer_missing_or_absent():
    assert jr.agrees(_row(jev_answer=None)) is None
    row = _row()
    del row["jev_answer"]
    assert jr.agrees(row) is None


# --- row_view -------------------------------------------------------------

_VIEW_KEYS = {"id", "attempt", "kind", "rule_answer", "jev_answer", "agree",
              "jev_probabilities", "jev_confidence", "status", "reason",
              "outcome", "jev_model", "latency_ms", "created_at", "outcome_at"}


def test_row_view_keys_and_computed_agree():
    view = jr.row_view(_row())
    assert set(view) == _VIEW_KEYS
    assert "key" not in view
    assert view["agree"] is True


def test_row_view_includes_outcome_at():
    row = _row(outcome="agreed", outcome_at="2026-01-02T00:00:00Z")
    view = jr.row_view(row)
    assert view["outcome"] == "agreed"
    assert view["outcome_at"] == "2026-01-02T00:00:00Z"


def test_row_view_decodes_probabilities():
    view = jr.row_view(_row(jev_probabilities='{"hold": 0.9, "run": 0.1}'))
    assert view["jev_probabilities"] == {"hold": 0.9, "run": 0.1}


def test_row_view_probabilities_bad_empty_or_non_dict_become_none():
    assert jr.row_view(_row(jev_probabilities="not json"))["jev_probabilities"] is None
    assert jr.row_view(_row(jev_probabilities=""))["jev_probabilities"] is None
    assert jr.row_view(_row(jev_probabilities=None))["jev_probabilities"] is None
    assert jr.row_view(_row(jev_probabilities="[1, 2]"))["jev_probabilities"] is None


def test_row_view_agree_false_and_none():
    assert jr.row_view(_row(jev_answer="run"))["agree"] is False
    assert jr.row_view(_row(status="timeout"))["agree"] is None


# --- agreement_report -----------------------------------------------------

def test_report_all_kinds_always_present_even_when_empty():
    rep = jr.agreement_report([])
    assert set(rep["kinds"]) == {"harness", "window", "budget", "review",
                                 "qc_accept"}
    for stats in rep["kinds"].values():
        assert stats["total"] == 0
        assert stats["agreement_rate"] is None
    assert rep["overall"]["total"] == 0
    assert rep["overall"]["agreement_rate"] is None


def test_report_mixed_counts_and_rates():
    rep = jr.agreement_report(_mixed_rows())
    assert rep["kinds"]["harness"] == {
        "total": 5, "ok": 3, "agree": 2, "disagree": 1, "timeout": 1,
        "unavailable": 1, "error": 0, "agreement_rate": 0.6667,
        "with_outcome": 1}
    assert rep["kinds"]["qc_accept"] == {
        "total": 2, "ok": 1, "agree": 1, "disagree": 0, "timeout": 0,
        "unavailable": 0, "error": 1, "agreement_rate": 1.0,
        "with_outcome": 1}


def test_report_excludes_non_ok_from_the_rate():
    statuses = jr.agreement_report(_mixed_rows())["kinds"]["harness"]
    assert statuses["agreement_rate"] == round(2 / 3, 4)  # ok = 3, not total = 5


def test_report_overall_sums_the_kinds():
    overall = jr.agreement_report(_mixed_rows())["overall"]
    assert overall["total"] == 7
    assert overall["ok"] == 4
    assert overall["agree"] == 3
    assert overall["disagree"] == 1
    assert overall["timeout"] == 1
    assert overall["unavailable"] == 1
    assert overall["error"] == 1
    assert overall["with_outcome"] == 2
    assert overall["agreement_rate"] == 0.75


def test_report_rate_none_when_no_ok_rows():
    rows = [_row(kind="window", status="timeout"),
            _row(kind="window", status="unavailable")]
    rep = jr.agreement_report(rows)
    assert rep["kinds"]["window"]["ok"] == 0
    assert rep["kinds"]["window"]["agreement_rate"] is None
    assert rep["overall"]["agreement_rate"] is None


def test_report_skips_bad_rows_and_unknown_kinds():
    rows = ["not a mapping", None, 42, _row(kind="mystery")]
    rep = jr.agreement_report(rows)
    assert rep["overall"]["total"] == 0
    assert rep["kinds"]["harness"]["total"] == 0


def test_report_unknown_or_missing_status_is_error():
    rows = [_row(kind="budget", status="weird"),
            _row(kind="review", status="ok", jev_answer="hold")]
    del rows[1]["status"]
    rep = jr.agreement_report(rows)
    assert rep["kinds"]["budget"]["error"] == 1
    assert rep["kinds"]["review"]["error"] == 1


def test_report_with_outcome_counts_any_non_none():
    rows = [_row(kind="budget", status="weird", outcome="x"),
            _row(kind="budget", status="ok", rule_answer="a",
                 jev_answer="a", outcome=0),
            _row(kind="budget", status="ok", rule_answer="a", jev_answer="a")]
    stats = jr.agreement_report(rows)["kinds"]["budget"]
    assert stats["total"] == 3
    assert stats["error"] == 1
    assert stats["agree"] == 2
    assert stats["agreement_rate"] == 1.0
    assert stats["with_outcome"] == 2


def test_report_accepts_a_generator():
    rep = jr.agreement_report(row for row in _mixed_rows())
    assert rep["overall"]["total"] == 7


# --- render ---------------------------------------------------------------

def test_render_empty():
    assert jr.render(jr.agreement_report([])) == "no jev shadow rows"


def test_render_mixed_kinds_in_order():
    lines = jr.render(jr.agreement_report(_mixed_rows())).splitlines()
    assert [line.split()[0] for line in lines] == ["harness", "qc_accept",
                                                   "overall"]
    harness = lines[0]
    assert "agree 2/3" in harness
    assert "rate 0.6667" in harness
    assert "timeout 1" in harness
    assert "unavailable 1" in harness
    assert "error 0" in harness
    assert "outcomes 1" in harness
    qc = lines[1]
    assert qc.startswith("qc_accept")
    assert "agree 1/1" in qc
    assert "error 1" in qc
    overall = lines[2]
    assert overall.startswith("overall")
    assert "agree 3/4" in overall
    assert "rate 0.75" in overall
    assert "outcomes 2" in overall


def test_render_dash_rate_when_a_kind_has_no_ok_rows():
    text = jr.render(jr.agreement_report([_row(kind="window", status="timeout")]))
    assert text.startswith("window")
    assert "(rate -)" in text
