"""Bounded late-persistence retries must post only the newly witnessed review."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_seat_verdict_record import record_dir, HEAD_SHA, REVIEW, seat_record, sv
from test_seat_record_opencode import event, event_log


@pytest.fixture
def run(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "run-" + "1" * 32, item_key="demo:A")
    argv.remove("--dry-run")
    argv += ["--url", "unused"]
    state = SimpleNamespace(argv=argv, seat=Path(argv[argv.index("--seat-dir") + 1]),
                            calls=[], ticks=0, waits=[], reads=0, after_wait=lambda: None)
    original = sv.evidence_from_record
    def read(*args, **kwargs):
        state.reads += 1
        return original(*args, **kwargs)
    def send(url, token, payload):
        assert url == "unused" and token == "dummy"
        state.calls.append(payload)
        return 200, "recorded"
    def advance(seconds):
        assert not state.calls, "never post the stale tail before persistence"
        state.waits.append(seconds)
        state.ticks += seconds
        state.after_wait()
    def post(wait=2.5):
        # Exercise base behaviour too: red proof must not be a missing-keyword error.
        options = {"clock": lambda: state.ticks, "sleep": advance} if sv.main.__kwdefaults__ else {}
        if options and wait is not None:
            options["record_wait"] = wait
        try:
            return sv.main(argv, **options)
        except UnicodeDecodeError as exc:
            raise AssertionError("torn UTF-8 must retry, not escape the poster") from exc
    state.post = post
    monkeypatch.setattr(sv, "evidence_from_record", read)
    monkeypatch.setattr(sv, "send", send)
    monkeypatch.setattr(sv.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    return state


@pytest.mark.parametrize("field", ["head", "tail", "both", "live"])
def test_completed_record_reread_until_fields_persist(run, field):
    path = run.seat / "record.json"
    complete = json.loads(path.read_text())
    pending = dict(complete)
    if field in ("head", "both"):
        pending["completion"] = {}
    if field in ("tail", "both", "live"):
        pending["stdout_tail"] = ""
    if field == "live":
        (run.seat / "live.log").write_text(event_log(event("step_start")))
    path.write_text(json.dumps(pending))
    def persist():
        if len(run.waits) == 2:
            path.write_text(json.dumps(complete))
            if field == "live":
                (run.seat / "live.log").write_text(REVIEW)
    run.after_wait = persist
    assert run.post() == 0
    assert run.waits == [1, 1] and run.ticks == 2 and run.reads == 3
    assert len(run.calls) == 1 and run.calls[0]["ok"] is True
    assert HEAD_SHA in run.calls[0]["reason"]


@pytest.mark.parametrize("settles", [False, True])
@pytest.mark.parametrize("attempts,initial", [
    (2, None), (2, b""), (2, b" \t\n"), (1, b"\xe2\x82"), (2, b"\xe2\x82"),
    *((1, event_log(event("step_start"), *([] if blank is None else
       [event("text", text=blank)]), *([] if finish is None else
       [event("step_finish", reason=finish)])).encode())
      for blank in (None, "", " \t\n") for finish in (None, "stop", "length", "tool-calls"))])
def test_late_live_log_rereads_without_stale_tail(run, capsys, attempts, initial, settles):
    path, live = run.seat / "record.json", run.seat / "live.log"
    record = json.loads(path.read_text())
    assert record["stdout_tail"] == REVIEW
    record["attempts"] *= attempts
    path.write_text(json.dumps(record))
    if initial is not None:
        live.write_bytes(initial)
    if settles:
        run.after_wait = lambda: live.write_text(event_log(event("step_start"),
            event("text", text="persisted final finding\nVERDICT: FIX"),
            event("step_finish", reason="stop")))
    else:
        try:
            seat_record.evidence_from_record(run.seat)
        except ValueError as exc:
            assert isinstance(exc, getattr(seat_record, "RecordPending", ()))
        else:
            pytest.fail("missing live review must be pending, not the stale tail")
    assert run.post() == (0 if settles else sv.EXIT_REFUSED)
    assert run.waits == ([1] if settles else [1, 1, 0.5])
    assert run.ticks == (1 if settles else 2.5) and run.ticks <= 2.5
    assert run.reads == (2 if settles else 4)
    captured = capsys.readouterr()
    if settles:
        assert len(run.calls) == 1 and run.calls[0]["ok"] is False
        assert "persisted final finding" in run.calls[0]["reason"] and not captured.err
    else:
        assert not run.calls and not captured.out
        assert captured.err.count("seat_verdict: refused, record:") == 1
        assert any(reason in captured.err for reason in
                   ("live.log required", "UnicodeDecodeError", "no final text in live.log"))


def test_deleted_record_mid_wait_refuses_without_full_timeout(run, capsys):
    path = run.seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {}
    path.write_text(json.dumps(record))
    run.after_wait = path.unlink
    assert run.post() == sv.EXIT_REFUSED
    assert run.waits == [1] and run.ticks == 1 and run.reads == 2
    captured = capsys.readouterr()
    assert "record.json unreadable: FileNotFoundError" in captured.err
    assert captured.err.count("refused") == 1 and not captured.out and not run.calls
    with pytest.raises(ValueError) as error:
        seat_record.evidence_from_record(run.seat)
    assert type(error.value) is ValueError


@pytest.mark.parametrize("read_number", [1, 2])
@pytest.mark.parametrize("failure", ["torn", "unreadable"])
@pytest.mark.parametrize("settles", [False, True])
def test_record_read_errors_wait_for_complete_persistence(
        run, monkeypatch, capsys, read_number, failure, settles):
    path, read_text, reads = run.seat / "record.json", Path.read_text, []
    def read(self, *args, **kwargs):
        if self == path:
            reads.append(self)
            if len(reads) == read_number or not settles and len(reads) > read_number:
                if failure == "unreadable":
                    raise PermissionError("persistence in progress")
                return "{"
        return read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert run.post(wait=None) == (0 if settles else sv.EXIT_REFUSED)
    assert run.ticks == (1 if settles else 30)
    assert run.waits == [1] * (1 if settles else 30)
    assert len(reads) == read_number + (2 if settles else 30)
    assert len(run.calls) == (1 if settles else 0)
    assert capsys.readouterr().err.count("refused") == (0 if settles else 1)


def test_record_identity_rechecked_after_wait(run, capsys):
    path = run.seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {}
    path.write_text(json.dumps(record))
    def persist():
        record.update(item_key="demo:B", completion={"head_sha": HEAD_SHA})
        path.write_text(json.dumps(record))
    run.after_wait = persist
    assert run.post() == sv.EXIT_REFUSED
    assert run.waits == [1] and run.ticks == 1 and not run.calls
    assert "item_key does not match --key" in capsys.readouterr().err


def test_invalid_fields_refuse_without_waiting(run, capsys):
    path = run.seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {"head_sha": HEAD_SHA + "\n"}
    path.write_text(json.dumps(record))
    assert run.post() == sv.EXIT_REFUSED
    assert not run.waits and run.ticks == 0 and not run.calls
    assert "completion.head_sha" in capsys.readouterr().err
