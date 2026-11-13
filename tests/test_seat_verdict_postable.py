"""Late record persistence and the operator's transport-only repost path."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from test_seat_verdict_record import record_dir, HEAD_SHA, REVIEW, sv
import seat_backfill as sb


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setitem(sv.main.__kwdefaults__, "sleep", lambda _: pytest.fail("unit test slept"))
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


@pytest.mark.parametrize("read_number", [1, 2])
@pytest.mark.parametrize("failure", ["torn", "unreadable"])
def test_record_read_errors_wait_for_complete_persistence(run, monkeypatch, read_number, failure):
    argv, seat, calls = run
    path, read_text, reads = seat / "record.json", Path.read_text, []
    def read(self, *a, **kw):
        if self == path:
            reads.append(self)
            if len(reads) == read_number:
                if failure == "unreadable":
                    raise PermissionError("persistence in progress")
                return "{"
        return read_text(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", read)
    ticks = [0]
    assert sv.main(argv, clock=lambda: ticks[0],
                   sleep=lambda s: ticks.__setitem__(0, ticks[0] + s)) == 0
    assert ticks == [1] and len(calls) == 1


@pytest.mark.parametrize("writers", [[], ["--writer-famliy", "deepseek"]])
def test_omitted_writer_family_requires_explicit_repost(run, monkeypatch, writers):
    argv, seat, calls = run
    index = argv.index("--writer-family")
    del argv[index:index + 2]
    (seat / "verdict.json").write_text(json.dumps({"status": "retry", "attempts": 1,
        "reason": "keeper verdict transport not configured"}))
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("usage must not read files"))
    with pytest.raises(SystemExit) as exc:
        sv.main(argv + writers)
    assert exc.value.code == 2 and calls == []


@pytest.mark.parametrize("head", [False, True])
def test_transport_retry_plain_command_posts_and_closes_outbox(run, head):
    argv, seat, calls = run
    argv += ["--repost-transport"]
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


@pytest.mark.parametrize("change", ["head", "writers", "gaming", "verdict", "identity", "ambiguous"])
def test_transport_repost_keeps_verifier_and_outbox_gates(run, change):
    argv, seat, calls = run
    argv += ["--repost-transport"]
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
    elif change == "verdict":
        record["stdout_tail"] = "no decision"
    elif change == "identity":
        record["item_key"] = "demo:B"
    else:
        journal["status"] = "sending"
    path.write_text(json.dumps(record))
    (seat / "verdict.json").write_text(json.dumps(journal))
    assert sv.main(argv) != 0
    assert calls == []
    assert json.loads((seat / "verdict.json").read_text()) == journal
    assert not (seat / "verdict.delivered").exists()
    if change != "ambiguous":
        argv[argv.index("--head") + 1] = HEAD_SHA
        record.update(item_key="demo:A", given_writer_families=["deepseek"], stdout_tail=REVIEW)
        path.write_text(json.dumps(record))
        assert sv.main(argv) == 0 and len(calls) == 1
        assert json.loads((seat / "verdict.json").read_text())["status"] == "delivered"


@pytest.mark.parametrize("error", [AttributeError("field"), KeyError("field"), SystemExit(2)])
@pytest.mark.parametrize("source", ["apply", "poster"])
def test_transport_repost_reports_unexpected_apply_errors(run, monkeypatch, capsys, error, source):
    argv, seat, calls = run
    args = SimpleNamespace(seat_dir=seat, dry_run=False, head=HEAD_SHA,
                           url="unused", token_dir=seat, key="demo:A")
    def fail(*a, **kw):
        raise error
    if source == "apply":
        monkeypatch.setattr(sb, "apply_backfill", fail)
    before = list(sys.path)
    for _ in range(2):
        (seat / "verdict.json").write_text(json.dumps({"status": "retry", "attempts": 1,
            "reason": "keeper verdict transport not configured"}))
        assert sb.repost_transport(args, fail if source == "poster" else sv.main) == 1
        assert "repost refused:" in capsys.readouterr().err
    assert sys.path == before and calls == []


def test_transport_repost_head_mismatch_refused_before_apply(run, monkeypatch):
    _, seat, calls = run
    args = SimpleNamespace(seat_dir=seat, dry_run=False, head="b" * 40,
                           url="unused", token_dir=seat, key="demo:A")
    monkeypatch.setattr(sb, "apply_backfill", lambda *a, **kw: pytest.fail("head mismatch reached apply"))
    assert sb.repost_transport(args, sv.main) == 1 and calls == []


@pytest.mark.parametrize("result,status", [(sv.EXIT_IO, "skipped"), ("crash", "sending")])
def test_transport_repost_uncertain_send_never_restores_retry(run, result, status):
    _, seat, calls = run
    args = SimpleNamespace(seat_dir=seat, dry_run=False, head=HEAD_SHA,
                           url="unused", token_dir=seat, key="demo:A")
    (seat / "verdict.json").write_text(json.dumps({"status": "retry", "attempts": 1,
        "reason": "keeper verdict transport not configured"}))
    def poster(argv):
        calls.append(argv)
        if result == "crash":
            raise RuntimeError("send interrupted")
        return result
    assert sb.repost_transport(args, poster) == 1
    assert json.loads((seat / "verdict.json").read_text())["status"] == status
    assert sb.repost_transport(args, poster) == 1 and len(calls) == 1


@pytest.mark.parametrize("mode", ["--backfill-dry-run", "--backfill-apply", "--writer-family"])
def test_transport_repost_rejects_conflicting_modes(run, mode):
    argv, _, calls = run
    if mode != "--writer-family":
        index = argv.index("--writer-family")
        del argv[index:index + 2]
        argv.append(mode)
    with pytest.raises(SystemExit) as exc:
        sv.main(argv + ["--repost-transport"])
    assert exc.value.code == 2 and calls == []
