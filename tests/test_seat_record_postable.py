"""Pending persistence and retention of a completed review, using synthetic logs."""
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
def test_completed_review_survives_unfinished_trailing_step(tmp_path, finished, blank):
    reply = "TEST_GAMING: no\nVERDICT: PASS"
    events = [event("step_start"), event("text", text=reply),
              event("step_finish", reason="stop"), event("step_start")]
    if blank is not None:
        events.append(event("text", text=blank))
    if finished:
        events.append(event("step_finish", reason="stop"))
    live = event_log(*events)
    assert seat_record._review_text_from_log(live) == reply
    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live, tail=""))
    assert text == reply and evidence["test_gaming"] == "no"
    assert sv.parse_verdict(text)[0] is True
    for replacement in (event("text", text="new answer"), event("tool_use"),
                        event("step_finish", reason="length")):
        assert sv.parse_verdict(seat_record._review_text_from_log(
            event_log(*events, event("step_start"), replacement)))[0] is None
