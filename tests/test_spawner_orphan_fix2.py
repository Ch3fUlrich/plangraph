"""Round-two containment regressions with bounded, owned real processes."""
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from plangraph.spawner import contained_exec, proc_kill
from plangraph.spawner.exit_outcome import terminal_reason

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux containment')


def test_unreaped_deadline_without_proc_children(tmp_path):
    pidfile = tmp_path / 'pid'
    # Hide wait status and proc children, not the actual child or direct kill.
    launch = f"""
import os, signal, threading, time
from unittest.mock import patch
from plangraph.spawner.contained_exec import main
real_kill = os.kill
def trigger():
    while not os.path.exists({str(pidfile)!r}): time.sleep(.01)
    real_kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=trigger, daemon=True).start()
with patch('os.waitpid', return_value=(0, 0)), patch('pathlib.Path.glob', return_value=[]):
    raise SystemExit(main(.02))
"""
    root = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, '-c', launch, sys.executable, '-c', root])
    kid = None
    try:
        assert proc.wait(timeout=4) == 143
        kid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while proc_kill.alive(kid, None) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not proc_kill.alive(kid, None)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        if kid is None and pidfile.exists():
            kid = int(pidfile.read_text())
        if kid and proc_kill.alive(kid, None):
            os.kill(kid, signal.SIGKILL)


@pytest.mark.parametrize('tail,code,event,expected', [
    (None, 1, None, 'harness exit 1'),
    (contained_exec.CONTAINMENT_MARKER, 1, None, contained_exec.CONTAINMENT_MARKER),
    (contained_exec.CONTAINMENT_MARKER, 125, 'writer_no_changes', 'writer_no_changes'),
])
def test_marker_validation(tail, code, event, expected):
    record = dict(status='failed', exit_code=code, stderr_tail=tail,
                  events=[{'event': event}] if event else [])
    terminal_reason(record)
    assert record['reason'] == expected


def test_recorded_setsid_survivor_after_group_death(monkeypatch):
    root = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        ident = proc_kill.root_identity(root.pid)
        start = proc_kill.root_identity(kid.pid)
        root.wait(timeout=3)
        monkeypatch.setattr(proc_kill, 'WAIT_S', .05)
        monkeypatch.setattr(proc_kill, 'signal_recorded', lambda *a: None)
        with pytest.raises(proc_kill.KillIncomplete) as error:
            asyncio.run(proc_kill.kill_process_tree(root.pid, ident=ident, members={kid.pid: start}))
        assert error.value.survivors == [kid.pid]
    finally:
        for proc in (root, kid):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)


def test_fallback_rechecks_identity_before_kill(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        start = proc_kill._stat(child.pid)[4]
        real_alive = proc_kill.alive
        checks = []
        def changing_identity(pid, token, **kwargs):
            checks.append(pid)
            return real_alive(pid, token if len(checks) == 1 else token + 1, **kwargs)
        monkeypatch.delattr(os, 'pidfd_open')
        monkeypatch.setattr(proc_kill, 'alive', changing_identity)
        proc_kill.signal_recorded(child.pid, start)
        assert len(checks) == 2
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
