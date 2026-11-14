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
    assert "unfinished" not in seat_record._review_text_from_log.__code__.co_varnames
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
