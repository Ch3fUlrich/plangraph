"""D-3544/D-3547: REQUEST_CHANGES -> fix, ACCEPT -> pass; reason label by verdict.

Every case below fails on origin/main: there REQUEST_CHANGES and ACCEPT read
as unknown first words (no verdict) and build_reason heads every reason PASS.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seat_verdict as sv  # noqa: E402
from test_seat_verdict import run  # noqa: E402


@pytest.mark.parametrize("text", [
    "VERDICT: REQUEST_CHANGES",
    "VERDICT: request-changes",
    "VERDICT: Request changes",
    "**VERDICT:** REQUEST_CHANGES",
    "VERDICT: request_changes",
    "VERDICT: request changes",
    "VERDICT: Request changes - the API contract breaks",
    "VERDICT: PASS\nVERDICT: REQUEST_CHANGES",
    "VERDICT: ACCEPT\nVERDICT: REQUEST_CHANGES",
])
def test_request_changes_is_fix(text):
    assert sv.parse_verdict(text)[0] is False


@pytest.mark.parametrize("text", [
    "VERDICT: ACCEPT",
    "VERDICT: accept",
    "VERDICT: Accept",
    "**VERDICT:** ACCEPT",
    "VERDICT: ACCEPT - no blockers",
    "verdict: accept. no findings",
    "VERDICT: REQUEST_CHANGES\nVERDICT: ACCEPT",
])
def test_accept_is_pass(text):
    assert sv.parse_verdict(text)[0] is True


@pytest.mark.parametrize("tail", ["", " - HIGH: the build breaks", " - LOW: nit only"])
def test_accept_shares_pass_label_rules(tail):
    assert sv.parse_verdict("VERDICT: PASS" + tail)[0] is True
    assert sv.parse_verdict("VERDICT: ACCEPT" + tail)[0] is True


@pytest.mark.parametrize("echo,alone,ok", [
    ("VERDICT: ACCEPT | REQUEST_CHANGES", "VERDICT: ACCEPT", True),
    ("VERDICT: request_changes / accept", "VERDICT: request_changes", False),
    ("VERDICT: accept / request_changes", "VERDICT: accept", True),
])
def test_template_echoes_stay_no_verdict(echo, alone, ok):
    assert sv.parse_verdict(echo)[0] is None
    # req 3's classic echo and req 5's fail-closed bare word both hold
    assert sv.parse_verdict("VERDICT: pass | fix")[0] is None
    assert sv.parse_verdict("VERDICT: request")[0] is None
    # ... while the same words without an | or / alternative DO decide
    assert sv.parse_verdict(alone)[0] is ok


def test_cli_accept_pass_and_gates(tmp_path, capsys):
    dry = ("--dry-run",)
    assert run(tmp_path, "a", "VERDICT: ACCEPT", extra=dry) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["body"]["ok"] is True
    assert " PASS corr " in payload["body"]["reason"]
    # the same pass gates refuse ACCEPT exactly like PASS
    assert run(tmp_path, "b", "VERDICT: ACCEPT", extra=dry, finish_reason="length") == 4
    assert run(tmp_path, "c", "VERDICT: ACCEPT", extra=dry, test_gaming="yes") == 4


def test_cli_request_changes_is_fix(tmp_path, capsys):
    assert run(tmp_path, "a", "VERDICT: REQUEST_CHANGES\nthe flaky test",
               extra=("--dry-run",)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["body"]["ok"] is False
    assert " FIX corr " in payload["body"]["reason"]
    assert "the flaky test" in payload["body"]["reason"]
    # like any fix it is still sent when the call was truncated
    assert run(tmp_path, "b", "VERDICT: request-changes\nwhy", extra=("--dry-run",),
               finish_reason="length", test_gaming="yes") == 0


def test_reason_head_labels_by_verdict():
    ev = {"served_model": "m", "correlation_id": "c",
          "prompt_sha256": "0123456789abcdef"}
    assert sv.build_reason(ev, True, "VERDICT: ACCEPT", 0) == \
        "seat m PASS corr c prompt 0123456789ab"
    reason = sv.build_reason(ev, False, "VERDICT: REQUEST_CHANGES\nwhy", 0)
    assert reason.startswith("seat m FIX corr c prompt 0123456789ab\n")
    assert reason.endswith("\nwhy")
