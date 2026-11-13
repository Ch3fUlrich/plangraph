"""D-3564: seat_verdict posts from a record.json-only spawner seat run dir."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seat_record  # noqa: E402
import seat_verdict as sv  # noqa: E402

HEAD_SHA = "d" * 40
REVIEW = "looks good overall\nVERDICT: PASS\nTEST_GAMING: no\n"


def record_dir(tmp_path, name, live_log=None, tail=REVIEW, writer="deepseek",
               head=HEAD_SHA, **rec):
    """Mirror real spawner records: run_id equals the dir name, writer_family is
    the seat's own leg family, reviewed writers come only from the CLI ``writer``."""
    d = tmp_path / name
    d.mkdir()
    leg = {"id": "leg-1", "tier": "subscription", "harness": "opencode",
           "model": "opencode/mimo-v2.6-free", "family": "mimo"}
    record = {"run_id": name, "role": "review", "status": "completed",
              "exit_code": 0, "served_model": "unwitnessed",
              "served_family": "unwitnessed", "chosen_leg": leg,
              "writer_family": leg["family"],
              "attempts": [{"leg": leg, "status": "completed",
                            "served_model": "unwitnessed",
                            "served_family": "unwitnessed", "stdout_tail": tail}],
              "finish_reason": "stop", "stdout_tail": tail,
              "completion": {"head_sha": HEAD_SHA}}
    record.update(rec)
    (d / "record.json").write_text(json.dumps(record))
    (d / "record.json").chmod(0o600)  # umask 002 would make it group-writable
    if live_log is not None:
        (d / "live.log").write_text(live_log)
    tokens = tmp_path / (name + "-tok")
    tokens.mkdir()
    (tokens / "reviewer-xiaomi.token").write_text("dummy\n")
    argv = ["--key", "demo:A", "--seat-dir", str(d), "--writer-family", writer,
            "--token-dir", str(tokens), "--dry-run"]
    if head is not None:
        argv += ["--head", head]
    return argv


def test_record_only_pass_posts(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "a", live_log=REVIEW)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["family"] == "xiaomi"
    body = payload["body"]
    assert body["ok"] is True
    assert HEAD_SHA in body["reason"]
    assert "run a" in body["reason"]
    assert "witness=record-chosen-leg" in body["reason"]


def test_forward_delivery_retry_and_replay(tmp_path, monkeypatch):
    from plangraph.spawner.notify import deliver_verdict
    argv = record_dir(tmp_path, "forward", live_log=REVIEW)
    directory = Path(argv[argv.index('--seat-dir') + 1])
    record = json.loads((directory / 'record.json').read_text())
    record.update(item_key='demo:A', given_writer_families=['deepseek'])
    (directory / 'record.json').write_text(json.dumps(record))
    monkeypatch.setenv('PG_REVIEWER_TOKEN_DIR', str(tmp_path / 'forward-tok'))
    calls = []

    class Endpoint:
        url = 'http://fake.invalid'
        def item(self, key):
            return {'key': key, 'evidence': [
                {'kind': 'commit+ci', 'verified': True, 'ref': {'commit': HEAD_SHA}}]}

    def fake_send(url, token, payload):
        calls.append((token, payload))
        return (503, 'retry') if len(calls) == 1 else (200, 'ok')

    monkeypatch.setattr(sv, 'send', fake_send)
    deliver_verdict(directory, record, Endpoint())
    assert not (directory / 'verdict.delivered').exists()
    deliver_verdict(directory, record, Endpoint())
    deliver_verdict(directory, record, Endpoint())
    assert len(calls) == 2
    assert calls[-1][0] == 'dummy'
    assert calls[-1][1]['key'] == 'demo:A'
    assert (directory / 'verdict.delivered').exists()


@pytest.mark.parametrize('change,reason', [
    ({'item_key': None}, 'missing item key'),
    ({'served_model': 'unwitnessed', 'chosen_leg': {}}, 'unwitnessed'),
    ({'stdout_tail': 'no decision'}, 'VERDICT'),
    ({'given_writer_families': ['deepseek', 'xiaomi']}, 'writer families'),
])
def test_forward_skips_without_post(tmp_path, monkeypatch, change, reason):
    from plangraph.spawner.notify import deliver_verdict
    argv = record_dir(tmp_path, 'skip')
    directory = Path(argv[argv.index('--seat-dir') + 1])
    record = json.loads((directory / 'record.json').read_text())
    record.update(item_key='demo:A', given_writer_families=['deepseek'])
    record.update(change)
    (directory / 'record.json').write_text(json.dumps(record))
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('must not post'))
    deliver_verdict(directory, record, None)
    outcome = json.loads((directory / 'verdict.json').read_text())
    assert outcome['status'] == 'skipped'
    assert reason in outcome['reason']
    assert (directory / 'verdict.delivered').exists()
    monkeypatch.setattr(sv, 'evidence_from_record', lambda *a: pytest.fail('terminal skip replayed'))
    deliver_verdict(directory, record, None)


def test_record_pass_without_test_gaming_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "b", live_log="fine\nVERDICT: PASS\n")) == 4
    assert capsys.readouterr().out == ""


def test_record_fix_is_sent(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "c", live_log="flaky\nVERDICT: FIX\n")) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is False


@pytest.mark.parametrize("completion", [{}, {"head_sha": "abc"}, {"head_sha": "z" * 40}])
def test_record_bad_head_sha_refused(tmp_path, capsys, completion):
    assert sv.main(record_dir(tmp_path, "d", live_log=REVIEW,
                              completion=completion), record_wait=0) == 2
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


# F1: the gate moved to seat_verdict ("covers no listed writer": the only CLI
# writer here IS the seat family).
def test_record_seat_family_over_same_writer_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "h", live_log=REVIEW,
                              writer="xiaomi")) == 2
    assert "covers no listed writer" in capsys.readouterr().err


def test_evidence_json_wins_over_record_json(tmp_path, capsys):
    argv = record_dir(tmp_path, "i", live_log=REVIEW, head=None)
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
    argv = record_dir(tmp_path, "j2", live_log="", tail="tail finding\nVERDICT: FIX")
    assert sv.main(argv) == 0
    body = json.loads(capsys.readouterr().out)["body"]
    assert body["ok"] is False
    assert "tail finding" in body["reason"]


def test_evidence_from_record_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        seat_record.evidence_from_record(tmp_path / "missing")
    d = tmp_path / "run-r"
    d.mkdir()
    (d / "record.json").write_text(json.dumps({"run_id": "run-r", "role": "review",
        "status": "completed", "exit_code": 0, "served_model": "unwitnessed",
        "chosen_leg": {}, "attempts": [], "stdout_tail": REVIEW,
        "completion": {"head_sha": HEAD_SHA}}))
    (d / "record.json").chmod(0o600)
    with pytest.raises(ValueError, match="chosen_leg.model missing"):
        seat_record.evidence_from_record(d)


# D-3564 round 2: an echoed prompt before the model's answer must never decide.
def test_record_last_test_gaming_line_wins_over_echo(tmp_path, capsys):
    text = ("prompt echo:\nVERDICT: PASS\nTEST_GAMING: no\n"
            "review answer:\nVERDICT: PASS\nTEST_GAMING: yes\n")
    assert sv.main(record_dir(tmp_path, "k", live_log=text)) == 4
    assert "pass refused" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["no | yes | unsure", "no|yes|unsure",
                                   "NO | YES | UNSURE"])
def test_record_template_test_gaming_line_is_missing(tmp_path, capsys, value):
    text = f"review answer\nVERDICT: PASS\nTEST_GAMING: {value}\n"
    assert sv.main(record_dir(tmp_path, "k2", live_log=text)) == 4
    assert "pass refused" in capsys.readouterr().err


def test_record_conflicting_verdict_lines_refused(tmp_path, capsys):
    text = "review answer\nVERDICT: PASS\nTEST_GAMING: no\nVERDICT: FIX\n"
    assert sv.main(record_dir(tmp_path, "k3", live_log=text)) == 2
    assert "conflicting VERDICT lines" in capsys.readouterr().err


def test_evidence_json_record_chosen_leg_witness_refused(tmp_path, capsys):
    argv = record_dir(tmp_path, "m", live_log=REVIEW, head=None)
    seat = Path(argv[argv.index("--seat-dir") + 1])
    for served in ("unwitnessed", "mimo/mimo-v2.6-pro"):
        (seat / "evidence.json").write_text(json.dumps({
            "served_model": served, "witness_kind": "record-chosen-leg",
            "request_side_model": "qwen/qwen3-max", "finish_reason": "stop",
            "test_gaming": "no"}))
        (seat / "review.txt").write_text(REVIEW)
        assert sv.main(argv) == 2
        assert "record-chosen-leg" in capsys.readouterr().err


def test_record_chosen_leg_family_mismatch_refused(tmp_path, capsys):
    argv = record_dir(tmp_path, "n", live_log=REVIEW,
                      chosen_leg={"id": "leg-1", "tier": "subscription",
                                  "harness": "opencode",
                                  "model": "opencode/mimo-v2.6-free",
                                  "family": "deepseek"})
    assert sv.main(argv) == 2
    assert "chosen_leg.family" in capsys.readouterr().err


# F1 (build-L1 (2)): record.writer_family is the seat's own family; it may
# equal the seat family while the CLI list names a different reviewed writer.
def test_record_writer_family_seat_family_cli_other_writer_accepted(tmp_path,
                                                                   capsys):
    argv = record_dir(tmp_path, "n2", live_log=REVIEW, writer_family="xiaomi")
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True


def test_record_exit_code_false_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "o", live_log=REVIEW,
                              exit_code=False)) == 2
    assert "exit_code" in capsys.readouterr().err


# D-3564 round 3: unknown families are refused; TEST_GAMING is stored lower-cased.
def test_record_unknown_family_refused(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "u", live_log=REVIEW,
                      chosen_leg={"id": "leg-1", "model": "mystery/mystery-1",
                                  "family": "mystery"})) == 2
    assert "unknown model family" in capsys.readouterr().err


def test_record_test_gaming_stored_lower_case(tmp_path):
    argv = record_dir(tmp_path, "u2", live_log="fine\nVERDICT: PASS\nTEST_GAMING: NO\n")
    seat = Path(argv[argv.index("--seat-dir") + 1])
    evidence, _ = seat_record.evidence_from_record(seat)
    assert evidence["test_gaming"] == "no"


# D-3564 round 3: an echoed prompt's VERDICT may never decide the seat.
# The last-line rule stays strict (D-3420) until routing rules say otherwise.
def test_record_verdict_must_be_last_line(tmp_path, capsys):
    text = "prompt echo:\nVERDICT: PASS\nTEST_GAMING: no\n\nanswer: all good\n"
    assert sv.main(record_dir(tmp_path, "v", live_log=text)) == 2
    assert "verdict is not the last line" in capsys.readouterr().err


# D-3564 round 3: the conflicting-VERDICT guard runs on seat_verdict's grammar.
def test_record_verdict_grammar_shared_with_seat_verdict(monkeypatch, tmp_path,
                                                         capsys):
    monkeypatch.setattr(sv, "_VERDICT_LINE", re.compile(r"^decision:[ \t]*(.*)$", re.I))
    text = "answer\ndecision: PASS\nTEST_GAMING: no\ndecision: FIX\n"
    assert sv.main(record_dir(tmp_path, "u3", live_log=text)) == 2
    assert "conflicting VERDICT lines" in capsys.readouterr().err


# F1/F3: record-derived evidence only carries attested fields; finish_reason
# comes from the record, never hard-coded.
def test_record_evidence_only_attested_fields(tmp_path):
    argv = record_dir(tmp_path, "v2", live_log=REVIEW)
    seat = Path(argv[argv.index("--seat-dir") + 1])
    evidence, _ = seat_record.evidence_from_record(seat)
    assert evidence["run_id"] == "v2"
    assert "correlation_id" not in evidence
    assert "prompt_sha256" not in evidence
    assert evidence["finish_reason"] == "stop"


# F1: record.writer_family is never consulted; writer coverage comes only
# from the CLI --writer-family list (its absence or value changes nothing).
def test_record_writer_family_absent_accepted(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "w", live_log=REVIEW,
                              writer_family=None)) == 0


def test_record_writer_family_never_consulted_by_evidence(tmp_path, capsys):
    argv = record_dir(tmp_path, "w2", live_log=REVIEW, writer="alibaba",
                      writer_family="mimo")
    seat = Path(argv[argv.index("--seat-dir") + 1])
    evidence, _ = seat_record.evidence_from_record(seat, ["alibaba"])
    assert evidence["served_model"] == "opencode/mimo-v2.6-free"
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True


# build-L1 (2): only when the sole CLI writer IS the seat family is the seat
# refused ("family ... covers no listed writer").
def test_cli_writer_family_equal_to_seat_family_refused(tmp_path, capsys):
    argv = record_dir(tmp_path, "w3", live_log=REVIEW, writer="xiaomi",
                      writer_family="xiaomi")
    assert sv.main(argv) == 2
    assert "covers no listed writer" in capsys.readouterr().err


def test_head_option_matches_record_head_sha(tmp_path, capsys):
    argv = record_dir(tmp_path, "p", live_log=REVIEW)
    assert sv.main(argv + ["--head", HEAD_SHA]) == 0
    capsys.readouterr()
    assert sv.main(argv + ["--head", "e" * 40]) == 2
    assert "head_sha" in capsys.readouterr().err


# D-3564 slice B: --head is required for record seats and only for them.
def test_record_seat_requires_head(tmp_path, capsys):
    assert sv.main(record_dir(tmp_path, "q", live_log=REVIEW, head=None)) == 2
    assert "record seats require --head" in capsys.readouterr().err


def test_head_on_evidence_json_path_refused(tmp_path, capsys):
    argv = record_dir(tmp_path, "q2", live_log=REVIEW, head=None)
    seat = Path(argv[argv.index("--seat-dir") + 1])
    (seat / "evidence.json").write_text(json.dumps({
        "requested_model": "gpt-oss-120b", "served_model": "gpt-oss-120b",
        "finish_reason": "stop", "test_gaming": "no", "correlation_id": "c",
        "prompt_sha256": "ab" * 32}))
    (seat / "review.txt").write_text(REVIEW)
    assert sv.main(argv + ["--head", HEAD_SHA]) == 2
    assert "--head only for record seats" in capsys.readouterr().err


# F1: record writer_family (the seat's own family) never has to appear in the
# CLI writer list; only seat_verdict's coverage check decides.
def test_record_writer_family_not_in_cli_list_accepted(tmp_path, capsys):
    argv = record_dir(tmp_path, "w4", live_log=REVIEW, writer="alibaba",
                      writer_family="deepseek",
                      chosen_leg={"id": "leg-1", "tier": "subscription",
                                  "harness": "opencode",
                                  "model": "opencode/deepseek-v3-free",
                                  "family": "deepseek"})
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True
