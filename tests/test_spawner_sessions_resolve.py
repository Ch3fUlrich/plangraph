"""tests/test_spawner_sessions_resolve.py — SESSION-SPAWN-MCP argv/id/resolve/list tests."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from sessions_support import UUID1, UUID2, UUID3, SHORT, MODEL, EFFORT, MODE, PROMPT, FakeRun, write_config, make, go
from sessions_support import site  # noqa: F401


def test_argv_linux_is_list_no_shell(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID1
    argv, kw = run.calls[0]
    assert argv == ["claude", "--bg", "--strict-mcp-config", "--mcp-config", str(site.mcp.resolve()),
                    "--remote-control", "alpha", "-n", "alpha", "--model", MODEL, "--effort", EFFORT,
                    "--permission-mode", MODE, PROMPT]
    assert all(isinstance(a, str) for a in argv)
    assert kw.get("shell") in (None, False)
    assert kw["cwd"] == str(site.proj.resolve())
    assert kw["env"]["FLEET_SESSION_NAME"] == "alpha"
    assert kw["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "330000"
    # style_text is appended via --append-system-prompt when configured
    cfg2 = site.tmp_path / "sessions2.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": "be terse"}))
    s.stop(name="alpha")  # a live record may only be replaced via relaunch
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, config_path=cfg2)
    go(site, s2)
    # the old-id gate polls agents first (round 4): pick the launch argv
    argv2 = run2.launch_calls()[0]
    assert argv2[-3:] == ["--append-system-prompt", "be terse", PROMPT]


def test_id_from_full_uuid(site):
    run = FakeRun(stdout="backgrounded · 88888888 · alpha\nsession " + UUID1 + " ready")
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID1
    assert len(run.calls) == 1  # full UUID wins; no claude agents lookup


def test_id_from_short_backgrounded(site):
    run = FakeRun(stdout="backgrounded · " + SHORT + " · alpha", agents_rows=[
        {"id": SHORT, "sessionId": UUID1, "name": "alpha", "kind": "background", "startedAt": 1_000_000}])
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID1
    assert any(a[:3] == ["claude", "agents", "--json"] for a, _ in run.calls)


def test_id_from_name_fallback_ignores_stale(site):
    stale = {"id": "s", "sessionId": UUID1, "name": "alpha", "kind": "background",
             "startedAt": 1_000_000 - 60_000}
    wrong_kind = {"id": "k", "sessionId": UUID3, "name": "alpha", "kind": "foreground",
                  "startedAt": 1_000_000}
    fresh = {"id": UUID2[:8], "sessionId": UUID2, "name": "alpha", "kind": "background",
             "startedAt": 1_000_000}
    run = FakeRun(stdout="done\n", agents_rows=[stale, wrong_kind, fresh])
    s = make(site, run)
    res = go(site, s)
    assert res["session_id"] == UUID2  # newest fresh background row, never the stale one
    s.stop(name="alpha")  # a live record may only be replaced via relaunch
    # stale-only: nothing resolves and the record keeps session_id null
    run2 = FakeRun(stdout="done\n", agents_rows=[stale])
    s2 = make(site, run2, resolve_budget=0.0)
    res2 = go(site, s2)
    assert res2["session_id"] is None and "warning" in res2
    rec = json.loads((site.state / "sessions" / "alpha.json").read_text())
    assert rec["session_id"] is None


def test_list_reports_context_tokens(site):
    run = FakeRun(stdout="session " + UUID1, agents_rows=[
        {"id": UUID1[:8], "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "running", "startedAt": 1_000_000}])
    s = make(site, run)
    go(site, s)
    # Claude maps every non-alphanumeric cwd char to "-"; spelled out here on
    # purpose so the test never reuses the implementation's regex.
    tdir = site.home / ".claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(site.proj.resolve()))
    tdir.mkdir(parents=True)
    usage = lambda n: json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": n, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}})
    (tdir / f"{UUID1}.jsonl").write_text(
        '{"type":"user","message":{}}\n' + usage(100) + "\n" + usage(350) + "\n")
    out = s.list()
    row = out["sessions"][0]
    assert row["name"] == "alpha" and row["id"] == UUID1
    assert row["cwd"] == str(site.proj.resolve())
    assert row["context_tokens"] == 350 + 10 + 5
    assert row["context_pct"] == pytest.approx(365 / 300000)
    assert row["state"] == "running" and row["ready"] is False
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf))
    assert s.list()["sessions"][0]["ready"] is True


def test_context_tokens_reads_only_last_256kb(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    tdir = site.home / ".claude" / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(site.proj.resolve()))
    tdir.mkdir(parents=True)
    usage = json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 900, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
    filler = json.dumps({"type": "user", "message": {"content": "x" * 1024}}) + "\n"
    (tdir / f"{UUID1}.jsonl").write_text(usage + "\n" + filler * 300)  # > 256 KB past the usage
    row = s.list()["sessions"][0]
    assert row["context_tokens"] == 0  # the 900-token line is outside the tail window


def test_transcript_path_maps_every_non_alnum(site):
    from plangraph.spawner.sessions import transcript_path
    p = transcript_path("C:\\Users\\x\\my_proj", UUID1)
    assert p.parent.name == "C--Users-x-my-proj"
    assert p.name == UUID1 + ".jsonl"
    p2 = transcript_path("/srv/u/my_proj", UUID1)
    assert p2.parent.name == "-srv-u-my-proj"


def test_resolve_short_id_miss_falls_back_and_excludes(site):
    row_new = {"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background", "startedAt": 1_000_000}
    # a short-id miss falls back to name + startedAt
    run = FakeRun(stdout="backgrounded · 00000000 · alpha", agents_rows=[row_new])
    res = go(site, make(site, run))
    assert res["session_id"] == UUID2
    # relaunch excludes the previous session id from every lookup
    run2 = FakeRun(stdout="session " + UUID1)
    s2 = make(site, run2, resolve_budget=0.0)
    go(site, s2, name="beta", prompt="do beta")
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s2.mark_ready("beta", str(hf))
    run2.stdout = "done\n"
    run2.agents_rows = [{"id": UUID1[:8], "sessionId": UUID1, "name": "beta", "kind": "background",
                         "startedAt": 2_000_000}]
    res2 = s2.relaunch("beta")  # the marker's handoff carries the relaunch
    assert res2["session_id"] is None and "warning" in res2  # only the excluded row matched


def test_docs_context_pct_fraction(site):
    text = (Path(__file__).resolve().parents[1] / "docs" / "spawner-sessions.md").read_text(encoding="utf-8")
    section = text.split("# Claude Code sessions", 1)[1]
    assert "context_pct" in section and "fraction" in section and "0..1" in section


def test_launched_ms_taken_before_start(site):
    clock = {"n": 0}

    def now():
        clock["n"] += 1
        return 1000.0 if clock["n"] == 1 else 3000.0

    row = {"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background",
           "startedAt": 1_001_000}  # just after the launch, far before the late clock
    run = FakeRun(stdout="done\n", agents_rows=[row])
    s = make(site, run, now=now)
    res = go(site, s)
    assert res["session_id"] == UUID2


def test_resolve_polls_every_half_second_until_budget(site, monkeypatch):
    class LateRows(FakeRun):
        def __init__(self, row_name="alpha"):
            super().__init__(stdout="done\n", agents_rows=[])
            self.agents_calls = 0
            self.row_name = row_name

        def __call__(self, argv, **kw):
            if self._is_agents(argv):
                self.agents_calls += 1
                if self.agents_calls >= 3:
                    self.agents_rows = [{"id": "f", "sessionId": UUID2, "name": self.row_name,
                                         "kind": "background", "startedAt": 1_000_000}]
            return super().__call__(argv, **kw)

    sleeps = []
    run = LateRows()
    s = make(site, run, sleep=sleeps.append)
    res = go(site, s)
    assert res["session_id"] == UUID2  # a row appearing late is still found
    assert run.agents_calls == 3 and sleeps[:2] == [0.5, 0.5]


def test_excluded_id_rejected_for_full_uuid_short_and_fallback(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)  # alpha at UUID1
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    run.agents_rows = [{"id": "f", "sessionId": UUID2, "name": "alpha", "kind": "background",
                        "startedAt": 1_000_000}]
    res = s.relaunch("alpha", handoff=str(hf))
    assert res["session_id"] == UUID2  # the old full UUID in stdout is not the new id
    # a short id row carrying the excluded sessionId is rejected too: the
    # stoppable row goes first (its own id is the short job id the stop needs,
    # D-3343) and keeps the SHORT row alive behind it - that row matches the
    # claimed short id, name and kind, yet carries the replaced sessionId and
    # is never the new id
    run.agents_rows = [{"id": UUID2[:8], "sessionId": UUID2, "name": "alpha", "kind": "background",
                        "startedAt": 1_000_000},
                       {"id": SHORT, "sessionId": UUID2, "name": "alpha", "kind": "background",
                        "startedAt": 1_000_000},
                       {"id": "g", "sessionId": UUID3, "name": "alpha", "kind": "background",
                        "startedAt": 1_000_000}]
    run.stdout = "backgrounded · " + SHORT + " · alpha"
    res2 = s.relaunch("alpha", handoff=str(hf))
    assert res2["session_id"] == UUID3


def test_short_id_matches_row_id_or_session_id_prefix(site):
    row = {"id": "other", "sessionId": SHORT + "-1234-1234-1234-123456789abc",
           "name": "alpha", "kind": "background", "startedAt": 0}
    run = FakeRun(stdout="backgrounded · " + SHORT + " · alpha", agents_rows=[row])
    res = go(site, make(site, run))
    assert res["session_id"] == row["sessionId"]  # sessionId prefix matches the short id


def test_short_id_match_requires_launched_name_and_background_kind(site):
    # a short id may claim only the launched name's own background row: a
    # wrong name, a wrong kind and the excluded id are all rejected
    run = FakeRun(stdout="backgrounded · " + SHORT + " · alpha", agents_rows=[
        {"id": SHORT, "sessionId": UUID2, "name": "beta", "kind": "background", "startedAt": 0}])
    res = go(site, make(site, run))
    assert res["session_id"] is None  # the row name is not the launched name
    run2 = FakeRun(stdout="backgrounded · " + SHORT + " · beta", agents_rows=[
        {"id": SHORT, "sessionId": UUID2, "name": "beta", "kind": "foreground", "startedAt": 0}])
    res2 = go(site, make(site, run2), name="beta", prompt="do beta")
    assert res2["session_id"] is None  # only kind background may be claimed
    run3 = FakeRun(stdout="backgrounded · " + SHORT + " · gamma", agents_rows=[
        {"id": SHORT, "sessionId": UUID2, "name": "gamma", "kind": "background", "startedAt": 0}])
    res3 = go(site, make(site, run3), name="gamma", prompt="do gamma")
    assert res3["session_id"] == UUID2  # name + kind match: the short id resolves
    run4 = FakeRun(stdout="session " + UUID1)
    s4 = make(site, run4)
    go(site, s4, name="delta", prompt="do delta")  # delta at UUID1
    run4.agents_rows = [
        {"id": UUID1[:8], "sessionId": UUID1, "name": "delta", "kind": "background", "startedAt": 0},
        {"id": SHORT, "sessionId": UUID1, "name": "delta", "kind": "background", "startedAt": 0},
        {"id": "g", "sessionId": UUID3, "name": "delta", "kind": "background", "startedAt": 2_000_000}]
    run4.stdout = "backgrounded · " + SHORT + " · delta"
    hf4 = site.tmp_path / "handoff4.md"
    hf4.write_text("carry on")
    res4 = s4.relaunch("delta", handoff=str(hf4))
    assert res4["session_id"] == UUID3  # the excluded id stays rejected despite name + kind


def test_list_state_prefers_agents_row(site):
    run = FakeRun(stdout="session " + UUID1, agents_rows=[
        {"id": "r", "sessionId": UUID1, "name": "alpha", "kind": "background",
         "state": "working", "startedAt": 1_000_000}])
    s = make(site, run)
    go(site, s)
    listed = [r for r in s.list()["sessions"] if r["name"] == "alpha"][0]
    assert listed["state"] == "working"  # the live row wins over the record
