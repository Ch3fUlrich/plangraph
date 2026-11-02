"""fleet_report.py: the status line is masked before it reaches the inbox, and
fleet_inject.py masks it again before it reaches the parent's context.

Every secret is synthetic and built from fragments; ``assert_no_leak`` never
prints one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoos_refs_support import assert_masked, assert_no_leak
from fleet_report_support import CHILD, PARENT, hooks, run_stop  # noqa: F401  (hooks is a fixture)

SECRET = "hunter" + "2pw"


def test_a_dsn_password_in_a_status_line_never_reaches_the_inbox(
    hooks, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = hooks[0]
    text = (f"BLOCKED item | Environment=AUTOOS_PG_DSN=postgresql://user:{SECRET}@host | go\n"
            f"second line with postgresql://user:{SECRET}@host/db")
    rows = run_stop(report, monkeypatch, tmp_path, text)
    raw = (tmp_path / "state" / "inbox" / f"{PARENT}.jsonl").read_text(encoding="utf-8")
    assert_no_leak((SECRET,), raw)
    assert [row["line"] for row in rows] == ["BLOCKED item | Environment=AUTOOS_PG_DSN=[redacted] | go"]


def test_a_url_password_is_starred_in_the_filed_line(
    hooks, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = run_stop(hooks[0], monkeypatch, tmp_path,
                    f"DONE item abc1234 pushed scheme://user:{SECRET}@host/db ok")
    assert_no_leak((SECRET,), json.dumps(rows))
    assert rows[0]["line"] == "DONE item abc1234 pushed scheme://user:***@host/db ok"


def test_the_injector_masks_a_line_an_older_writer_filed_raw(hooks) -> None:
    _report, inject, _common, _redact = hooks
    raw = f"BLOCKED item | Environment=AUTOOS_PG_DSN=postgresql://user:{SECRET}@host | go"
    row = inject.format_row({"from": CHILD, "status": "BLOCKED", "line": raw})
    assert_no_leak((SECRET,), row)
    assert row == f"{CHILD} BLOCKED: BLOCKED item | Environment=AUTOOS_PG_DSN=[redacted] | go"


def test_redact_is_the_shared_masker_plus_long_runs(hooks) -> None:
    report, _inject, _common, shared = hooks
    line = f"x token={SECRET} u://a:{SECRET}@h/db {'A1b2' * 8}"
    out = report.redact(line)
    assert_masked(out, "x token=[redacted] u://a:***@h/db [redacted]", SECRET)
    assert_masked(report.redact(out), out, SECRET)
    assert_masked(report.redact(f"token={SECRET}"),
                  shared.redact_credentials(f"token={SECRET}"), SECRET)
    assert report.redact("a" * 31) == "a" * 31 and report.redact("a" * 32) == "[redacted]"
    assert report.redact(None) == "" and report.redact(5) == ""


def test_the_key_shapes_the_old_table_masked_are_still_masked(hooks) -> None:
    report = hooks[0]
    for shape in ("sk-" + "live-A1b2C3d4E5f6G7h8", "ghp_" + "A1b2" * 5, "gho_" + "A1b2" * 5,
                  "github_pat_" + "A1b2_" * 5, "xoxb-" + "123-AbCd", "AKIA" + "ABCD1234" * 2,
                  "Bearer abc.def-ghi", "eyJ" + "abc.eyJdef.ghi", "MY_API_KEY=" + SECRET):
        assert_no_leak((shape.split("=")[-1].split()[-1],), report.redact(f"DONE {shape} end"))
    assert report.redact("DONE ok 3 passed: 41 in 9s") == "DONE ok 3 passed: 41 in 9s"
