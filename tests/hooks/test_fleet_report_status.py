"""fleet_report.py: which reply lines are filed to the parent's inbox, and the
one session-name check it shares with the other hooks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fleet_report_support import CHILD, hooks, run_stop  # noqa: F401  (hooks is a fixture)

KEYWORDS = ("DONE", "MERGE-READY", "BLOCKED", "FAIL", "FAILED", "RECYCLE-READY", "STOP")


@pytest.mark.parametrize("keyword", KEYWORDS)
def test_every_keyword_line_is_filed(
    hooks, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, keyword: str
) -> None:
    rows = run_stop(hooks[0], monkeypatch, tmp_path, f"working on it\n{keyword} item the reason")
    assert [(row["status"], row["line"], row["from"]) for row in rows] == [
        (keyword, f"{keyword} item the reason", CHILD)]


def test_the_contract_fail_line_is_filed(hooks, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rows = run_stop(hooks[0], monkeypatch, tmp_path, "FAIL g2 pytest red: 3 failed")
    assert [row["status"] for row in rows] == ["FAIL"]  # was dropped: only FAILED was a keyword


@pytest.mark.parametrize("text", [
    "FAILURE item", "FAIL-item x", "FAILEDX", "FAILS item", "STOPPED", "DONEX", "fail item",
    "not at the line start FAIL item",
])
def test_lines_that_only_resemble_a_keyword_are_not_filed(
    hooks, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> None:
    assert run_stop(hooks[0], monkeypatch, tmp_path, text) == []


def test_the_injector_lists_a_fail_row_with_its_status(hooks) -> None:
    report, inject, _common, _redact = hooks
    assert "FAIL" in report.STATUS_KEYWORDS and "FAILED" in report.STATUS_KEYWORDS
    row = inject.format_row({"from": CHILD, "status": "FAIL", "line": "FAIL item reason"})
    assert row == f"{CHILD} FAIL: FAIL item reason"


def test_the_name_check_is_the_shared_one(hooks) -> None:
    report, _inject, common, _redact = hooks
    assert report.safe_name is common.safe_id
    assert not hasattr(report, "_SAFE_NAME")
    for name in ("a", "child-0001", "x.y_z-1", "A" * 128):
        assert report.safe_name(name)
    for name in ("", "-a", ".a", "a/b", "a b", "A" * 129, None, 5):
        assert not report.safe_name(name)
