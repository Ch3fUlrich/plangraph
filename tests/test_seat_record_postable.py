"""Portable F6 checks and completed OpenCode answer retention, without live data."""
from types import SimpleNamespace
from pathlib import Path

import pytest

from test_seat_record_opencode import rec_dir, event, event_log, seat_record, sv


@pytest.mark.parametrize("has_getuid", [False, True])
def test_windows_record_skips_posix_owner_and_mode(tmp_path, monkeypatch, has_getuid):
    seat = rec_dir(tmp_path, live="VERDICT: FIX\n")
    (seat / "record.json").chmod(0o777)
    def forbidden():
        pytest.fail("Windows must not call getuid")
    platform = SimpleNamespace(name="nt", **({"getuid": forbidden} if has_getuid else {}))
    monkeypatch.setattr(seat_record, "os", platform)
    assert seat_record.evidence_from_record(seat)[1] == "VERDICT: FIX\n"


@pytest.mark.parametrize("root,accepted", [
    ("/mnt/c", True), ("/mnt/Z", True), ("/mnt/data", False), ("/native", False)])
def test_drive_mount_mode_unknown_but_native_world_writable_refused(
        tmp_path, monkeypatch, root, accepted):
    seat = rec_dir(tmp_path, live="VERDICT: FIX\n")
    path = seat / "record.json"
    path.chmod(0o777)
    monkeypatch.setattr(Path, "resolve", lambda self: Path(root) / seat.name / self.name)
    if accepted:
        assert seat_record.evidence_from_record(seat)[1] == "VERDICT: FIX\n"
    else:
        with pytest.raises(ValueError, match="not group/world-writable"):
            seat_record.evidence_from_record(seat)


def test_drive_mount_still_requires_linux_owner(tmp_path, monkeypatch):
    seat = rec_dir(tmp_path, live="VERDICT: FIX\n")
    uid = (seat / "record.json").stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid + 1))
    monkeypatch.setattr(Path, "resolve", lambda self: Path("/mnt/c") / seat.name / self.name)
    with pytest.raises(ValueError, match="owned by the current uid"):
        seat_record.evidence_from_record(seat)


@pytest.mark.parametrize("blank", [None, "", " \t\n"])
def test_stop_completed_review_survives_empty_stop_step(tmp_path, blank):
    # Structure only: stdout is decoded text; live.log retains lifecycle JSON.
    reply = "review complete\nTEST_GAMING: no\nVERDICT: PASS"
    events = [event("step_start"), event("text", 2, text=reply),
              event("step_finish", 3, reason="stop"), event("step_start", 4)]
    if blank is not None:
        events.append(event("text", 5, text=blank))
    live = event_log(*events, event("step_finish", 6, reason="stop"))
    assert live.strip() and reply.strip()
    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live, tail=reply))
    assert text == reply
    assert evidence["test_gaming"] == "no"
    assert sv.parse_verdict(text)[0] is True


@pytest.mark.parametrize("reason", [None, "length", "tool-calls"])
def test_empty_nonstop_step_cannot_revive_completed_review(tmp_path, reason):
    live = event_log(event("step_start"), event("text", 2, text="VERDICT: PASS"),
                     event("step_finish", 3, reason="stop"), event("step_start", 4),
                     event("step_finish", 5, reason=reason))
    with pytest.raises(ValueError, match="no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


@pytest.mark.parametrize("empty_stop", [False, True])
@pytest.mark.parametrize("blank", [None, "", " \t\n"])
def test_completed_review_survives_unfinished_trailing_step(tmp_path, empty_stop, blank):
    reply = "TEST_GAMING: no\nVERDICT: PASS"
    events = [event("step_start"), event("text", text=reply),
              event("step_finish", reason="stop"), event("step_start")]
    if empty_stop:
        events += [event("step_finish", reason="stop"), event("step_start")]
    if blank is not None:
        events.append(event("text", text=blank))
    evidence, text = seat_record.evidence_from_record(
        rec_dir(tmp_path, live=event_log(*events), tail=reply))
    assert text == reply and evidence["test_gaming"] == "no"


@pytest.mark.parametrize("replacement", [event("text", text="new unfinished answer"),
                                        event("tool_use")])
def test_unfinished_step_activity_invalidates_retained_answer(replacement):
    live = event_log(event("step_start"), event("text", text="VERDICT: PASS"),
                     event("step_finish", reason="stop"), event("step_start"), replacement)
    assert sv.parse_verdict(seat_record._review_text_from_log(live))[0] is None
