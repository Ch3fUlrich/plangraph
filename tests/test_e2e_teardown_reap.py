import os
import ast
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest
sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import rtp1_local_util as u
pytestmark = pytest.mark.skipif(os.name != "posix" or not Path('/proc/self').exists(),
                                reason="requires POSIX sessions and Linux /proc")


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.fixture
def children():
    pids = []
    yield pids
    for pid in pids:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    end = time.monotonic() + 2
    while any(alive(pid) for pid in pids) and time.monotonic() < end:
        time.sleep(.01)
    assert not any(alive(pid) for pid in pids)


def child(pid_dir, children, ignore=False):
    pid_dir.mkdir(exist_ok=True)
    code = ("import os,signal,time,pathlib; "
            f"signal.signal(signal.SIGTERM, {'signal.SIG_IGN' if ignore else 'signal.SIG_DFL'}); "
            f"pathlib.Path({str(pid_dir)!r}, str(os.getpid())).write_text(str(os.getpid())); "
            "time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    children.append(p.pid)
    end = time.monotonic() + 2
    while not (pid_dir / str(p.pid)).exists():
        assert time.monotonic() < end
        time.sleep(.01)
    return p


def test_detached_child(tmp_path, children):
    parent = subprocess.Popen(["sleep", "60"], start_new_session=True)
    children.append(parent.pid)
    p = child(tmp_path / "pids", children)
    try:
        os.killpg(parent.pid, signal.SIGTERM)
        parent.wait(timeout=2)
        assert alive(p.pid)
        assert u.reap_recorded(tmp_path / "pids", grace_s=.1) == [p.pid]
        assert not alive(p.pid)
    finally:
        p.kill() if alive(p.pid) else None
        p.wait(timeout=2)


def test_term_ignorer(tmp_path, children):
    p = child(tmp_path, children, ignore=True)
    try:
        start = time.monotonic()
        assert u.reap_recorded(tmp_path, grace_s=.1) == [p.pid]
        assert time.monotonic() - start < 2.1
        assert not alive(p.pid)
    finally:
        p.kill() if alive(p.pid) else None
        p.wait(timeout=2)


def test_stale_and_missing(tmp_path, children):
    p = subprocess.Popen(["sleep", "60"])
    children.append(p.pid)
    try:
        (tmp_path / str(p.pid)).write_text(str(p.pid))
        (tmp_path / "2147483647").write_text("2147483647")
        assert u.reap_recorded(tmp_path, grace_s=.01) == []
        assert alive(p.pid)
    finally:
        p.kill()
        p.wait(timeout=2)


def test_exited_worker(tmp_path, children):
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    binary = tmp_path / "worker"
    binary.write_text('#!/bin/sh\nsetsid sh -c \'echo $$ > "$PG_FAKE_PID_DIR/$$"; exec sleep 60\' &\n')
    binary.chmod(0o700)
    worker = u.WorkerProc(str(binary), dict(os.environ, PG_JAIL="off",
                          PG_FAKE_PID_DIR=str(pid_dir)), tmp_path / "worker.log")
    try:
        worker.proc.wait(timeout=2)
        end = time.monotonic() + 2
        while not list(pid_dir.iterdir()):
            assert time.monotonic() < end
            time.sleep(.01)
        pid = int(next(pid_dir.iterdir()).name)
        children.append(pid)
        worker.stop()
        assert not alive(pid)
    finally:
        worker.stop()


def test_straggler_does_not_abort(tmp_path, monkeypatch, caplog):
    records = [tmp_path / "101", tmp_path / "102"]
    for record in records:
        record.write_text(record.name)
    original = Path.read_text
    monkeypatch.setattr(Path, "glob", lambda *a: iter(records))
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        "101 (fake) S" if str(p).startswith("/proc/") else original(p))
    monkeypatch.setattr(Path, "read_bytes", lambda p: str(tmp_path).encode())
    clock = iter(range(100))
    monkeypatch.setattr(u.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(u.time, "sleep", lambda _: None)
    signals = []
    monkeypatch.setattr(u.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert u.reap_recorded(tmp_path, grace_s=0) == []
    assert signals == [(p, s) for p in (101, 102) for s in (signal.SIGTERM, signal.SIGKILL)]
    assert "101, 102" in caplog.text


@pytest.mark.parametrize("group_error", [False, True])
def test_stop_reaps_after_timeout_or_group_error(tmp_path, monkeypatch, group_error):
    worker = u.WorkerProc.__new__(u.WorkerProc)
    worker.pid_dir, worker.out, worker.proc = tmp_path, MagicMock(), MagicMock()
    worker.proc.poll.return_value = None
    worker.proc.wait.side_effect = [subprocess.TimeoutExpired("worker", 10), None]
    killpg = MagicMock(side_effect=PermissionError if group_error else None)
    reap = MagicMock()
    monkeypatch.setattr(u.os, "killpg", killpg)
    monkeypatch.setattr(u, "reap_recorded", reap)
    if group_error:
        with pytest.raises(PermissionError):
            worker.stop()
    else:
        worker.stop()
        assert killpg.call_args_list[-1].args[1] == signal.SIGKILL
    reap.assert_called_once_with(tmp_path)
    worker.out.close.assert_called_once()


def test_real_fake_records_and_behaves(tmp_path):
    bin_dir = tmp_path / "bin with spaces"
    u.write_fake_opencode(bin_dir)
    for prompt in ("question", u.ANSWER_TOKEN):
        p = subprocess.Popen([str(bin_dir / "opencode"), "run", "-m", "fake", prompt],
                             cwd=tmp_path, stdout=subprocess.PIPE, text=True)
        stdout, _ = p.communicate(timeout=2)
        assert p.returncode == 0
        assert (bin_dir / "pids" / str(p.pid)).read_text().strip() == str(p.pid)
        assert ("PG_QUESTION:" if prompt == "question" else "wrote out.txt") in stdout
    assert (tmp_path / u.CARD_FILE).read_text() == "done\n"


def test_fake_rejects_changed_shebang(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "FAKE_OPENCODE", u.FAKE_OPENCODE.replace("#!/bin/sh", "#!/bin/bash"))
    with pytest.raises(AssertionError, match="shebang"):
        u.write_fake_opencode(tmp_path / "bin")


def test_fixture_failed_wait_up_stops_worker(tmp_path, monkeypatch, children):
    # Execute the actual fixture body without importing its optional live-service dependencies.
    source = Path(__file__).parent / "e2e" / "test_rtp1_local_e2e.py"
    fixture = next(n for n in ast.parse(source.read_text()).body
                   if isinstance(n, ast.FunctionDef) and n.name == "rig")
    fixture.decorator_list = []
    worker = MagicMock()
    p = child(tmp_path / "pids", children)
    worker.wait_up.side_effect = RuntimeError("startup failed")
    worker.stop.side_effect = lambda: u.reap_recorded(tmp_path / "pids", grace_s=0)
    fake_u = MagicMock()
    fake_u.WorkerProc.return_value = worker
    namespace = dict(u=fake_u, time=time, os=os, sys=sys, DevServer=MagicMock(),
                     World=MagicMock(), seed=MagicMock(), ITEM="item", _BIN="worker",
                     _CLI="cli", BUILD_ID="test")
    exec(compile(ast.Module(body=[fixture], type_ignores=[]), str(source), "exec"), namespace)
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            next(namespace["rig"](tmp_path, monkeypatch, MagicMock(param="ready")))
        worker.stop.assert_called_once()
        assert not alive(p.pid)
    finally:
        p.kill() if alive(p.pid) else None
        p.wait(timeout=2)
