"""Synthetic OpenCode event logs must yield only the final step's review text."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_record  # noqa: E402
import seat_verdict as sv  # noqa: E402

HEAD40 = "a" * 40
FREE_LEG = {"id": "leg-1", "tier": "subscription", "harness": "opencode",
            "model": "opencode/mimo-v2.6-free", "family": "mimo"}
FINDING = "MED a.py:1 - x - y - z"


def rec_dir(tmp_path, *, live, tail="VERDICT: FIX\n"):
    """Copy the compact synthetic record helper from test_seat_record_fixes."""
    name = "run-" + "1" * 32
    d = tmp_path / name
    d.mkdir(mode=0o700)
    record = {"run_id": name, "role": "review", "status": "completed",
              "exit_code": 0, "served_model": "unwitnessed", "served_family": "unwitnessed",
              "writer_family": FREE_LEG["family"], "chosen_leg": FREE_LEG,
              "finish_reason": "stop", "stdout_tail": tail,
              "completion": {"event": "DONE", "rc": 0, "head_sha": HEAD40},
              "attempts": [{"leg": FREE_LEG, "status": "completed",
                            "served_model": "unwitnessed", "served_family": "unwitnessed",
                            "stdout_tail": tail}]}
    (d / "record.json").write_text(json.dumps(record), encoding="utf-8")
    (d / "record.json").chmod(0o600)
    (d / "live.log").write_text(live, encoding="utf-8")
    return d


def event(kind, timestamp=1, **part):
    return {"type": kind, "timestamp": timestamp, "sessionID": "s",
            "part": {"id": f"p{timestamp}", "type": kind.replace("_", "-"), **part}}


def event_log(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.fixture
def extreme_json_lines():
    # Python 3.12's C decoder accepts 5,000 levels; exceed its own recursion cap.
    return ["1" * 5000, "[" * 100000 + "0" + "]" * 100000]


@pytest.fixture(params=[0, 1], ids=["integer", "nested"])
def extreme_json_line(request, extreme_json_lines):
    return extreme_json_lines[request.param]


def test_json_final_verdict_parses_and_evidence_is_preserved(tmp_path):
    reply = f"{FINDING}\ntests not run (no tools)\nVERDICT: ACCEPT"
    live = event_log(
        {"plangraph_oom": {"event": "oom_score_adj", "value": 500}},
        event("step_start"), event("text", 2, text=reply), event("step_finish", 3))
    seat = rec_dir(tmp_path, live=live)

    evidence, text = seat_record.evidence_from_record(seat)

    assert text == reply
    assert sv.parse_verdict(text) == (True, 2)
    assert evidence == {"head_sha": HEAD40, "source": "record.json", "run_id": seat.name,
                        "finish_reason": "stop", "served_model": FREE_LEG["model"],
                        "witness_kind": "record-chosen-leg",
                        "request_side_model": FREE_LEG["model"]}


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_json_raw_unicode_separators_preserve_final_text_and_verdict(tmp_path, newline):
    reply = f"{FINDING}\u2028detail\u0085more\u2029context\nVERDICT: ACCEPT"
    events = [event("step_start"), event("text", 2, text=reply), event("step_finish", 3)]
    # Match JSON.stringify: these separators are raw characters inside the JSON string.
    live = newline.join(json.dumps(e, ensure_ascii=False) for e in events) + newline

    assert all(char in live for char in ("\u2028", "\u0085", "\u2029"))
    assert seat_record._review_text_from_log(live) == reply

    _, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == reply
    assert sv.parse_verdict(text) == (True, 4)


@pytest.mark.parametrize("split_parts", [False, True])
def test_json_finding_after_verdict_stays_strict(tmp_path, split_parts):
    parts = [event("text", 2, text="VERDICT: ACCEPT"),
             event("text", 3, text=FINDING)] if split_parts else [
                 event("text", 2, text=f"VERDICT: ACCEPT\n{FINDING}")]
    live = event_log(event("step_start"), *parts, event("step_finish", 4))

    with pytest.raises(ValueError, match="verdict is not the last line"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


@pytest.mark.parametrize("empty_part", [None, "", " \n\t"])
def test_json_without_text_after_last_step_refused_without_tail_fallback(tmp_path, empty_part):
    events = [event("step_start"), event("text", 2, text="VERDICT: ACCEPT"),
              event("step_finish", 3), event("step_start", 4),
              event("tool_use", 5, text="VERDICT: ACCEPT")]
    if empty_part is not None:
        events.append(event("text", 6, text=empty_part))
    events.append(event("step_finish", 7))
    live = event_log(*events)

    with pytest.raises(ValueError, match="record has no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


@pytest.mark.parametrize("blank_text", [None, "", " \n\t"])
def test_step_start_only_or_blank_text_has_no_review_text(tmp_path, blank_text):
    events = [event("step_start")]
    if blank_text is not None:
        events.append(event("text", 2, text=blank_text))
    live = event_log(*events)

    assert not seat_record._review_text_from_log(live).strip()
    with pytest.raises(ValueError, match="record has no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


def test_json_only_tool_and_step_events_have_no_review_text(tmp_path):
    live = event_log(event("step_start"),
                     event("tool_use", 2, text="VERDICT: ACCEPT"), event("step_finish", 3))

    with pytest.raises(ValueError, match="record has no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


@pytest.mark.parametrize("live", [
    "\n  review text  \nVERDICT: ACCEPT\n\n",
    '{"plangraph_oom": {"event": "oom_score_adj", "value": 500}}\nVERDICT: FIX\n',
    'not JSON\n["type", "text"]\n"text"\nnull\n42\nVERDICT: FIX\n',
])
def test_plain_text_log_without_typed_json_objects_is_unchanged(tmp_path, live):
    _, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == live


@pytest.mark.parametrize("quoted_line", [
    '> {"type": "text", "part": {"text": "x"}}',
    json.dumps('{"type": "text", "part": {"text": "x"}}'),
])
def test_plain_text_log_with_quoted_json_is_unchanged(tmp_path, quoted_line):
    live = f"{FINDING}\n{quoted_line}\nVERDICT: ACCEPT\n"

    _, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == live
    assert sv.parse_verdict(text) == (True, 2)


@pytest.mark.parametrize("non_event", [
    {"type": "diagnostic", "part": {"text": "x"}},
    {"type": None, "part": {}}, {"type": ["text"], "part": {}},
    {"type": {"name": "text"}, "part": {}},
    *({"type": kind} for kind in ("step_start", "step_finish", "text", "tool_use")),
    *({"type": kind, "part": part}
      for kind in ("step_start", "step_finish", "text", "tool_use")
      for part in (None, [], "x", 42)),
])
def test_plain_text_log_with_non_event_typed_object_is_unchanged(tmp_path, non_event):
    live = f"{FINDING}\n{json.dumps(non_event)}\nVERDICT: ACCEPT\n"

    _, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == live
    assert sv.parse_verdict(text) == (True, 2)


@pytest.mark.parametrize("kind", ["step_start", "step_finish"])
def test_minimal_lifecycle_event_marks_event_log(tmp_path, kind):
    line = json.dumps({"type": kind, "part": {"type": kind.replace("_", "-")}},
                      separators=(",", ":"))
    live = f"{FINDING}\n{line}\nVERDICT: ACCEPT\n"

    assert seat_record._review_text_from_log(live) == ""
    with pytest.raises(ValueError, match="record has no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


@pytest.mark.parametrize("kind", ["text", "tool_use"])
def test_plain_text_log_with_one_non_lifecycle_event_is_unchanged(tmp_path, kind):
    line = json.dumps({"type": kind, "part": {"text": "x"}}, separators=(",", ":"))
    live = f"{FINDING}\n{line}\nVERDICT: ACCEPT\nTEST_GAMING: yes\n"

    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert seat_record._review_text_from_log(live) == live
    assert text == live
    assert evidence["test_gaming"] == "yes"


def test_plain_text_log_with_extreme_json_is_unchanged(tmp_path, extreme_json_line):
    live = f"{FINDING}\n{extreme_json_line}\nVERDICT: FIX\nTEST_GAMING: yes\n"

    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert seat_record._review_text_from_log(live) == live
    assert text == live
    assert evidence["test_gaming"] == "yes"


def test_plain_text_log_with_both_extreme_json_lines_is_unchanged(tmp_path, extreme_json_lines):
    live = "\n".join([FINDING, *extreme_json_lines, "VERDICT: FIX", "TEST_GAMING: yes", ""])

    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == live
    assert evidence["test_gaming"] == "yes"


def test_record_with_extreme_json_is_refused(tmp_path, extreme_json_line):
    seat = rec_dir(tmp_path, live="VERDICT: FIX\n")
    (seat / "record.json").write_text(extreme_json_line, encoding="utf-8")

    with pytest.raises(ValueError, match="record.json is not valid JSON"):
        seat_record.evidence_from_record(seat)


def test_record_index_skips_extreme_json_without_losing_valid_seat(tmp_path, extreme_json_line):
    seat = rec_dir(tmp_path, live="VERDICT: FIX\n")
    broken = tmp_path / ("run-" + "2" * 32)
    broken.mkdir()
    (broken / "record.json").write_text(extreme_json_line, encoding="utf-8")

    assert list(seat_record.record_index(tmp_path)) == [seat / "record.json"]


def test_json_only_text_after_last_step_start_counts_in_file_order(tmp_path):
    live = event_log(
        event("text", 1, text="VERDICT: FIX"),
        event("step_start", 2),
        event("text", 3, text="VERDICT: FIX\nTEST_GAMING: yes"),
        event("tool_use", 4, text='{"type":"text","part":{"text":"VERDICT: FIX"}}'),
        event("step_finish", 5), event("step_start", 6),
        event("text", 10, text=FINDING),
        event("text", 8, text="tests not run (no tools)\nTEST_GAMING: no"),
        event("text", 7, text="VERDICT: ACCEPT"), event("step_finish", 11))

    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == f"{FINDING}\ntests not run (no tools)\nTEST_GAMING: no\nVERDICT: ACCEPT"
    assert sv.parse_verdict(text) == (True, 3)
    assert evidence["test_gaming"] == "no"


def test_json_ignores_raw_lines_untyped_objects_and_non_text_events(tmp_path):
    live = event_log(event("step_start"), event("text", 2, text="VERDICT: ACCEPT"))
    live += "unstructured diagnostic\nVERDICT: FIX\n{invalid JSON\n"
    live += event_log(
        {"plangraph_oom": {"event": "oom_score_adj", "value": 500},
         "part": {"text": "VERDICT: FIX"}},
        {"part": {"text": "VERDICT: FIX"}},
        [event("text", text="VERDICT: FIX")],
        event("tool_use", 3, text="VERDICT: FIX"), event("step_finish", 4))

    _, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == "VERDICT: ACCEPT"


def test_json_raw_or_untyped_text_cannot_supply_missing_review(tmp_path):
    live = event_log(event("step_start"), event("tool_use", 2))
    live += "VERDICT: ACCEPT\n" + event_log({"part": {"text": "VERDICT: ACCEPT"}})

    with pytest.raises(ValueError, match="record has no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))


@pytest.mark.parametrize("with_plain_lines", [False, True])
def test_plain_log_with_text_only_json_lines_is_unchanged(tmp_path, with_plain_lines):
    live = event_log(event("text", text=FINDING),
                     event("text", 2, text="VERDICT: ACCEPT"))
    if with_plain_lines:
        live = f"{FINDING}\n{live}VERDICT: ACCEPT\nTEST_GAMING: yes\n"

    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert seat_record._review_text_from_log(live) == live
    assert text == live
    assert evidence.get("test_gaming") == ("yes" if with_plain_lines else None)


def test_step_finish_without_step_start_selects_event_text(tmp_path):
    reply = f"{FINDING}\nVERDICT: ACCEPT\nTEST_GAMING: no"
    live = event_log(event("text", text=reply), event("step_finish", 2))

    evidence, text = seat_record.evidence_from_record(rec_dir(tmp_path, live=live))

    assert text == reply
    assert evidence["test_gaming"] == "no"


@pytest.mark.parametrize("bad_event", [
    {"type": "text"}, {"type": "text", "part": None},
    {"type": "text", "part": []}, {"type": "text", "part": "VERDICT: ACCEPT"},
    {"type": "text", "part": {}}, {"type": "text", "part": {"text": None}},
    {"type": "text", "part": {"text": 42}},
])
def test_json_without_a_string_text_part_refused(tmp_path, bad_event):
    live = event_log(event("step_start"), bad_event, event("step_finish", 3))

    with pytest.raises(ValueError, match="record has no review text"):
        seat_record.evidence_from_record(rec_dir(tmp_path, live=live))
