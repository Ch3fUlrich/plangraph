import os
from pathlib import Path
import signal
import subprocess
import sys
import time

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
