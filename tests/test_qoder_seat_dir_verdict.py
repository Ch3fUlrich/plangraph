"""write_seat_dir persists the parsed verdict fields (hermetic).

Before this, a qoder seat dir written without ``extra`` held review.txt +
evidence.json but no verdict/test_gaming, so tools/seat_verdict.py refused
every PASS ("pass refused (finish_reason not stop or end_turn, or
test_gaming not no)").
"""
import json
import subprocess
import sys
from pathlib import Path

from plangraph.review import qoder as q

REPO = Path(__file__).resolve().parents[1]
SEAT_VERDICT = REPO / "tools" / "seat_verdict.py"

PASS_TEXT = ("The flush ordering is sound.\n"
             "TEST-GAMING: no - searched the diff for hidden test skips: 0 hits\n"
             "VERDICT: pass\n")
FIX_TEXT = ("The flush is racy under load.\n"
            "TEST-GAMING: no - searched the diff for hidden test skips: 0 hits\n"
            "VERDICT: fix\n")
NO_GAMING_TEXT = "Everything else is clean.\nVERDICT: pass\n"
FIX_NO_GAMING_TEXT = "The flush is racy.\nVERDICT: fix\n"


def _result(text):
    """Fake QoderResult the way test_review_qoder builds one: run + stub runner."""
    payload = json.dumps({"type": "result", "subtype": "success",
                          "is_error": False, "num_turns": 1, "result": text,
                          "usage": {"input_tokens": 10, "output_tokens": 5}})

    def fake(cmd, **kwargs):
        assert isinstance(cmd, list)
        return subprocess.CompletedProcess(args=["qodercli"], returncode=0,
                                           stdout=payload, stderr="")
    return q.run("prompt text", runner=fake)


def _write(tmp_path, text, **kw):
    out = tmp_path / "seat"
    q.write_seat_dir(out, _result(text), **kw)
    return json.loads((out / "evidence.json").read_text())


def test_pass_text_records_pass_and_no_gaming(tmp_path):
    """A PASS review lands verdict=pass, test_gaming=no in evidence.json."""
    ev = _write(tmp_path, PASS_TEXT, material="the diff for hidden test skips")
    assert ev["verdict"] == "pass"
    assert ev["test_gaming"] == "no"


def test_fix_text_records_fix(tmp_path):
    """A FIX review lands verdict=fix."""
    ev = _write(tmp_path, FIX_TEXT, material="the diff")
    assert ev["verdict"] == "fix"


def test_missing_test_gaming_line_is_missing(tmp_path):
    """No TEST-GAMING line parses to test_gaming 'missing', never 'no'."""
    ev = _write(tmp_path, NO_GAMING_TEXT, material="")
    assert ev["verdict"] == "pass"
    assert ev["test_gaming"] == "missing"


def test_extra_cannot_override_judged_verdict(tmp_path):
    """extra={"verdict": "pass", "test_gaming": "no"} must not overwrite
    a parsed fix / missing gaming; other extra keys still land."""
    ev = _write(tmp_path, FIX_NO_GAMING_TEXT, material="the diff",
                extra={"verdict": "pass", "test_gaming": "no", "part": 9})
    assert ev["verdict"] == "fix"
    assert ev["test_gaming"] == "missing"
    assert ev["part"] == 9



def test_extra_may_only_make_judged_fields_stricter(tmp_path):
    """seat.py's failed run (verdict None) and a caller gaming finding win
    over a parsed PASS / 'no'."""
    ev = _write(tmp_path, PASS_TEXT, material="",
                extra={"verdict": None, "test_gaming": "yes"})
    assert ev["verdict"] is None
    assert ev["test_gaming"] == "yes"

def test_seat_verdict_dry_run_no_longer_refuses_pass(tmp_path):
    """tools/seat_verdict.py --dry-run accepts a written PASS seat dir."""
    out = tmp_path / "seat"
    q.write_seat_dir(out, _result(PASS_TEXT),
                     material="the diff for hidden test skips")
    (tmp_path / "reviewer-alibaba.token").write_text("dummy\n")
    proc = subprocess.run(
        [sys.executable, str(SEAT_VERDICT), "--key", "gen40/pr241-r2",
         "--seat-dir", str(out), "--writer-family", "openai",
         "--token-dir", str(tmp_path), "--url", "http://127.0.0.1:9",
         "--dry-run"],
        capture_output=True, text=True, cwd=str(REPO))
    combined = proc.stdout + proc.stderr
    assert "pass refused" not in combined, combined
    assert proc.returncode == 0, combined
