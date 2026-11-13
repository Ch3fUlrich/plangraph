"""Late record persistence and the operator's transport-only repost path."""
import json
from pathlib import Path

import pytest

from test_seat_verdict_record import record_dir, HEAD_SHA, sv


@pytest.fixture
def run(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "run-" + "1" * 32,
                      item_key="demo:A", given_writer_families=["deepseek"])
    argv.remove("--dry-run")
    argv += ["--url", "unused"]
    seat = Path(argv[argv.index("--seat-dir") + 1])
    calls = []
    def send(url, token, payload):
        assert url == "unused" and token == "dummy"
        calls.append(payload)
        return 200, "recorded"
    monkeypatch.setattr(sv, "send", send)
    monkeypatch.setattr(sv.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    return argv, seat, calls


@pytest.mark.parametrize("field", ["head", "tail", "both", "live"])
def test_completed_record_reread_until_fields_persist(run, field):
    argv, seat, calls = run
    path = seat / "record.json"
    complete = json.loads(path.read_text())
    pending = dict(complete)
    if field in ("head", "both"):
        pending["completion"] = {}
    if field in ("tail", "both", "live"):
        pending["stdout_tail"] = ""
    if field == "live":
        (seat / "live.log").write_text('{"type":"step_start","part":{}}\n')
    path.write_text(json.dumps(pending))
    ticks, waits = [0], []
    def advance(seconds):
        waits.append(seconds)
        ticks[0] += seconds
        if len(waits) == 2:
            path.write_text(json.dumps(complete))
            if field == "live":
                (seat / "live.log").write_text(complete["stdout_tail"])
    assert sv.main(argv, clock=lambda: ticks[0], sleep=advance) == 0
    assert len(waits) == 2 and len(calls) == 1
    assert HEAD_SHA in calls[0]["reason"] and calls[0]["ok"] is True


def test_missing_fields_wait_is_bounded_by_default_thirty_seconds(run, capsys):
    argv, seat, calls = run
    path = seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {}
    path.write_text(json.dumps(record))
    ticks = [0]
    assert sv.main(argv, clock=lambda: ticks[0], sleep=lambda s: ticks.__setitem__(0, ticks[0] + s)) == 2
    assert ticks == [30] and not calls
    assert "completion.head_sha" in capsys.readouterr().err


def test_record_identity_rechecked_after_wait(run, capsys):
    argv, seat, calls = run
    path = seat / "record.json"
    record = json.loads(path.read_text())
    record.update(completion={})
    path.write_text(json.dumps(record))
    ticks = [0]
    def advance(seconds):
        ticks[0] += seconds
        record.update(item_key="demo:B", completion={"head_sha": HEAD_SHA})
        path.write_text(json.dumps(record))
    assert sv.main(argv, record_wait=2, clock=lambda: ticks[0], sleep=advance) == 2
    assert not calls
    assert "item_key does not match --key" in capsys.readouterr().err


def test_invalid_fields_refuse_without_waiting(run):
    argv, seat, calls = run
    path = seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {"head_sha": "invalid"}
    path.write_text(json.dumps(record))
    assert sv.main(argv, sleep=lambda _: pytest.fail("invalid SHA is not pending")) == 2
    assert calls == []


@pytest.mark.parametrize("head", [False, True])
def test_transport_retry_plain_command_posts_and_closes_outbox(run, head):
    argv, seat, calls = run
    for flag in ("--writer-family", "--head"):
        index = argv.index(flag)
        del argv[index:index + 2]
    if head:
        argv += ["--head", HEAD_SHA]
    (seat / "verdict.json").write_text(json.dumps({"status": "retry", "attempts": 1,
        "reason": "keeper verdict transport not configured"}))
    assert sv.main(argv) == 0
    assert len(calls) == 1 and calls[0]["key"] == "demo:A"
    assert calls[0]["ok"] is True
    assert json.loads((seat / "verdict.json").read_text())["status"] == "delivered"
    assert (seat / "verdict.delivered").exists()
    assert sv.main(argv) != 0
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["head", "writers", "gaming", "ambiguous"])
def test_transport_repost_keeps_verifier_and_outbox_gates(run, change):
    argv, seat, calls = run
    index = argv.index("--writer-family")
    del argv[index:index + 2]
    journal = {"status": "retry", "attempts": 1,
               "reason": "keeper verdict transport not configured"}
    path = seat / "record.json"
    record = json.loads(path.read_text())
    if change == "head":
        argv[argv.index("--head") + 1] = "b" * 40
    elif change == "writers":
        record["given_writer_families"] = ["xiaomi"]
    elif change == "gaming":
        record["stdout_tail"] = "TEST_GAMING: yes\nVERDICT: PASS"
    else:
        journal["status"] = "sending"
    path.write_text(json.dumps(record))
    (seat / "verdict.json").write_text(json.dumps(journal))
    assert sv.main(argv) != 0
    assert calls == []
