"""Hermetic AutoOS ready-v2 contracts, adapted to the plangraph hooks layout."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from hookkit import HOOKS, SID, load_module

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
    handoff, _, checker, env, marker, run = ready
    guard = load_module("context_guard")
    with patch.dict(os.environ, env, clear=True), patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("handoff-check", 10)):
        assert guard.cmd_ready([str(handoff)]) == 2
    assert not marker.exists()
    checker.unlink()
    p = run()
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


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
