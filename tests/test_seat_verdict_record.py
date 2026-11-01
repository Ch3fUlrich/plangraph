"""D-3564: seat_verdict posts from a record.json-only spawner seat run dir."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seat_record  # noqa: E402
import seat_verdict as sv  # noqa: E402

HEAD_SHA = "d" * 40
REVIEW = "looks good overall\nVERDICT: PASS\nTEST_GAMING: no\n"


def record_dir(tmp_path, name, live_log=None, tail=REVIEW, writer="deepseek", **rec):
    d = tmp_path / name
    d.mkdir()
    record = {"run_id": "run-9f2c", "role": "review", "status": "completed",
              "exit_code": 0, "served_model": "unwitnessed",
              "chosen_leg": {"id": "leg-1", "model": "mimo/mimo-v2.6-pro",
                             "family": "mimo"},
              "writer_family": writer, "stdout_tail": tail,
              "completion": {"head_sha": HEAD_SHA}}
    record.update(rec)
    (d / "record.json").write_text(json.dumps(record))
    if live_log is not None:
        (d / "live.log").write_text(live_log)
    tokens = tmp_path / (name + "-tok")
    tokens.mkdir()
    (tokens / "reviewer-xiaomi.token").write_text("dummy\n")
    return ["--key", "demo:A", "--seat-dir", str(d), "--writer-family", writer,
            "--token-dir", str(tokens), "--dry-run"]


def test_record_only_pass_posts(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "a", live_log=REVIEW)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "xiaomi"
    body = payload["body"]
    assert body["ok"] is True
    assert HEAD_SHA in body["reason"]
    assert "run-9f2c" in body["reason"]
    assert "witness=record-chosen-leg" in body["reason"]


def test_record_pass_without_test_gaming_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "b", live_log="fine\nVERDICT: PASS\n")) == 4
    assert capsys.readouterr().out == ""


def test_record_fix_is_sent(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "c", live_log="flaky\nVERDICT: FIX\n")) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False


@pytest.mark.parametrize("completion", [{}, {"head_sha": "abc"}, {"head_sha": "z" * 40}])
def test_record_bad_head_sha_refused(tmp_path, capsys, completion):
    assert sv.main(record_dir(tmp_path, "d", live_log=REVIEW,
                              completion=completion)) == 2
    assert "head_sha" in capsys.readouterr().err


def test_record_fail_closed_on_role_and_status(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "e", live_log=REVIEW,
                              role="implement")) == 2
    assert sv.main(record_dir(tmp_path, "f", live_log=REVIEW,
                              status="failed")) == 2
    err = capsys.readouterr().err
    assert err.count("seat_verdict: refused, record:") == 2
    assert "role" in err and "status" in err


def test_record_no_verdict_line(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "g", live_log="no decision\n")) == 3
    assert "no parseable VERDICT" in capsys.readouterr().err


def test_record_seat_family_over_same_writer_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "h", live_log=REVIEW,
                              writer="xiaomi")) == 2
    assert "covers no listed writer" in capsys.readouterr().err


def test_evidence_json_wins_over_record_json(tmp_path, capsys):
    argv = record_dir(tmp_path, "i", live_log=REVIEW)
    seat = Path(argv[argv.index("--seat-dir") + 1])
    (seat / "evidence.json").write_text(json.dumps({
        "requested_model": "gpt-oss-120b", "served_model": "gpt-oss-120b",
        "finish_reason": "stop", "test_gaming": "no", "correlation_id": "c",
        "prompt_sha256": "ab" * 32}))
    (seat / "review.txt").write_text("VERDICT: PASS")
    (seat / "record.json").write_text(json.dumps({"role": "implement"}))
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["family"] == "openai"


def test_live_log_preferred_over_stdout_tail(tmp_path, capsys):
    argv = record_dir(tmp_path, "j", live_log="fine\nVERDICT: PASS\nTEST_GAMING: no\n",
                      tail="VERDICT: FIX\nfrom the tail")
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True
    argv = record_dir(tmp_path, "j2", live_log="", tail="VERDICT: FIX\ntail finding")
    assert sv.main(argv) == 0
    body = json.loads(capsys.readouterr().out)["body"]
    assert body["ok"] is False
    assert "tail finding" in body["reason"]


def test_evidence_from_record_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        seat_record.evidence_from_record(tmp_path / "missing")
    d = tmp_path / "r"
    d.mkdir()
    (d / "record.json").write_text(json.dumps({"role": "review", "status": "completed",
        "exit_code": 0, "served_model": "unwitnessed", "chosen_leg": {},
        "stdout_tail": "", "completion": {"head_sha": HEAD_SHA}}))
    with pytest.raises(ValueError):
        seat_record.evidence_from_record(d)
