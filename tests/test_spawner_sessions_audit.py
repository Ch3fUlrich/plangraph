"""tests/test_spawner_sessions_audit.py — SESSION-SPAWN-MCP audit-line tests."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import pytest
from plangraph.routing_errors import ParameterRefused
from plangraph.spawner.sessions import Sessions
from sessions_support import UUID1, PROMPT, FakeRun, write_config, make, go
from sessions_support import site  # noqa: F401


def test_launch_writes_record_and_audit(site):
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    res = go(site, s)
    assert res["ok"] is True
    rec_path = site.state / "sessions" / "alpha.json"
    rec = json.loads(rec_path.read_text())
    for key in ("name", "cwd", "model", "effort", "mcp", "prompt_file", "handoff", "parent",
                "children", "ts", "session_id", "job_id", "permission_mode", "platform",
                "stopped_ts"):
        assert key in rec, key
    assert rec["name"] == "alpha" and rec["session_id"] == UUID1
    assert rec["job_id"] == UUID1[:8] and rec["stopped_ts"] is None
    prompt_file = site.state / "sessions" / "alpha.prompt.txt"
    assert rec["prompt_file"] == str(prompt_file)
    assert prompt_file.read_text() == PROMPT
    assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600
    audit_lines = (site.state / "sessions" / "audit.log").read_text().splitlines()
    assert len(audit_lines) == 1
    line = json.loads(audit_lines[0])
    assert line["action"] == "launch" and line["name"] == "alpha" and line["session_id"] == UUID1
    assert line["ok"] is True and "caller" in line and "ts" in line
    assert line["argv"][-1] == f"<{len(PROMPT)} chars>"
    assert PROMPT not in audit_lines[0]


def test_audit_and_record_hold_no_secret(site, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-xyz")
    prompt = "ship the fix; secret sk-test-xyz must stay out of logs"
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s, prompt=prompt)
    audit_text = (site.state / "sessions" / "audit.log").read_text()
    rec_text = (site.state / "sessions" / "alpha.json").read_text()
    assert "sk-test-xyz" not in audit_text  # env value never logged
    assert prompt not in audit_text         # prompt text never logged
    assert "sk-test-xyz" not in rec_text    # env value never recorded
    assert "sk-test-xyz" in (site.state / "sessions" / "alpha.prompt.txt").read_text()
    # the secret must reach a recorded field: the ready-marker card (this
    # assertion fails if _scrub is ever removed from the marker write)
    hf = site.tmp_path / "handoff.md"
    hf.write_text("carry on")
    s.mark_ready("alpha", str(hf), card="card holding sk-test-xyz")
    marker_text = (site.state / "sessions" / "alpha.ready.json").read_text()
    assert "sk-test-xyz" not in marker_text
    assert "[autoos:redacted]" in marker_text
    # error text scrubbed before it reaches the caller
    with pytest.raises(ParameterRefused) as exc:
        go(site, s, mcp_config=str(site.tmp_path / "sk-test-xyz.json"))
    assert "sk-test-xyz" not in str(exc.value)
    # returned results carry no secret either: _shown length-redacts the style
    # text before it reaches the dry-run argv (SESSION-SPAWN-MCP)
    cfg2 = site.tmp_path / "sessions-secret.json"
    write_config(cfg2, json.dumps(
        {**json.loads(site.cfg.read_text()), "style_text": "be terse with sk-test-xyz"}))
    res = go(site, make(site, FakeRun(), config_path=cfg2), name="beta", prompt="abc", dry_run=True)
    assert "sk-test-xyz" not in json.dumps(res)
    assert f"<{len('be terse with sk-test-xyz')} chars>" in json.dumps(res)


def test_audit_on_refusals_name_truncated(site):
    run = FakeRun()
    s = make(site, run)
    with pytest.raises(ParameterRefused, match="name"):
        go(site, s, name="x" * 100)
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.stop(name="ghost")
    with pytest.raises(ParameterRefused, match="handoff"):
        s.mark_ready("alpha", str(site.tmp_path / "ghost-handoff.md"))
    with pytest.raises(ParameterRefused, match="unknown session"):
        s.relaunch("ghost")
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert [l["action"] for l in lines] == ["launch", "stop", "mark_ready", "relaunch"]
    assert all(l["ok"] is False and l["reason"] for l in lines)
    assert lines[0]["name"] == "x" * 80  # name as given, truncated to 80 chars
    assert not run.calls


def test_timeout_expired_is_audited_failure(site):
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            self.calls.append((list(argv), kw))
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    run = TimeoutRun()
    s = make(site, run)
    with pytest.raises(subprocess.TimeoutExpired):
        go(site, s)
    assert [kw for argv, kw in run.calls if argv[:2] == ["claude", "--bg"]][0]["timeout"] == 60
    lines = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["action"] == "launch" and lines[0]["ok"] is False

    class StopTimeout(FakeRun):
        def __call__(self, argv, **kw):
            self.calls.append((list(argv), kw))
            if argv[:2] == ["claude", "stop"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    run2 = StopTimeout(stdout="session " + UUID1)
    s2 = make(site, run2)
    go(site, s2, name="beta", prompt="do beta")
    run2.agents_rows = [{"id": "r", "sessionId": UUID1, "name": "beta",  # r7 adapt
                         "kind": "background", "state": "running",
                         "startedAt": 1_000_000}]
    with pytest.raises(subprocess.TimeoutExpired):
        s2.stop(name="beta")
    lines2 = [json.loads(l) for l in (site.state / "sessions" / "audit.log").read_text().splitlines()]
    assert lines2[-1]["action"] == "stop" and lines2[-1]["ok"] is False


def test_audit_caller_is_principal_or_user(site, monkeypatch):
    import getpass
    monkeypatch.setenv("PLANGRAPH_PRINCIPAL", "operator-7")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)
    go(site, s)
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["caller"] == "operator-7"  # the principal wins
    monkeypatch.delenv("PLANGRAPH_PRINCIPAL")
    monkeypatch.setattr(getpass, "getuser", lambda: "os-user")
    go(site, s, name="beta", prompt="do beta")
    line2 = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line2["caller"] == "os-user"  # else the OS user, never a source location
    assert ".py" not in line2["caller"] and "sessions.py" not in line["caller"]


def test_ok_false_audit_includes_redacted_argv(site):
    secret_prompt = "secret prompt text"
    run = FakeRun(stdout="session " + UUID1, launch_rc=1)
    s = make(site, run)
    res = go(site, s, prompt=secret_prompt)
    assert res["ok"] is False
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["ok"] is False
    assert line["argv"][-1] == f"<{len(secret_prompt)} chars>"  # prompt-redacted argv
    assert secret_prompt not in json.dumps(line)


def test_unexpected_exception_is_audited_and_refused(site, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-xyz")
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def boom(*_a, **_kw):
        raise RuntimeError("backend exploded holding sk-test-xyz")

    monkeypatch.setattr(s, "_check_cwd", boom)
    with pytest.raises(ParameterRefused) as exc:
        go(site, s)
    msg = str(exc.value)
    assert "RuntimeError" in msg and "backend exploded" in msg
    assert "sk-test-xyz" not in msg  # scrubbed before it reaches the caller
    line = json.loads((site.state / "sessions" / "audit.log").read_text().splitlines()[-1])
    assert line["action"] == "launch" and line["ok"] is False
    assert line["reason"] == msg  # type + scrubbed message, same text as the refusal
    assert "sk-test-xyz" not in json.dumps(line)


def test_audit_write_failure_after_success_warns(site, monkeypatch):
    mod = sys.modules[Sessions._audit.__module__]
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    res = go(site, s)
    assert res["ok"] is True and res["session_id"] == UUID1  # the launch succeeded
    assert "audit" in res.get("warning", "")  # the caller is warned, not failed


def test_scrub_touches_only_free_text_fields(site, monkeypatch):
    from plangraph.spawner.sessions import _scrub
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test-xyz")
    out = _scrub({
        "name": "sk-test-xyz", "cwd": "/srv/u/sk-test-xyz", "session_id": "sk-test-xyz",
        "prompt_file": "/tmp/sk-test-xyz.txt", "handoff": "/tmp/sk-test-xyz.md",
        "mcp": "/tmp/sk-test-xyz.json",
        "reason": "r sk-test-xyz", "error": "e sk-test-xyz", "warning": "w sk-test-xyz",
        "card": "c sk-test-xyz", "message": "m sk-test-xyz", "argv": ["claude", "sk-test-xyz"],
        "children": [{"warning": "nested sk-test-xyz"}],
    })
    for key, val in [("name", "sk-test-xyz"), ("cwd", "/srv/u/sk-test-xyz"),
                     ("session_id", "sk-test-xyz"), ("prompt_file", "/tmp/sk-test-xyz.txt"),
                     ("handoff", "/tmp/sk-test-xyz.md"), ("mcp", "/tmp/sk-test-xyz.json")]:
        assert out[key] == val  # structured fields are never redacted
    for key in ("reason", "error", "warning", "card", "message"):
        assert "sk-test-xyz" not in out[key] and "[autoos:redacted]" in out[key]
    assert "sk-test-xyz" not in json.dumps(out["argv"])
    assert "sk-test-xyz" not in out["children"][0]["warning"]  # nested free text too


def test_refusal_outcome_survives_audit_write_failure(site, monkeypatch):
    mod = sys.modules[Sessions._audit.__module__]
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    with pytest.raises(ParameterRefused) as exc:
        go(site, s, name="bad name")
    assert "audit" in exc.value.warning  # the refusal stands; the caller is warned


def test_timeout_outcome_survives_audit_write_failure(site, monkeypatch):
    mod = sys.modules[Sessions._audit.__module__]
    class TimeoutRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
            return super().__call__(argv, **kw)

    s = make(site, TimeoutRun())

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        go(site, s)
    assert "audit" in exc.value.warning  # the timeout stands; the caller is warned


def test_action_failed_outcome_survives_audit_write_failure(site, monkeypatch):
    mod = sys.modules[Sessions._audit.__module__]
    run = FakeRun(stdout="session " + UUID1, launch_rc=1)
    s = make(site, run)

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha" and res["session_id"] is None
    assert "audit" in res["warning"]  # the failure stands; the caller is warned


def test_run_oserror_outcome_survives_audit_write_failure(site, monkeypatch):
    mod = sys.modules[Sessions._audit.__module__]
    class OsErrRun(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:2] == ["claude", "--bg"]:
                raise OSError("exec format error")
            return super().__call__(argv, **kw)

    s = make(site, OsErrRun())

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(mod, "append_private", dead)
    res = go(site, s)
    assert res["ok"] is False and res["name"] == "alpha"
    assert "audit" in res["warning"]  # the failure stands; the caller is warned


def test_unexpected_outcome_survives_audit_write_failure(site, monkeypatch):
    mod = sys.modules[Sessions._audit.__module__]
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run)

    def boom(*_a, **_kw):
        raise RuntimeError("backend exploded")

    def dead(*_a, **_kw):
        raise OSError("audit disk full")

    monkeypatch.setattr(s, "_check_cwd", boom)
    monkeypatch.setattr(mod, "append_private", dead)
    with pytest.raises(ParameterRefused) as exc:
        go(site, s)
    assert "RuntimeError" in str(exc.value) and "backend exploded" in str(exc.value)
    assert "audit" in exc.value.warning  # the refusal stands; the caller is warned


def test_audit_argv_redacts_style_text(site):
    # The value after --append-system-prompt is config style prose: the audit
    # argv must show its length like the prompt, never its text (SESSION-SPAWN-MCP).
    style_text = "STYLE-SECRET-TEXT"  # 17 chars
    cfg2 = site.tmp_path / "sessions-secret.json"
    write_config(cfg2, json.dumps({**json.loads(site.cfg.read_text()), "style_text": style_text}))
    run = FakeRun(stdout="session " + UUID1)
    s = make(site, run, config_path=cfg2)
    assert go(site, s)["ok"] is True
    audit_text = (site.state / "sessions" / "audit.log").read_text()
    assert style_text not in audit_text
    line = json.loads(audit_text.splitlines()[-1])
    argv = line["argv"]
    assert argv[argv.index("--append-system-prompt") + 1] == "<17 chars>"
    res = go(site, s, name="beta", dry_run=True)
    assert all(style_text not in a for a in res["argv"])
