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
    """harness: 3 ok (2 agree), 1 timeout, 1 unavailable; review: 1 ok, 1 error."""
    return [
        _row(id=1, kind="harness", status="ok", rule_answer="hold",
             jev_answer="hold", outcome="agreed"),
        _row(id=2, kind="harness", status="ok", rule_answer="hold",
             jev_answer="hold"),
        _row(id=3, kind="harness", status="ok", rule_answer="hold",
             jev_answer="run"),
        _row(id=4, kind="harness", status="timeout", jev_answer=None),
        _row(id=5, kind="harness", status="unavailable", jev_answer=None),
        _row(id=6, kind="review", status="ok", rule_answer="accept",
             jev_answer="accept", outcome="accepted"),
        _row(id=7, kind="review", status="error", jev_answer=None),
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

_VIEW_KEYS = {"decision_id", "input_hash", "id", "attempt", "kind", "rule_answer", "jev_answer", "agree",
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
    assert set(rep["kinds"]) == {"harness", "window", "budget", "review"}
    for stats in rep["kinds"].values():
        assert stats["total"] == 0
        assert stats["agreement_rate"] is None
        assert stats["kappa"] is None
        assert stats["confusion"] == {}
        assert stats["labels"] == []
        assert stats["no_answer"] == 0
    assert rep["overall"]["total"] == 0
    assert rep["overall"]["agreement_rate"] is None
    assert rep["overall"]["kappa"] is None
    assert rep["overall"]["confusion"] == {}
    assert rep["overall"]["labels"] == []
    assert rep["overall"]["no_answer"] == 0


def test_report_mixed_counts_and_rates():
    rep = jr.agreement_report(_mixed_rows())
    assert rep["kinds"]["harness"] == {
        "total": 5, "ok": 3, "agree": 2, "disagree": 1, "timeout": 1,
        "unavailable": 1, "error": 0, "agreement_rate": 0.6667,
        "with_outcome": 1, "no_answer": 2, "kappa": 0.0,
        "confusion": {"hold": {"hold": 2, "run": 1}, "run": {"hold": 0, "run": 0}},
        "labels": ["hold", "run"]}
    assert rep["kinds"]["review"] == {
        "total": 2, "ok": 1, "agree": 1, "disagree": 0, "timeout": 0,
        "unavailable": 0, "error": 1, "agreement_rate": 1.0,
        "with_outcome": 1, "no_answer": 1, "kappa": None,
        "confusion": {"accept": {"accept": 1}},
        "labels": ["accept"]}


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
    assert overall["no_answer"] == 3
    assert overall["kappa"] == 0.5556
    assert overall["labels"] == ["accept", "hold", "run"]
    assert overall["confusion"] == {
        "accept": {"accept": 1, "hold": 0, "run": 0},
        "hold": {"accept": 0, "hold": 2, "run": 1},
        "run": {"accept": 0, "hold": 0, "run": 0},
    }


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
    assert [line.split()[0] for line in lines] == ["harness", "review",
                                                   "overall"]
    harness = lines[0]
    assert "agree 2/3" in harness
    assert "rate 0.6667" in harness
    assert "timeout 1" in harness
    assert "unavailable 1" in harness
    assert "error 0" in harness
    assert "outcomes 1" in harness
    review = lines[1]
    assert review.startswith("review")
    assert "agree 1/1" in review
    assert "error 1" in review
    overall = lines[2]
    assert overall.startswith("overall")
    assert "agree 3/4" in overall
    assert "rate 0.75" in overall
    assert "outcomes 2" in overall


def test_render_dash_rate_when_a_kind_has_no_ok_rows():
    text = jr.render(jr.agreement_report([_row(kind="window", status="timeout")]))
    assert text.startswith("window")
    assert "(rate -)" in text


# --- kappa and confusion matrix -------------------------------------------

def test_report_kappa_perfect_agreement():
    # 2 labels with matching rule and jev answers:
    # rows: (a, a), (b, b).
    # P_o = 2 / 2 = 1.0
    # P_e = (1*1 + 1*1) / 4 = 0.5
    # kappa = (1.0 - 0.5) / (1.0 - 0.5) = 1.0
    rows = [
        _row(kind="harness", status="ok", rule_answer="a", jev_answer="a"),
        _row(kind="harness", status="ok", rule_answer="b", jev_answer="b"),
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["harness"]
    assert stats["kappa"] == 1.0
    assert stats["labels"] == ["a", "b"]
    assert stats["confusion"] == {"a": {"a": 1, "b": 0}, "b": {"a": 0, "b": 1}}
    assert stats["no_answer"] == 0


def test_report_kappa_total_disagreement_2_labels():
    # 2 labels "a" and "b", 5 rows each where rule and jev always differ.
    # rule=a, jev=b (5 times)
    # rule=b, jev=a (5 times)
    # Total N = 10
    # Observed agreement P_o = 0 / 10 = 0.0
    # Marginals:
    #   Rule: R_a = 5, R_b = 5
    #   Jev:  J_a = 5, J_b = 5
    # Expected agreement P_e = (5*5 + 5*5) / 100 = 50 / 100 = 0.5
    # Cohen's kappa = (P_o - P_e) / (1 - P_e) = (0.0 - 0.5) / (1.0 - 0.5) = -0.5 / 0.5 = -1.0
    rows = [
        *[_row(kind="window", status="ok", rule_answer="a", jev_answer="b") for _ in range(5)],
        *[_row(kind="window", status="ok", rule_answer="b", jev_answer="a") for _ in range(5)],
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["window"]
    assert stats["kappa"] == -1.0
    assert stats["labels"] == ["a", "b"]
    assert stats["confusion"] == {"a": {"a": 0, "b": 5}, "b": {"a": 5, "b": 0}}


def test_report_kappa_chance_level():
    # 2 labels "a" and "b", uniform cross-product:
    # (a, a), (a, b), (b, a), (b, b).
    # N = 4
    # Observed agreement P_o = 2 / 4 = 0.5
    # Marginals:
    #   Rule: R_a = 2, R_b = 2
    #   Jev:  J_a = 2, J_b = 2
    # Expected agreement P_e = (2*2 + 2*2) / 16 = 8 / 16 = 0.5
    # kappa = (0.5 - 0.5) / (1.0 - 0.5) = 0.0
    rows = [
        _row(kind="budget", status="ok", rule_answer="a", jev_answer="a"),
        _row(kind="budget", status="ok", rule_answer="a", jev_answer="b"),
        _row(kind="budget", status="ok", rule_answer="b", jev_answer="a"),
        _row(kind="budget", status="ok", rule_answer="b", jev_answer="b"),
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["budget"]
    assert stats["kappa"] == 0.0
    assert stats["confusion"] == {"a": {"a": 1, "b": 1}, "b": {"a": 1, "b": 1}}


def test_report_kappa_single_label_undefined():
    # Only 1 label "a" is present across all usable rows.
    # R_a = 3, J_a = 3, N = 3
    # P_o = 3 / 3 = 1.0
    # P_e = (3 * 3) / 9 = 1.0
    # Expected agreement == 1.0 -> denominator (1 - P_e) is 0 -> kappa is None.
    rows = [
        _row(kind="review", status="ok", rule_answer="a", jev_answer="a"),
        _row(kind="review", status="ok", rule_answer="a", jev_answer="a"),
        _row(kind="review", status="ok", rule_answer="a", jev_answer="a"),
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["review"]
    assert stats["kappa"] is None
    assert stats["labels"] == ["a"]
    assert stats["confusion"] == {"a": {"a": 3}}


def test_report_no_answer_rows_excluded_from_kappa_and_matrix():
    # 2 ok rows: (a, a) and (b, b)
    # plus non-ok rows: timeout, unavailable, error, and an ok row with jev_answer=None
    rows = [
        _row(kind="harness", status="ok", rule_answer="a", jev_answer="a"),
        _row(kind="harness", status="ok", rule_answer="b", jev_answer="b"),
        _row(kind="harness", status="timeout", jev_answer=None),
        _row(kind="harness", status="unavailable", jev_answer=None),
        _row(kind="harness", status="error", jev_answer=None),
        _row(kind="harness", status="ok", rule_answer="c", jev_answer=None),
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["harness"]
    assert stats["no_answer"] == 4
    assert stats["ok"] == 3
    # Only the 2 usable rows contribute to labels and confusion
    assert stats["labels"] == ["a", "b"]
    assert stats["confusion"] == {"a": {"a": 1, "b": 0}, "b": {"a": 0, "b": 1}}
    assert stats["kappa"] == 1.0


def test_report_deterministic_confusion_ordering():
    # Add rows with labels in arbitrary order: "z", "a", "m"
    rows = [
        _row(kind="review", status="ok", rule_answer="z", jev_answer="m"),
        _row(kind="review", status="ok", rule_answer="a", jev_answer="z"),
        _row(kind="review", status="ok", rule_answer="m", jev_answer="a"),
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["review"]
    # Labels must be sorted
    assert stats["labels"] == ["a", "m", "z"]
    # Outer dict keys must be in sorted order
    assert list(stats["confusion"].keys()) == ["a", "m", "z"]
    # Inner dict keys must be in sorted order
    for row_k in stats["confusion"]:
        assert list(stats["confusion"][row_k].keys()) == ["a", "m", "z"]


def test_report_kappa_3_labels_hand_computed():
    # Hand-computed 3-label example with labels "A", "B", "C".
    # Confusion counts:
    #   rule=A, jev=A: 3
    #   rule=A, jev=B: 1
    #   rule=B, jev=A: 1
    #   rule=B, jev=B: 2
    #   rule=B, jev=C: 1
    #   rule=C, jev=C: 2
    #
    # Matrix:
    #       Jev: A  B  C | Total Rule
    # Rule A:    3  1  0 | 4
    # Rule B:    1  2  1 | 4
    # Rule C:    0  0  2 | 2
    # Total Jev: 4  3  3 | N = 10
    #
    # Observed agreement:
    #   agree = 3 + 2 + 2 = 7
    #   P_o = 7 / 10 = 0.7
    #
    # Expected agreement by chance:
    #   P(A) = (4 * 4) / 100 = 0.16
    #   P(B) = (4 * 3) / 100 = 0.12
    #   P(C) = (2 * 3) / 100 = 0.06
    #   P_e = 0.16 + 0.12 + 0.06 = 0.34
    #
    # Cohen's kappa:
    #   kappa = (P_o - P_e) / (1 - P_e)
    #         = (0.70 - 0.34) / (1.0 - 0.34)
    #         = 0.36 / 0.66
    #         = 6 / 11
    #         = 0.545454... -> rounded to 4 places: 0.5455
    rows = [
        *[_row(kind="harness", status="ok", rule_answer="A", jev_answer="A") for _ in range(3)],
        *[_row(kind="harness", status="ok", rule_answer="A", jev_answer="B") for _ in range(1)],
        *[_row(kind="harness", status="ok", rule_answer="B", jev_answer="A") for _ in range(1)],
        *[_row(kind="harness", status="ok", rule_answer="B", jev_answer="B") for _ in range(2)],
        *[_row(kind="harness", status="ok", rule_answer="B", jev_answer="C") for _ in range(1)],
        *[_row(kind="harness", status="ok", rule_answer="C", jev_answer="C") for _ in range(2)],
    ]
    rep = jr.agreement_report(rows)
    stats = rep["kinds"]["harness"]
    assert stats["labels"] == ["A", "B", "C"]
    assert stats["confusion"] == {
        "A": {"A": 3, "B": 1, "C": 0},
        "B": {"A": 1, "B": 2, "C": 1},
        "C": {"A": 0, "B": 0, "C": 2},
    }
    assert stats["kappa"] == 0.5455
