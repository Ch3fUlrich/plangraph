"""Hermetic AutoOS ready-v2 contracts, adapted to the plangraph hooks layout."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from hookkit import HOOKS, SID, load_module, run_script, usage_line

SCRIPT = HOOKS / "context_guard.py"


def make_ready_fixture(tmp):
    handoff = tmp / "handoff.md"
    handoff.write_bytes(b"handoff\r\n")
    card = handoff.with_suffix(".card.md")
    card.write_bytes(b"# worker card\n")
    checker = tmp / "handoff-check"
    checker.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                       "assert len(sys.argv) == 2\nPath(sys.argv[1]).read_bytes()\n")
    checker.chmod(0o755)
    env = {**{k: os.environ[k] for k in ("SystemRoot", "PATHEXT") if k in os.environ},
           "PATH": str(tmp), "HOME": str(tmp), "AUTOOS_READY_KICK": "0",
           "XDG_STATE_HOME": str(tmp / "state"), "CLAUDE_CODE_SESSION_ID": SID}
    if os.name == "nt":
        checker.with_suffix(".cmd").write_text(f'@"{sys.executable}" "{checker}" %*\n')
    marker = tmp / "state/autoos/ctx-guard" / (SID + ".ready.json")

    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, cwd=tmp,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
    return handoff, card, checker, env, marker, run


@pytest.fixture
def ready(tmp_path):
    return make_ready_fixture(tmp_path)


def test_complete_marker_and_status(ready):
    handoff, card, _, _, marker, run = ready
    p = run("ready", str(handoff))
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    m = json.loads(marker.read_text())
    assert m == dict(v=2, session_id=SID, session_name=None, role=None, launched_at=None,
                     handoff=str(handoff), card=str(card), ctx_tokens=None, reason="cap", agents=None,
                     handoff_sha256=hashlib.sha256(handoff.read_bytes()).hexdigest(),
                     card_sha256=hashlib.sha256(card.read_bytes()).hexdigest(),
                     cwd=str(handoff.parent), ts=m["ts"])
    assert isinstance(m["ts"], (int, float))
    assert list(marker.parent.iterdir()) == [marker]
    assert run("status").stdout == f"{SID} ready handoff={handoff}\n"


@pytest.mark.parametrize("failure", ["invalid", "missing", "unexecutable", "card", "handoff"])
def test_refuses_without_checked_pair(ready, failure):
    handoff, card, checker, _, marker, run = ready
    if failure == "invalid":
        with checker.open("a") as fh:
            fh.write("print('missing heading Goal')\nsys.exit(1)\n")
    elif failure == "missing":
        checker.unlink()
        checker.with_suffix(".cmd").unlink(missing_ok=True)
    elif failure == "unexecutable":
        if os.name == "nt":
            pytest.skip("Windows has no executable permission bits")
        checker.chmod(0o644)
    else:
        (card if failure == "card" else handoff).unlink()
    p = run("ready", str(handoff))
    assert p.returncode == 2 and p.stderr and not marker.exists()
    if failure == "invalid":
        assert "missing heading Goal" in p.stderr
    if failure in ("missing", "unexecutable"):
        assert "handoff-check" in p.stderr


@pytest.mark.parametrize("reason", ["cap", "router", "operator", "planned", "bogus"])
def test_reason(ready, reason):
    handoff, _, _, _, marker, run = ready
    p = run("ready", str(handoff), "--reason", reason)
    assert p.returncode == (2 if reason == "bogus" else 0), p.stderr
    if reason != "bogus":
        assert json.loads(marker.read_text())["reason"] == reason
    else:
        assert "reason" in p.stderr and not marker.exists()


def test_ready_argument_and_guarded_errors(ready):
    handoff, _, _, _, marker, run = ready
    for args, diagnostic in ((("--reason",), "ready needs"), (("--unknown",), "ready needs"),
                             ((str(handoff), "--reason"), "--reason needs a value"),
                             ((str(handoff), "--reason", "--other"), "--reason needs a value"),
                             ((str(handoff), "extra"), "ready needs"), (("",), "validation failed"),
                             (("/",), "validation failed")):
        p = run("ready", *args)
        assert p.returncode == 2 and diagnostic in p.stderr and "Traceback" not in p.stderr
        assert not marker.exists()


@pytest.mark.parametrize("which", [0, 1])
def test_changed_pair_refused(ready, which):
    handoff, card, checker, _, marker, run = ready
    with checker.open("a") as fh:
        fh.write(f"Path({str((handoff, card)[which])!r}).write_bytes(b'changed')\n")
    assert run("ready", str(handoff)).returncode == 2
    assert not marker.exists()


def test_checker_override_and_real_pair(ready):
    handoff, card, checker, env, _, run = ready
    with checker.open("a") as fh:
        fh.write(f"assert Path(sys.argv[1]) == Path({str(card)!r})\n"
                 "assert Path(sys.argv[1]).with_suffix('').with_suffix('.md').read_bytes()\n")
    renamed = checker.with_name("custom-check")
    checker.rename(renamed)
    if os.name == "nt":
        wrapper = renamed.with_suffix(".cmd")
        wrapper.write_text(f'@"{sys.executable}" "{renamed}" %*\n')
        renamed = wrapper
    env["AUTOOS_HANDOFF_CHECK"] = str(renamed)
    assert run("ready", str(handoff)).returncode == 0


def test_atomic_marker_and_post_replace_race(ready):
    handoff, card, _, env, marker, _ = ready
    guard = load_module("context_guard")
    original = guard._write_json
    real_replace = os.replace
    observed = []

    def replace(src, dst):
        assert not marker.exists() and Path(dst) == marker
        assert json.loads(Path(src).read_text())["v"] == 2
        observed.append(dst)
        real_replace(src, dst)

    def moved(path, data):
        original(path, data)
        card.write_bytes(b"moved")

    with patch.dict(os.environ, env, clear=True):
        with patch.object(os, "replace", replace):
            assert guard.cmd_ready([str(handoff)]) == 0
        assert observed == [str(marker)]
        marker.unlink()
        with patch.object(guard, "_write_json", moved):
            assert guard.cmd_ready([str(handoff)]) == 2
            assert not marker.exists()
        with patch.object(os, "getcwd", side_effect=FileNotFoundError("deleted cwd")):
            assert guard.cmd_ready([str(handoff)]) == 2
            assert not marker.exists()


def test_checker_timeout_is_loud_but_hook_ignores_broken_checker(ready):
    handoff, _, checker, env, marker, _ = ready
    guard = load_module("context_guard")
    with patch.dict(os.environ, env, clear=True), patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("handoff-check", 10)):
        assert guard.cmd_ready([str(handoff)]) == 2
    assert not marker.exists()
    checker.unlink()
    checker.with_suffix(".cmd").unlink(missing_ok=True)
    transcript = handoff.with_suffix(".jsonl")
    transcript.write_text(usage_line(inp=280000) + "\n")
    payload = {"transcript_path": str(transcript), "session_id": SID, "cwd": str(handoff.parent)}
    # Hook mode never calls the checker; only ready does. Missing it cannot hide guidance.
    p = run_script(SCRIPT, payload, env=env, cwd=handoff.parent)
    assert (p.returncode, p.stderr) == (0, "")
    assert not marker.exists()
    assert json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] == guard.emit_line(
        "handoff", 280000, 270000)


@pytest.mark.parametrize("outcome", ["success", "refused", "failed", "missing", "timeout", "disabled"])
def test_ready_kick_and_checker_resolution(ready, outcome, capsys):
    handoff, _, checker, env, marker, _ = ready
    guard = load_module("context_guard")
    if outcome != "disabled":
        env.pop("AUTOOS_READY_KICK")
    resolved = str(checker.with_suffix(".cmd") if os.name == "nt" else checker)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] != "systemctl":
            assert argv == [resolved, str(handoff.with_suffix(".card.md"))]
            assert kwargs["timeout"] == 10
            return subprocess.CompletedProcess(argv, int(outcome == "refused"), "", "")
        assert marker.is_file() and kwargs["timeout"] == 5 and kwargs["check"] is True
        assert argv == ["systemctl", "--user", "start", "--no-block", "ctx-watchdog.service"]
        if outcome == "missing":
            raise FileNotFoundError("missing systemctl")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if outcome == "failed":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch.dict(os.environ, env, clear=True), patch.object(guard.shutil, "which", return_value=resolved) as which, \
            patch.object(subprocess, "run", side_effect=fake_run):
        assert guard.cmd_ready([str(handoff)]) == (2 if outcome == "refused" else 0)
        which.assert_called_once_with("handoff-check", path=env["PATH"])
    assert len(calls) == (1 if outcome in ("refused", "disabled") else 2)
    assert marker.exists() == (outcome != "refused")
    if outcome in ("failed", "missing", "timeout"):
        err = capsys.readouterr().err
        assert len(err.splitlines()) == 1 and "timer" in err


def _pretooluse_payload(session_id=SID, tool_name="Bash", tool_input=None):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {"command": "git status"},
    }


def test_pretooluse_denies_when_ready_marker_exists(ready):
    handoff, _, _, env, marker, run = ready
    assert run("ready", str(handoff)).returncode == 0
    ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(json.loads(marker.read_text())["ts"]))
    expected = (f"context_guard: ready was sent at {ts_str} (handoff {handoff}); "
                "your successor owns the role. End your turn now with one final line; no further tool calls.")
    for payload in (
        _pretooluse_payload(tool_name="Bash", tool_input={"command": "git merge main"}),
        _pretooluse_payload(tool_name="Edit", tool_input={"file_path": "/tmp/a"}),
        _pretooluse_payload(tool_name="SendMessage", tool_input={"to": "parent"}),
    ):
        proc = run_script(SCRIPT, payload, env=env, cwd=handoff.parent)
        assert (proc.returncode, proc.stderr) == (0, "")
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out == {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": expected,
        }


def test_pretooluse_allows_without_ready_marker(ready):
    handoff, _, _, env, marker, _ = ready
    assert not marker.exists()
    for tool_name, tool_input in (("Bash", {"command": "git status"}), ("Edit", {"file_path": "a"})):
        proc = run_script(SCRIPT, _pretooluse_payload(tool_name=tool_name, tool_input=tool_input),
                          env=env, cwd=handoff.parent)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_pretooluse_allows_other_session_ready_marker(ready):
    handoff, _, _, env, marker, run = ready
    assert run("ready", str(handoff)).returncode == 0
    proc = run_script(SCRIPT, _pretooluse_payload(session_id="other-session-id"),
                      env=env, cwd=handoff.parent)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    other_payload = json.loads(marker.read_text())
    other_payload["session_id"] = "other-session-id"
    marker.write_text(json.dumps(other_payload))
    proc = run_script(SCRIPT, _pretooluse_payload(session_id=SID), env=env, cwd=handoff.parent)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_pretooluse_allows_ready_and_status_retry_only(ready):
    handoff, _, _, env, _, run = ready
    assert run("ready", str(handoff)).returncode == 0
    installed = Path(env["HOME"]) / ".local" / "share" / "plangraph" / "hooks" / "context_guard.py"
    evil_dir = handoff.parent / "evil"
    evil_dir.mkdir()
    (evil_dir / "context_guard.py").write_text("print('pwned')\n", encoding="utf-8")
    evil_py = evil_dir / "python3"
    try:
        evil_py.symlink_to(sys.executable)
    except OSError:
        evil_py.write_text("", encoding="utf-8")
    allowed = (
        f"python3 {SCRIPT} ready {handoff}",
        f"python {SCRIPT} ready {handoff} --reason router",
        f'python3 "{SCRIPT}" ready "{handoff}" --reason cap',
        f"{sys.executable} {SCRIPT} status",
        f"python3 {installed} status",
        f"python3 {installed} ready {handoff} --reason operator",
        f"{SCRIPT} status",
    )
    if Path("/usr/bin/python3").exists():
        allowed = allowed + (f"/usr/bin/python3 {SCRIPT} status",)
    for command in allowed:
        proc = run_script(SCRIPT, _pretooluse_payload(tool_name="Bash", tool_input={"command": command}),
                          env=env, cwd=handoff.parent)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", ""), command
    denied = (
        "python3 /tmp/evil/context_guard.py status",
        f"python3 {evil_dir / 'context_guard.py'} status",
        "PATH=/tmp/evil python3 context_guard.py status",
        "LD_PRELOAD=x python3 context_guard.py ready y",
        f"CLAUDE_CODE_SESSION_ID={SID} python3 {SCRIPT} status",
        "python3 context_guard.py status",
        f"{evil_py} {SCRIPT} status",
        "/tmp/evil/python3 " + f"{SCRIPT} status",
        f"python3.99 {SCRIPT} status",
        f"python3 {SCRIPT} ready -x",
        f"python3 {SCRIPT} ready {handoff} --reason bogus",
        f"python3 {SCRIPT} ready {handoff} --reason -x",
        f"python3 {SCRIPT} status extra",
        f"python3 {SCRIPT} status && git push",
        f"python3 {SCRIPT} ready {handoff}; rm -rf /",
        f"python3 {SCRIPT} ready $(id)",
        f"python3 {SCRIPT} ready `id`",
        f"python3 {SCRIPT} status > /tmp/out",
        'python3 -c "import os"',
        f"python3 {SCRIPT}",
        f'python3 {SCRIPT} ready "/unclosed',
        "git status",
    )
    for command in denied:
        proc = run_script(SCRIPT, _pretooluse_payload(tool_name="Bash", tool_input={"command": command}),
                          env=env, cwd=handoff.parent)
        assert (proc.returncode, proc.stderr) == (0, ""), command
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny", command
    proc = run_script(
        SCRIPT,
        {**_pretooluse_payload(tool_name="Bash", tool_input={"command": "python3 context_guard.py status"}),
         "cwd": str(evil_dir)},
        env=env,
        cwd=HOOKS,
    )
    assert (proc.returncode, proc.stderr) == (0, "")
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_denies_subagent_when_ready_marker_exists(ready):
    handoff, _, _, env, _, run = ready
    subagent_payload = {**_pretooluse_payload(tool_name="Bash", tool_input={"command": "git status"}),
                        "agent_id": "subagent-1"}
    proc = run_script(SCRIPT, subagent_payload, env=env, cwd=handoff.parent)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert run("ready", str(handoff)).returncode == 0
    for hook in (SCRIPT, HOOKS / "ctx_guard_session.py"):
        proc = run_script(hook, subagent_payload, env=env, cwd=handoff.parent)
        assert (proc.returncode, proc.stderr) == (0, "")
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["permissionDecision"] == "deny"
        assert str(handoff) in out["permissionDecisionReason"]


def test_pretooluse_corrupt_or_non_v2_marker_fails_open(ready):
    handoff, _, _, env, marker, _ = ready
    marker.parent.mkdir(parents=True, exist_ok=True)
    for raw in (
        b"{not json",
        b"\xff\xfe\x00",
        b"[1, 2]",
        b"{}",
        json.dumps({"v": 1, "session_id": SID, "handoff": str(handoff), "ts": 1.0}).encode(),
        b"x" * (1024 * 1024 + 10),
    ):
        marker.write_bytes(raw)
        proc = run_script(SCRIPT, _pretooluse_payload(), env=env, cwd=handoff.parent)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    marker.unlink()
    marker.mkdir()
    proc = run_script(SCRIPT, _pretooluse_payload(), env=env, cwd=handoff.parent)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    marker.rmdir()
    marker.write_text(json.dumps({"v": 2, "session_id": SID, "handoff": 123, "ts": 1e30}))
    proc = run_script(SCRIPT, _pretooluse_payload(), env=env, cwd=handoff.parent)
    assert (proc.returncode, proc.stderr) == (0, "")
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "ready was sent at unknown (handoff unknown)" in out["permissionDecisionReason"]


def test_pretooluse_ctx_guard_session_denies_even_when_off(ready):
    handoff, _, _, env, _, run = ready
    assert run("ready", str(handoff)).returncode == 0
    override = Path(env["XDG_STATE_HOME"]) / "claude-hooks" / "ctx-guard-session" / f"{SID}.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"off": True}), encoding="utf-8")
    proc = run_script(HOOKS / "ctx_guard_session.py", _pretooluse_payload(), env=env, cwd=handoff.parent)
    assert (proc.returncode, proc.stderr) == (0, "")
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert str(handoff) in out["permissionDecisionReason"]
