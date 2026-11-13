"""D-3649: a claude stream-json review run leaves its final text, not the raw stream, as the review output.

seat_verdict reads the review text; the raw stream (one JSON event per line) never parses, so every Haiku seat
ended as no_verdict.  The transcripts below have the shape ``claude -p --output-format stream-json --verbose``
prints (hook, init, thinking, assistant and result events), with synthetic ids and text.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner import claude_stream
from plangraph.spawner.runner import SpawnerRunner, spawn_service

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

SID = "00000000-0000-4000-8000-000000000000"


def ev(**fields):
    return json.dumps(fields, separators=(",", ":"))


def assistant(*blocks, stop=None):
    return ev(type="assistant", message={"id": "msg_0", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                                         "content": list(blocks), "stop_reason": stop}, parent_tool_use_id=None, session_id=SID)


def transcript(answer, *, result=True, tool_turn=False, stop="end_turn", is_error=False):
    lines = [
        ev(type="system", subtype="hook_started", hook_event="SessionStart", hook_id="h1", session_id=SID),
        ev(type="system", subtype="hook_response", hook_event="SessionStart", hook_id="h1", output="VERDICT: FIX\nbanner",
           stdout="VERDICT: FIX\nbanner", stderr="", exit_code=0, outcome="success", session_id=SID),
        ev(type="system", subtype="init", cwd=".", session_id=SID, tools=[], mcp_servers=[], model="claude-haiku-5-5",
           permissionMode="plan", apiKeySource="none"),
        ev(type="system", subtype="thinking_tokens", estimated_tokens=40, session_id=SID),
        assistant({"type": "thinking", "thinking": "weigh the diff", "signature": "sig"}),
    ]
    if tool_turn:
        lines += [
            assistant({"type": "tool_use", "id": "toolu_0", "name": "Read", "input": {"file_path": "a.py"}}),
            ev(type="user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_0", "content": "x = 1"}]},
               session_id=SID),
            assistant({"type": "text", "text": "draft\nVERDICT: FIX"}),
        ]
    lines += [assistant({"type": "text", "text": answer}, stop=stop), ev(type="rate_limit_event", rate_limit_info={}, session_id=SID)]
    if result:
        lines.append(ev(type="result", subtype="error_during_execution" if is_error else "success", is_error=is_error, num_turns=1,
                        result=answer, stop_reason=stop, session_id=SID, total_cost_usd=0.0))
    return lines


ANSWER = "Finding: none.\nTEST-GAMING: no\nVERDICT: PASS"


def test_the_raw_stream_has_no_parseable_verdict():
    assert sv.parse_verdict("\n".join(transcript(ANSWER))) == (None, -1)


@pytest.mark.parametrize("kwargs,want", [
    ({}, (ANSWER, "end_turn")),
    ({"tool_turn": True}, (ANSWER, "end_turn")),
    ({"result": False}, (ANSWER, None)),
    ({"stop": "max_tokens"}, (ANSWER, "max_tokens")),
    ({"is_error": True}, None),
])
def test_final_text_is_the_result_event_else_the_last_assistant_text(kwargs, want):
    assert claude_stream.final_text("\n".join(transcript(ANSWER, **kwargs))) == want


def test_the_result_event_wins_over_the_last_assistant_text():
    lines = transcript("partial answer", result=False) + [ev(type="result", subtype="success", is_error=False, result=ANSWER, stop_reason="end_turn")]
    assert claude_stream.final_text("\n".join(lines)) == (ANSWER, "end_turn")
    job = SimpleNamespace(stdout="\n".join(lines), events=[], _live_log=None)
    assert sv.parse_verdict(claude_stream.final_text(job.stdout)[0]) == (True, 2)


def test_final_text_ignores_noise_and_needs_an_answer():
    assert claude_stream.final_text("") is None and claude_stream.final_text(None) is None
    assert claude_stream.final_text("not json\n[1]\n{}\n" + assistant({"type": "thinking", "thinking": "x"})) is None
    assert claude_stream.final_text(ev(type="result", result="  ", is_error=False) + "\n" + assistant({"type": "text", "text": "late"})) == ("late", None)
    two = assistant({"type": "text", "text": "one"}, {"type": "tool_use", "id": "t", "name": "Read", "input": {}}, {"type": "text", "text": "two"})
    assert claude_stream.final_text(two) == ("one\ntwo", None)


def test_real_shaped_transcript_ending_in_verdict_pass_parses_as_pass(tmp_path):
    stream = "\n".join(transcript(ANSWER, tool_turn=True)) + "\n"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = SimpleNamespace(stdout=stream, events=[], _live_log=str(run_dir / "live.log"))
    assert claude_stream.render_review_output(job, stream, lambda text: text) is True
    assert job.stdout == ANSWER and sv.parse_verdict(job.stdout) == (True, 2)
    assert (run_dir / claude_stream.RAW_NAME).read_text() == stream   # the raw stream is kept whole, elsewhere
    assert job.events == [{"event": "claude_stream_rendered", "raw": "claude-stream.jsonl", "stop_reason": "end_turn"}]


def test_a_fix_verdict_and_an_unusable_stream_keep_failing_closed(tmp_path):
    (tmp_path / "run").mkdir()
    for lines, rendered in ((transcript("VERDICT: FIX\nwhy"), True), (transcript("no verdict here"), True), (transcript(ANSWER, is_error=True), False)):
        stream = "\n".join(lines)
        job = SimpleNamespace(stdout=stream, events=[], _live_log=str(tmp_path / "run" / "live.log"))
        assert claude_stream.render_review_output(job, stream, lambda text: text) is rendered
        if rendered:
            ok, _ = sv.parse_verdict(job.stdout)
            assert ok is (False if "FIX" in job.stdout else None)
        else:
            assert job.stdout == stream   # nothing usable: the raw stream stays the output


def test_nothing_changes_without_a_run_dir_or_when_the_raw_stream_cannot_be_kept(tmp_path):
    stream = "\n".join(transcript(ANSWER))
    bare = SimpleNamespace(stdout=stream, events=[], _live_log=None)
    assert claude_stream.render_review_output(bare, stream, lambda text: text) is False and bare.stdout == stream and bare.events == []
    (tmp_path / "blocked").write_text("a file where the run dir should be")
    blocked = SimpleNamespace(stdout=stream, events=[], _live_log=str(tmp_path / "blocked" / "live.log"))
    assert claude_stream.render_review_output(blocked, stream, lambda text: text) is False
    assert blocked.stdout == stream and blocked.events == [{"event": "claude_stream_not_kept"}]


def fake_claude(tmp_path, lines, exit_code=0):
    """A process factory whose child prints ``lines`` as claude would, then exits."""
    script = "import sys\nfor line in %r:\n    print(line, flush=True)\nsys.exit(%d)\n" % (lines, exit_code)

    async def factory(*args, **kwargs):
        return await asyncio.create_subprocess_exec(sys.executable, "-c", script, **kwargs)
    return factory


REVIEW_DIFF = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'

def run_review(tmp_path, monkeypatch, lines, exit_code=0):
    """Drive the production dispatch path for the claude Haiku leg and return (record, run dir)."""
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "claude").write_text("#!/bin/sh\nexit 0\n")
    (binary / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PLANGRAPH_GATEWAY_URL", "http://127.0.0.1:9/v1")
    monkeypatch.delenv("PLANGRAPH_SPAWN_RUN_TIMEOUT", raising=False)
    leg = next(l for l in DEFAULT_LEGS if l.id == "claude-haiku-5.5")

    async def run():
        runner = SpawnerRunner(process_factory=fake_claude(tmp_path, lines, exit_code), default_cwd=str(tmp_path))
        service = spawn_service(runner, tmp_path / "state")
        monkeypatch.setattr(service, "_route", lambda *a, **k: SpawnPlan((leg,), (), "test", (), ()))
        record = await service.spawn(card={"estimate_tokens": 1000}, prompt="review this\n" + REVIEW_DIFF, role="review",
                                     writer_families=("mimo",), wait_seconds=None)
        return record, service._path(record["run_id"]).parent
    return asyncio.run(run())


def test_dispatch_record_carries_the_review_text_and_the_raw_stream_sits_beside_it(tmp_path, monkeypatch):
    lines = transcript(ANSWER, tool_turn=True)
    record, run_dir = run_review(tmp_path, monkeypatch, lines)
    assert record["status"] == "completed", record["status"]
    attempt = record["attempts"][-1]
    assert attempt["stdout_tail"] == ANSWER and record["stdout_tail"] == ANSWER
    assert sv.parse_verdict(attempt["stdout_tail"]) == (True, 2)
    raw = (run_dir / "claude-stream.jsonl").read_text()
    assert raw.splitlines() == lines and "weigh the diff" in raw
    assert [e for e in record["events"] if e["event"] == "claude_stream_rendered"] == [
        {"event": "claude_stream_rendered", "raw": "claude-stream.jsonl", "stop_reason": "end_turn"}]
    assert "claude-stream.jsonl" not in (run_dir / "live.log").read_text()   # live.log keeps its activity feed untouched


def test_the_rendered_review_text_is_what_seat_verdict_reads(tmp_path, monkeypatch, capsys):
    record, _ = run_review(tmp_path, monkeypatch, transcript(ANSWER))
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / "review.txt").write_text(record["attempts"][-1]["stdout_tail"])
    (seat / "evidence.json").write_text(json.dumps({
        "served_model": "claude-haiku-5-5", "finish_reason": "end_turn", "test_gaming": "no",
        "correlation_id": "c1", "prompt_sha256": "ab" * 32}))
    tokens = tmp_path / "tok"
    tokens.mkdir()
    assert sv.main(["--key", "demo:A", "--seat-dir", str(seat), "--writer-family", "xiaomi", "--token-dir", str(tokens), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["body"]["ok"] is True


def test_a_failed_claude_run_keeps_its_raw_stdout_for_diagnosis(tmp_path, monkeypatch):
    lines = transcript(ANSWER, is_error=True)
    record, run_dir = run_review(tmp_path, monkeypatch, lines, exit_code=1)
    assert record["status"] == "failed"
    assert record["attempts"][-1]["stdout_tail"].splitlines() == lines
    assert not (run_dir / "claude-stream.jsonl").exists()
