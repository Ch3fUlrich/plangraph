"""Bounded real-process regressions for the private containment wrapper."""
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plangraph.spawner import contained_exec, proc_kill
from plangraph.spawner.exit_outcome import terminal_reason

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux subreaper')

# Children spawned with ``python -c`` get no conftest sys.path: bootstrap the
# checkout's src so they run this worktree's modules.
_SRC = str(Path(proc_kill.__file__).resolve().parents[2])


@pytest.mark.parametrize('terminate', [False, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_wrapper_bounds_daemons_and_forwards(tmp_path, terminate):
    pidfile = tmp_path / 'pid'
    root = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); "
            f"open({str(pidfile)!r},'w').write(str(p.pid)); "
            + ('time.sleep(60)' if terminate else 'sys.exit(23)'))
    launch = (f"import sys; sys.path.insert(0, {_SRC!r}); "
              "from plangraph.spawner.contained_exec import main; raise SystemExit(main(.15))")
    proc = subprocess.Popen([sys.executable, '-c', launch, sys.executable, '-c', root])
    kid = None
    try:
        deadline = time.monotonic() + 3
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline
            time.sleep(.01)
        kid = int(pidfile.read_text())
        if terminate:
            proc.send_signal(terminate)
        assert proc.wait(timeout=3) == (128 + terminate if terminate else 23)
        assert not proc_kill.alive(kid, None)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        if kid and proc_kill.alive(kid, None):
            os.kill(kid, signal.SIGKILL)


def test_late_fork_after_snapshot(tmp_path):
    gate, pidfile = tmp_path / 'gate', tmp_path / 'pid'
    code = ("import pathlib,subprocess,sys,time; "
            f"g=pathlib.Path({str(gate)!r}); "
            "exec('while not g.exists(): time.sleep(.01)'); "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
    kid = None
    try:
        ident = proc_kill.root_identity(proc.pid)
        members = proc_kill.snapshot(proc.pid, ident)
        gate.touch()
        deadline = time.monotonic() + 3
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline
            time.sleep(.01)
        kid = int(pidfile.read_text())
        asyncio.run(proc_kill.kill_process_tree(proc.pid, ident=ident, members=members))
        proc.wait(timeout=3)
        assert not proc_kill.alive(kid, None)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        if kid and proc_kill.alive(kid, None):
            os.kill(kid, signal.SIGKILL)


def test_denial_marker_not_exit_125():
    code = (f"import sys; sys.path.insert(0, {_SRC!r}); "
            "from unittest.mock import patch; from types import SimpleNamespace; "
            "from plangraph.spawner.contained_exec import main; "
            "exec('with patch(\"ctypes.CDLL\", return_value=SimpleNamespace(prctl=lambda *a: -1)):\\n raise SystemExit(main())')")
    denied = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=3)
    harness = subprocess.run(contained_exec.command([sys.executable, '-c', 'raise SystemExit(125)']),
                             capture_output=True, timeout=3)
    for result, expected in ((denied, 'containment_unavailable'), (harness, 'harness exit 125')):
        assert result.returncode == 125
        record = dict(status='failed', exit_code=125, stderr_tail=result.stderr.decode())
        terminal_reason(record)
        assert record['reason'] == expected


def test_no_pidfd_never_signals_recorded_child(monkeypatch):
    # Round-3 contract: without a pidfd there is no identity proof, so nothing is
    # signalled (the old fallback os.kill is gone) and the survivor is reported.
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        start = proc_kill.root_identity(proc.pid)
        with monkeypatch.context() as m:
            m.delattr(os, 'pidfd_open')
            m.setattr(os, 'kill', lambda *args: pytest.fail('signal without pidfd proof'))
            proc_kill.signal_recorded(proc.pid, start)
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
