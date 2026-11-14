"""Portable record metadata checks through the real poster, with mocked HTTP."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_seat_verdict_record import record_dir, seat_record, sv


@pytest.fixture
def posting(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "run-" + "1" * 32)
    argv.remove("--dry-run")
    argv.extend(["--url", "http://keeper.invalid"])
    sent = []

    def send(url, token, payload):
        assert url == "http://keeper.invalid" and token == "dummy"
        assert payload["key"] == "demo:A" and payload["ok"] is True
        sent.append(payload)
        return 200, "ok"

    monkeypatch.setattr(sv, "send", send)
    return argv, tmp_path / ("run-" + "1" * 32), sent


@pytest.mark.parametrize("has_getuid", [False, True])
def test_windows_record_skips_posix_owner_and_mode(posting, monkeypatch, has_getuid):
    argv, seat, sent = posting
    (seat / "record.json").chmod(0o777)

    def forbidden():
        pytest.fail("Windows must not call getuid")

    platform = SimpleNamespace(name="nt", **({"getuid": forbidden} if has_getuid else {}))
    monkeypatch.setattr(seat_record, "os", platform)
    assert sv.main(argv) == 0
    assert len(sent) == 1


def test_drive_mount_still_requires_linux_owner(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    uid = (seat / "record.json").stat().st_uid
    # Compare the same foreign owner on Windows and POSIX, including drive mounts.
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="nt", getuid=lambda: uid + 1))
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid + 1))
    monkeypatch.setattr(Path, "absolute", lambda self: Path("/mnt/c") / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "owned by the current uid" in capsys.readouterr().err
    assert len(sent) == 1


@pytest.mark.parametrize("mode", [0o620, 0o602], ids=["group", "other"])
@pytest.mark.parametrize("root", ["/native", "/mnt/data"])
def test_posix_record_group_or_other_writable_refused(posting, monkeypatch, capsys, mode, root):
    argv, seat, sent = posting
    path = seat / "record.json"
    path.chmod(mode)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="nt"))
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    monkeypatch.setattr(Path, "absolute", lambda self: Path(root) / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "not group/world-writable" in capsys.readouterr().err
    assert len(sent) == 1


@pytest.mark.parametrize("drive", ["c", "Z"])
def test_native_record_link_into_drive_mount_keeps_mode_checks(
        posting, tmp_path, monkeypatch, capsys, drive):
    argv, seat, sent = posting
    path, target = seat / "record.json", tmp_path / "target.json"
    path.rename(target)
    target.chmod(0o666)
    path.symlink_to(target)
    uid = target.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    mounted = Path("/mnt") / drive / seat.name / path.name
    monkeypatch.setattr(Path, "absolute", lambda self: mounted)
    monkeypatch.setattr(Path, "resolve", lambda self: mounted)
    # The real target is on the drive, including a native lexical alias.
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(Path, "absolute", lambda self: Path("/native") / seat.name / self.name)
    assert sv.main(argv) == 0
    assert not capsys.readouterr().err
    assert len(sent) == 2


def test_mnt_prefix_with_dotdot_is_not_a_drive_path(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    path = seat / "record.json"
    path.chmod(0o666)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    monkeypatch.setattr(Path, "absolute", lambda self: Path("/mnt/c/../../home") / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "not group/world-writable" in capsys.readouterr().err
    assert not sent
"""Pending persistence and retention of a completed review, using synthetic logs."""
import asyncio
import json

import pytest

from test_seat_record_opencode import rec_dir, event, event_log, seat_record, sv


@pytest.mark.parametrize("initial", [None, b"", b" \t\n", b"\xe2\x82",
                                    event_log(event("step_start"),
                                              event("step_finish", reason="stop")).encode()])
def test_missing_live_review_is_record_pending_not_stale_tail(tmp_path, initial):
    seat = rec_dir(tmp_path, live="")
    path = seat / "record.json"
    record = json.loads(path.read_text())
    record["attempts"] *= 2
    path.write_text(json.dumps(record))
    live = seat / "live.log"
    live.unlink() if initial is None else live.write_bytes(initial)
    with pytest.raises(ValueError) as error:
        seat_record.evidence_from_record(seat)
    assert isinstance(error.value, getattr(seat_record, "RecordPending", ()))


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize("blank", [None, "", " \t\n"])
@pytest.mark.parametrize("malformed", [{"type": "step_finish"},
    *({"type": "step_finish", "part": part} for part in (None, [], "x", 42))])
def test_completed_review_survives_unfinished_trailing_step(tmp_path, finished, blank, malformed):
    reply = "TEST_GAMING: no\nVERDICT: PASS"
    events = [event("step_start"), event("text", text=reply),
              event("step_finish", reason="stop"), event("step_start")]
    if blank is not None:
        events.append(event("text", text=blank))
    if finished:
        events.append(event("step_finish", reason="stop"))
    events.append(malformed)
    live = event_log(*events)
    assert seat_record._review_text_from_log(live) == reply
    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live, tail=""))
    assert text == reply and evidence["test_gaming"] == "no"
    assert sv.parse_verdict(text)[0] is True
    for replacement in (event("text", text="new answer"), event("tool_use"),
                        event("step_finish", reason="length")):
        assert sv.parse_verdict(seat_record._review_text_from_log(
            event_log(*events, event("step_start"), replacement)))[0] is None


@pytest.mark.parametrize("source", ["item_key", "item", "env"])
def test_spawn_normalizes_repeated_project_before_launch(tmp_path, monkeypatch, source):
    from registry_route_util import _service
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLANGRAPH_SPAWN_ITEM", "plangraph:KEY")
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "admit_spawn", lambda *a, **kw: None)
    spawn, card, key = service.runner.spawn, {"estimate_tokens": 100}, "plangraph:KEY"
    if source == "env":
        monkeypatch.setenv("PLANGRAPH_SPAWN_ITEM", "plangraph:" + key)
    else:
        card[source] = "plangraph:" + key
    async def launch(**kwargs):
        assert kwargs["prompt_record"][1]["item_key"] == key
        assert json.loads(service._path(kwargs["prompt_record"][1]["run_id"]).read_text())["item_key"] == key
        job = await spawn(**kwargs)
        job.exit_code, job.stdout = 0, "VERDICT: FIX\n"
        return job
    monkeypatch.setattr(service.runner, "spawn", launch)
    result = asyncio.run(service.spawn(card=card, prompt="Review the change", role="review",
                                      writer_families=("google",), wait_seconds=5))
    assert result["status"] == "completed" and len(service.runner.calls) == 1
    assert result["item_key"] == json.loads(service._path(result["run_id"]).read_text())["item_key"] == key


def test_drive_alias_to_native_record_enforces_real_mode(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    path = seat / 'record.json'
    path.chmod(0o666)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, 'os', SimpleNamespace(name='posix', getuid=lambda: uid))
    monkeypatch.setattr(Path, 'absolute', lambda self: Path('/mnt/c/link') / seat.name / self.name)
    monkeypatch.setattr(Path, 'resolve', lambda self: Path('/native/records') / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert 'not group/world-writable' in capsys.readouterr().err
    assert not sent
