import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from test_seat_verdict_record import record_dir, HEAD_SHA, sv
import seat_backfill as sb
@pytest.fixture
def run(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "repost", item_key="demo:A", given_writer_families=["deepseek"])
    index = argv.index("--writer-family")
    del argv[index:index + 2]
    argv.remove("--dry-run")
    argv += ["--url", "unused", "--repost-transport"]
    seat, calls = Path(argv[argv.index("--seat-dir") + 1]), []
    (seat / "verdict.json").write_text('{"status":"retry","attempts":1,"reason":"keeper verdict transport not configured"}\n')
    monkeypatch.setattr(sv, "send", lambda *a: (calls.append(a) or (200, "recorded")))
    monkeypatch.setattr(sv.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network forbidden"))
    return argv, seat, calls

def test_diagnosis_documented_transport_repost_command_posts(run):
    argv, seat, calls = run
    command = (Path(__file__).resolve().parents[1] / "docs/seat-verdict-backfill-diagnosis.md").read_text().split("`python3 tools/seat_verdict.py ")[1].split("`")[0]
    values = dict(K="demo:A", RUN=str(seat), URL="unused", DIR=argv[argv.index("--token-dir") + 1])
    assert sv.main([values.get(a, a) for a in command.removesuffix(" [--head FULLSHA]").split()]) == 0
    assert len(calls) == 1 and json.loads((seat / "verdict.json").read_text())["status"] == "delivered"
    assert (seat / "verdict.delivered").exists() and sv.main(argv) == 1 and len(calls) == 1

def test_transport_repost_preserves_subprocess_import_order(run):
    argv, _, _ = run
    code = """import sys
sys.path.insert(0, sys.argv[1] + '/src')
import plangraph.spawner.notify
sys.path.insert(0, sys.argv[1] + '/tools')
import seat_verdict as sv
sys.path.remove(sys.argv[1] + '/src')
sv.send = lambda *a: (200, 'recorded')
before = sys.path[:]
assert sys.argv[1] + '/src' not in before
assert sv.main(sys.argv[2:]) == 0
assert sys.path == before
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1]), *argv], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

def test_transport_repost_token_read_refusal_preserves_retry(run, capsys):
    argv, seat, calls = run
    record = json.loads((seat / "record.json").read_text())
    assert record["given_writer_families"] == ["deepseek"]
    assert sv.model_family(record["chosen_leg"]["model"]) == "xiaomi"
    before = (seat / "verdict.json").read_bytes()
    (Path(argv[argv.index("--token-dir") + 1]) / "reviewer-xiaomi.token").unlink()
    assert sv.main(argv) == sv.EXIT_IO and calls == []
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()
    assert "seat_verdict: repost refused: poster exit 1" in capsys.readouterr().err

@pytest.mark.parametrize("failure", ["missing-url", "empty-url", "dry-run"])
def test_transport_repost_requires_url_without_dry_run(run, monkeypatch, capsys, failure):
    argv, seat, calls = run
    monkeypatch.delenv("PG_URL", raising=False)
    if failure == "missing-url":
        index = argv.index("--url")
        del argv[index:index + 2]
    elif failure == "empty-url":
        argv[argv.index("--url") + 1] = ""
    else:
        argv.append("--dry-run")
    before = (seat / "verdict.json").read_bytes()
    with pytest.raises(SystemExit) as exc: sv.main(argv)
    assert exc.value.code == 2 and "requires --url" in capsys.readouterr().err
    assert calls == [] and (seat / "verdict.json").read_bytes() == before
    assert not (seat / "verdict.json.lock").exists() and not (seat / "verdict.delivered").exists()

def test_transport_repost_evidence_json_refusal_explains_cause(run, capsys):
    argv, seat, calls = run
    (seat / "evidence.json").write_text("{}")
    before = (seat / "verdict.json").read_bytes()
    assert sv.main(argv) == 1 and calls == []
    error = capsys.readouterr().err
    assert "evidence.json exists" in error and "dry-run" not in error
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()

@pytest.mark.parametrize("failure", ["argv", "exit", "reason"])
def test_transport_repost_invalid_argv_preserves_journal_before_apply(run, failure):
    _, seat, calls = run
    journal, row = seat / "verdict.json", {"reasons": []}
    if failure == "reason": journal.write_text('{"status":"retry","attempts":1,"reason":"other"}')
    before = journal.read_bytes()
    def poster(_):
        sb._SEND_STATE.get()["started"] = False
        calls.append(True)
        raise SystemExit(2)
    args = SimpleNamespace(backfill_apply=True, url="unused", token_dir=seat)
    assert sb.apply_backfill(args, {"key": "demo:A", "writer_families": ["deepseek"]},
        {"seat_dir": seat, "head_sha": "-head" if failure == "argv" else HEAD_SHA}, poster, row,
        retry_reason="keeper verdict transport not configured") == 1
    assert journal.read_bytes() == before
    assert calls == ([True] if failure == "exit" else [])
    if failure == "reason": assert "journal is not a transport-only retry" in row["reasons"]

def test_transport_repost_send_raising_keeps_retry_journal(run, monkeypatch):
    argv, seat, calls = run
    before = (seat / "verdict.json").read_bytes()
    def boom(*a):
        calls.append(a)
        raise OSError("network down")
    monkeypatch.setattr(sv, "send", boom)
    assert sv.main(argv) == 1 and len(calls) == 1
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()
    monkeypatch.setattr(sv, "send", lambda *a: (calls.append(a) or (200, "recorded")))
    assert sv.main(argv) == 0 and len(calls) == 2
    assert json.loads((seat / "verdict.json").read_text())["status"] == "delivered"
    assert (seat / "verdict.delivered").exists()

def test_transport_repost_unexpected_send_error_keeps_retry_journal(run, monkeypatch):
    argv, seat, calls = run
    before = (seat / "verdict.json").read_bytes()
    def boom(*a): raise RuntimeError("boom")
    monkeypatch.setattr(sv, "send", boom)
    assert sv.main(argv) == 1
    assert (seat / "verdict.json").read_bytes() == before and not (seat / "verdict.delivered").exists()

@pytest.mark.parametrize("rc", [0, 1])
def test_non_repost_apply_backfill_unchanged(run, rc):
    _, seat, _ = run
    (seat / "verdict.json").unlink()
    row = {"reasons": []}
    args = SimpleNamespace(backfill_apply=True, url="unused", token_dir=seat)
    assert sb.apply_backfill(args, {"key": "demo:A", "writer_families": ["deepseek"]},
        {"seat_dir": seat, "head_sha": HEAD_SHA}, lambda _: rc, row) == rc
    assert row["reasons"] == [] and row["delivery"] == ("failed" if rc else "delivered")
    assert json.loads((seat / "verdict.json").read_text())["status"] == ("skipped" if rc else "delivered")

def test_non_repost_poster_systemexit_propagates(run):
    _, seat, _ = run
    (seat / "verdict.json").unlink()
    def poster(_): raise SystemExit(2)
    args = SimpleNamespace(backfill_apply=True, url="unused", token_dir=seat)
    with pytest.raises(SystemExit):
        sb.apply_backfill(args, {"key": "demo:A", "writer_families": ["deepseek"]},
            {"seat_dir": seat, "head_sha": HEAD_SHA}, poster, {"reasons": []})

"""Bounded late-persistence retries must post only the newly witnessed review."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_seat_verdict_record import record_dir, HEAD_SHA, REVIEW, seat_record, sv
from test_seat_record_opencode import event, event_log


@pytest.fixture
def pending_run(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "run-" + "1" * 32, item_key="demo:A")
    argv.remove("--dry-run")
    argv += ["--url", "unused"]
    state = SimpleNamespace(argv=argv, seat=Path(argv[argv.index("--seat-dir") + 1]),
                            calls=[], ticks=0, waits=[], reads=0, clock_reads=0, after_wait=lambda: None)
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
    def clock():
        state.clock_reads += 1
        return state.ticks
    def post(wait=2.5):
        assert sv.main.__kwdefaults__, "bounded retry clock/sleep injection required; no real waits"
        options = {"clock": clock, "sleep": advance}
        if wait is not None:
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
def test_completed_record_reread_until_fields_persist(pending_run, field):
    path = pending_run.seat / "record.json"
    complete = json.loads(path.read_text())
    pending = dict(complete)
    if field in ("head", "both"):
        pending["completion"] = {}
    if field in ("tail", "both", "live"):
        pending["stdout_tail"] = ""
    if field == "live":
        (pending_run.seat / "live.log").write_text(event_log(event("step_start")))
    path.write_text(json.dumps(pending))
    def persist():
        if len(pending_run.waits) == 2:
            path.write_text(json.dumps(complete))
            if field == "live":
                (pending_run.seat / "live.log").write_text(REVIEW)
    pending_run.after_wait = persist
    assert pending_run.post() == 0
    assert pending_run.waits == [1, 1] and pending_run.ticks == 2 and pending_run.reads == 3
    assert len(pending_run.calls) == 1 and pending_run.calls[0]["ok"] is True
    assert HEAD_SHA in pending_run.calls[0]["reason"]


@pytest.mark.parametrize("settles", [False, True])
@pytest.mark.parametrize("initial,reason", [
    *((initial, "stdout_tail accumulates every leg; live.log required")
      for initial in (None, b"", b" \t\n")),
    (b"\xe2\x82", "live.log unreadable: UnicodeDecodeError"),
    *((event_log(event("step_start"), malformed).encode(), "record has no review text (no final text in live.log)")
      for malformed in ({"type": "step_finish"}, *({"type": "step_finish", "part": part}
                                                 for part in (None, [], "x", 42)))),
    *((event_log(event("step_start"), *([] if blank is None else
       [event("text", text=blank)]), *([] if finish is None else
        [event("step_finish", reason=finish)])).encode(), "record has no review text (no final text in live.log)")
       for blank in (None, "", " \t\n") for finish in (None, "stop", "length", "tool-calls"))])
def test_late_live_log_rereads_without_stale_tail(pending_run, capsys, initial, reason, settles):
    path, live = pending_run.seat / "record.json", pending_run.seat / "live.log"
    record = json.loads(path.read_text())
    assert record["stdout_tail"] == REVIEW
    if initial is None or not initial.strip():
        record["attempts"] *= 2  # Only multi-attempt records require a nonempty live.log.
    path.write_text(json.dumps(record))
    if initial is not None:
        live.write_bytes(initial)
    if settles:
        pending_run.after_wait = lambda: live.write_text(event_log(event("step_start"),
            event("text", text="persisted final finding\nVERDICT: FIX"),
            event("step_finish", reason="stop")))
    else:
        try:
            seat_record.evidence_from_record(pending_run.seat)
        except ValueError as exc:
            assert isinstance(exc, getattr(seat_record, "RecordPending", ()))
        else:
            pytest.fail("missing live review must be pending, not the stale tail")
    assert pending_run.post() == (0 if settles else sv.EXIT_REFUSED)
    assert pending_run.waits == ([1] if settles else [1, 1, 0.5])
    assert pending_run.ticks == (1 if settles else 2.5) and pending_run.ticks <= 2.5
    assert pending_run.reads == (2 if settles else 4)
    captured = capsys.readouterr()
    if settles:
        assert len(pending_run.calls) == 1 and pending_run.calls[0]["ok"] is False
        assert "persisted final finding" in pending_run.calls[0]["reason"] and not captured.err
    else:
        assert not pending_run.calls and not captured.out
        assert captured.err.count("seat_verdict: refused, record:") == 1
        assert captured.err == f"seat_verdict: refused, record: {reason}\n"


def test_deleted_record_mid_wait_refuses_without_full_timeout(pending_run, capsys):
    path = pending_run.seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {}
    path.write_text(json.dumps(record))
    pending_run.after_wait = path.unlink
    assert pending_run.post() == sv.EXIT_REFUSED
    assert pending_run.waits == [1] and pending_run.ticks == 1 and pending_run.reads == 2
    captured = capsys.readouterr()
    assert "record.json unreadable: FileNotFoundError" in captured.err
    assert captured.err.count("refused") == 1 and not captured.out and not pending_run.calls
    with pytest.raises(ValueError) as error:
        seat_record.evidence_from_record(pending_run.seat)
    assert type(error.value) is ValueError


@pytest.mark.parametrize("read_number", [1, 2])
@pytest.mark.parametrize("failure", ["torn", "unreadable"])
@pytest.mark.parametrize("settles", [False, True])
def test_record_read_errors_wait_for_complete_persistence(
        pending_run, monkeypatch, capsys, read_number, failure, settles):
    path, read_text, reads = pending_run.seat / "record.json", Path.read_text, []
    def read(self, *args, **kwargs):
        if self == path:
            reads.append(self)
            if len(reads) == read_number or not settles and len(reads) > read_number:
                if failure == "unreadable":
                    raise PermissionError("persistence in progress")
                return "{"
        return read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert pending_run.post(wait=None) == (0 if settles else sv.EXIT_REFUSED)
    assert pending_run.ticks == (1 if settles else 30)
    assert pending_run.waits == [1] * (1 if settles else 30)
    assert len(reads) == read_number + (2 if settles else 30)
    assert len(pending_run.calls) == (1 if settles else 0)
    assert capsys.readouterr().err.count("refused") == (0 if settles else 1)


def test_record_identity_rechecked_after_wait(pending_run, capsys):
    path = pending_run.seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {}
    path.write_text(json.dumps(record))
    def persist():
        record.update(item_key="demo:B", completion={"head_sha": HEAD_SHA})
        path.write_text(json.dumps(record))
    pending_run.after_wait = persist
    assert pending_run.post() == sv.EXIT_REFUSED
    assert pending_run.waits == [1] and pending_run.ticks == 1 and not pending_run.calls
    assert "item_key does not match --key" in capsys.readouterr().err


def test_invalid_fields_refuse_without_waiting(pending_run, capsys):
    path = pending_run.seat / "record.json"
    record = json.loads(path.read_text())
    record["completion"] = {"head_sha": HEAD_SHA + "\n"}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="completion.head_sha") as error:
        seat_record.evidence_from_record(pending_run.seat)
    assert type(error.value) is ValueError  # Invalid, not pending; strict fullmatch is intentional.
    assert pending_run.post() == sv.EXIT_REFUSED
    assert not pending_run.waits and pending_run.ticks == 0 and not pending_run.calls
    assert pending_run.reads == 1 and pending_run.clock_reads == 2  # Deadline set, one terminal read, no retry.
    assert "completion.head_sha" in capsys.readouterr().err
