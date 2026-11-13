"""D-5624: recorded finish events feed the real seat verdict posting gate."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_record  # noqa: E402
import seat_verdict as sv  # noqa: E402

HEAD_SHA = "a" * 40
REVIEW = "review complete\nTEST_GAMING: no\nVERDICT: PASS\n"
FINISH_EVENT = "opencode_step_finish"


def record_dir(tmp_path, **fields):
    seat = tmp_path / ("run-" + "1" * 32)
    seat.mkdir(mode=0o700)
    record = {"run_id": seat.name, "role": "review", "status": "completed",
              "exit_code": 0, "served_model": "unwitnessed",
              "chosen_leg": {"model": "opencode/mimo-v2.6-free", "family": "mimo"},
              "attempts": [{}], "stdout_tail": REVIEW,
              "completion": {"head_sha": HEAD_SHA}}
    record.update(fields)
    (seat / "record.json").write_text(json.dumps(record), encoding="utf-8")
    (seat / "record.json").chmod(0o600)
    return seat


@pytest.fixture
def poster(tmp_path, monkeypatch):
    """Exercise main without dry-run, replacing only the network boundary."""
    calls = []
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "reviewer-xiaomi.token").write_text("dummy\n", encoding="utf-8")

    def no_network(*args, **kwargs):
        pytest.fail("finish-reason tests must not access the network")

    def send(url, token, payload):
        assert url == "unused"
        assert token == "dummy"
        calls.append(payload)
        return 200, "recorded"

    monkeypatch.setattr(sv.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(sv, "send", send)

    def post(seat):
        return sv.main(["--key", "demo:A", "--seat-dir", str(seat),
                        "--writer-family", "deepseek", "--head", HEAD_SHA,
                        "--token-dir", str(tokens), "--url", "unused"])

    return post, calls


@pytest.mark.parametrize("finish", ["stop", "end_turn"])
def test_events_only_finish_posts_pass(tmp_path, poster, capsys, finish):
    seat = record_dir(tmp_path, events=[
        {"event": FINISH_EVENT, "finish_reason": finish},
        {"event": "other", "finish_reason": "length"}])
    record = json.loads((seat / "record.json").read_text("utf-8"))
    assert "finish_reason" not in record
    assert "finish_reason" not in record["completion"]

    evidence, text = seat_record.evidence_from_record(seat)

    assert evidence["finish_reason"] == finish
    assert evidence["test_gaming"] == "no"
    assert text == REVIEW
    post, calls = poster
    assert post(seat) == 0
    assert len(calls) == 1
    assert calls[0]["key"] == "demo:A"
    assert calls[0]["ok"] is True
    assert HEAD_SHA in calls[0]["reason"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("fields", [
    {}, {"events": []}, {"events": None}, {"events": "bad"},
    {"events": {}}, {"events": 42}, {"events": True},
    {"events": [{"event": "other", "finish_reason": "stop"}]}])
def test_absent_or_malformed_finish_events_refuse_pass(tmp_path, poster, capsys, fields):
    seat = record_dir(tmp_path, **fields)
    evidence, _ = seat_record.evidence_from_record(seat)
    assert "finish_reason" not in evidence
    post, calls = poster
    assert post(seat) == sv.EXIT_PASS_REFUSED
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pass refused (finish_reason not stop or end_turn" in captured.err


@pytest.mark.parametrize("finish", ["length", "tool-calls", "tool_calls", "recorded-other"])
def test_last_finish_event_wins_and_refuses_pass(tmp_path, poster, capsys, finish):
    seat = record_dir(tmp_path, events=[
        {"event": FINISH_EVENT, "finish_reason": "stop"},
        {"event": FINISH_EVENT, "finish_reason": finish},
        {"event": "other", "finish_reason": "stop"}])
    evidence, _ = seat_record.evidence_from_record(seat)
    assert evidence["finish_reason"] == finish
    post, calls = poster
    assert post(seat) == sv.EXIT_PASS_REFUSED
    assert calls == []
    assert "pass refused" in capsys.readouterr().err


@pytest.mark.parametrize("fields,expected,accepted", [
    ({"finish_reason": "length", "completion": {"head_sha": HEAD_SHA,
      "finish_reason": "stop"}}, "length", False),
    ({"finish_reason": "stop", "completion": {"head_sha": HEAD_SHA,
      "finish_reason": "length"}}, "stop", True),
    ({"completion": {"head_sha": HEAD_SHA, "finish_reason": "length"}}, "length", False),
    ({"completion": {"head_sha": HEAD_SHA, "finish_reason": "end_turn"}}, "end_turn", True),
])
def test_record_then_completion_finish_precedes_events(
        tmp_path, poster, capsys, fields, expected, accepted):
    seat = record_dir(tmp_path, events=[{"event": FINISH_EVENT, "finish_reason": "stop"}],
                      **fields)
    evidence, _ = seat_record.evidence_from_record(seat)
    assert evidence["finish_reason"] == expected
    post, calls = poster
    assert post(seat) == (0 if accepted else sv.EXIT_PASS_REFUSED)
    assert len(calls) == int(accepted)
    if accepted:
        assert calls[0]["ok"] is True
    else:
        assert "pass refused" in capsys.readouterr().err


@pytest.mark.parametrize("fields", [
    {}, {"finish_reason": None}, {"finish_reason": ""}, {"finish_reason": 0},
    {"finish_reason": True}, {"finish_reason": ["stop"]}, {"finish_reason": {"stop": True}},
])
def test_unusable_last_finish_never_uses_earlier_stop(tmp_path, poster, capsys, fields):
    seat = record_dir(tmp_path, events=[
        {"event": FINISH_EVENT, "finish_reason": "stop"},
        {"event": FINISH_EVENT, **fields}])
    evidence, _ = seat_record.evidence_from_record(seat)
    assert "finish_reason" not in evidence
    post, calls = poster
    assert post(seat) == sv.EXIT_PASS_REFUSED
    assert calls == []
    assert "pass refused" in capsys.readouterr().err


@pytest.mark.parametrize("unset", [None, "", 0, False, [], {}])
def test_unusable_record_fields_allow_recorded_event(tmp_path, poster, unset):
    seat = record_dir(tmp_path, finish_reason=unset,
                      completion={"head_sha": HEAD_SHA, "finish_reason": unset},
                      events=[{"event": FINISH_EVENT, "finish_reason": "stop"}])
    evidence, _ = seat_record.evidence_from_record(seat)
    assert evidence["finish_reason"] == "stop"
    post, calls = poster
    assert post(seat) == 0
    assert len(calls) == 1
    assert calls[0]["ok"] is True


def test_malformed_event_entries_are_ignored_without_raising(tmp_path, poster):
    malformed = [None, 42, True, "opencode_step_finish", [], [FINISH_EVENT], {},
                 {"event": [FINISH_EVENT], "finish_reason": "stop"},
                 {"event": {"name": FINISH_EVENT}, "finish_reason": "stop"},
                 {"finish_reason": "stop"}]
    seat = record_dir(tmp_path, events=[
        *malformed, {"event": FINISH_EVENT},
        {"event": FINISH_EVENT, "finish_reason": 42},
        {"event": FINISH_EVENT, "finish_reason": "end_turn"}, *malformed])
    evidence, _ = seat_record.evidence_from_record(seat)
    assert evidence["finish_reason"] == "end_turn"
    post, calls = poster
    assert post(seat) == 0
    assert len(calls) == 1
    assert calls[0]["ok"] is True
