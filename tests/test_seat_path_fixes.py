"""Hermetic regressions for SEAT-PATH-FIXES (D-3676)."""
import importlib.util
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.review.verdict import parse_verdict
from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.run_watchdog import Watchdog, thresholds, terminal_record


def test_inline_review_without_diff_ref(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a document review must not invoke git")
    monkeypatch.setattr("plangraph.spawner.inline_diff.subprocess.run", forbidden)
    assert inline_diff_prompt("Review this design", None, max_chars=1, allow_document=True) == "Review this design"
    with pytest.raises(InlineDiffError, match="no diff_ref"):
        inline_diff_prompt("Review this design", None, max_chars=1)
    with pytest.raises(InlineDiffError):
        inline_diff_prompt("Review", {}, max_chars=1)


def test_mimo_flash_is_inline_only():
    assert next(l for l in DEFAULT_LEGS if l.id == "mimo-v2.6-flash").tool_mode == "inline-only"


def test_verdict_commentary_and_parser_parity():
    spec = importlib.util.spec_from_file_location("seat_verdict", Path(__file__).parents[1] / "tools/seat_verdict.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    # Historical run stdout was unavailable; these are synthetic regression cases.
    for line, expected in [
        ("VERDICT: REQUEST_CHANGES — HIGH: broken contract", "fix"),
        ("VERDICT: ACCEPT — no blocking findings", "pass"),
        ("VERDICT: PASS: no blockers", "pass"),
        ("VERDICT: PASS? commentary follows", "pass"),
        ("VERDICT: pass | fix", None),
        ("VERDICT: request_changes / accept", None),
        ("VERDICT: unknown commentary", None),
    ]:
        text = "VERDICT: halt\n" + line
        assert parse_verdict(text) == expected
        assert tool.parse_verdict(text)[0] == {"fix": False, "pass": True, None: None}[expected]
    # Parity per rule: halt, low-label downgrade, and multi-VERDICT ordering
    # (the last VERDICT line decides even when it is no decision at all).
    for text, expected in [
        ("VERDICT: halt", "halt"),
        ("VERDICT: halt — low: nits only", "halt"),
        ("VERDICT: fix — low: nit", "pass"),
        ("VERDICT: changes: LOW: docs only", "pass"),
        ("VERDICT: fix — low: nit, HIGH: broken contract", "fix"),
        ("VERDICT: pass\nVERDICT: fix — high: broken", "fix"),
        ("VERDICT: pass\nVERDICT: pass | fix", None),
        ("VERDICT: fix\nVERDICT: unknown commentary", None),
    ]:
        assert parse_verdict(text) == expected
        assert tool.parse_verdict(text)[0] == {"fix": False, "pass": True, "halt": False, None: None}[expected]


def test_review_default_wall_cap(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.delenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", raising=False)
    limits = thresholds(role="review")
    assert limits["max_s"] == 1500 and limits["hard_max"]
    watchdog = Watchdog(limits, start=0, edit=None, writer=False)
    assert watchdog.decide(1499, None, None, None) is None
    event = watchdog.decide(1500, None, None, None)
    assert event["rule"] == "max"
    record = {"role": "review"}
    assert terminal_record(record, SimpleNamespace(status="timeout", exit_code=124, events=[event]))
    assert record["status"] == "timed_out"
    assert thresholds()["max_s"] == thresholds(role="orchestrate")["max_s"] == 5400
    assert thresholds({"max_min": 2}, role="review")["max_s"] == 120
    monkeypatch.setenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", "7")
    assert thresholds(role="review")["max_s"] == 7
    assert thresholds({"max_min": 2}, role="review")["max_s"] == 120
