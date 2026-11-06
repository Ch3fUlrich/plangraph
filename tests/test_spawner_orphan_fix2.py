"""Round-two containment regressions with bounded, owned real processes."""
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

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux containment')

# Children spawned with ``python -c`` get no conftest sys.path: bootstrap the
# checkout's src so they run this worktree's modules.
_SRC = str(Path(proc_kill.__file__).resolve().parents[2])


def test_unreaped_deadline_without_proc_children(tmp_path):
    pidfile = tmp_path / 'pid'
    # Hide wait status and proc children, not the actual child or direct kill.
    launch = f"""
import sys
sys.path.insert(0, {_SRC!r})
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


def test_wrapper_does_not_exit_zero_with_live_adopted_child(tmp_path):
    pidfile = tmp_path / 'pid'
    # Hide only the /proc children read: the adopted setsid child stays live and
    # unreaped, so exit status must come from waitpid state alone.
    launch = f"""
import sys
sys.path.insert(0, {_SRC!r})
from unittest.mock import patch
from plangraph.spawner.contained_exec import main
with patch('pathlib.Path.glob', return_value=[]):
    raise SystemExit(main(.02))
"""
    root = (f"import subprocess,sys; open({str(pidfile)!r},'w')"
            f".write(str(subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            f" start_new_session=True).pid))")
    proc = subprocess.Popen([sys.executable, '-c', launch, sys.executable, '-c', root])
    kid = None
    try:
        assert proc.wait(timeout=6) == 128 + signal.SIGKILL
        kid = int(pidfile.read_text())
        assert proc_kill.alive(kid, None)   # survivor: never reported as success
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)
        if kid is None and pidfile.exists():
            kid = int(pidfile.read_text())
        if kid and proc_kill.alive(kid, None):
            os.kill(kid, signal.SIGKILL)


def test_anchored_kill_finishes_and_wrapper_is_gone(tmp_path):
    pidfile = tmp_path / 'pid'
    wrapper = subprocess.Popen(
        contained_exec.command([sys.executable, '-c',
                                f"import os,time; open({str(pidfile)!r},'w')"
                                f".write(str(os.getpid())); time.sleep(30)"]),
        start_new_session=True)
    try:
        ident = proc_kill.root_identity(wrapper.pid)
        assert ident is not None
        asyncio.run(asyncio.wait_for(
            proc_kill.kill_process_tree(wrapper.pid, ident=ident), 15))
        assert wrapper.wait(timeout=5) is not None   # the anchor left by itself
        assert not proc_kill.alive(wrapper.pid, ident)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.wait(timeout=5)
        if pidfile.exists():
            kid = int(pidfile.read_text())
            if proc_kill.alive(kid, None):
                os.kill(kid, signal.SIGKILL)


def test_no_pidfd_means_no_signal_and_survivor_reported(monkeypatch):
    """Without a pidfd there is no identity proof: nothing is signalled and the
    survivor stays recorded so the kill ends in KillIncomplete."""
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             start_new_session=True)
    try:
        start = proc_kill.root_identity(child.pid)
        with monkeypatch.context() as m:
            m.delattr(os, 'pidfd_open')
            m.setattr(os, 'kill', lambda *args: pytest.fail('signal without pidfd proof'))
            proc_kill.signal_recorded(child.pid, start)
            assert child.poll() is None
        with pytest.raises(proc_kill.KillIncomplete) as error:
            asyncio.run(asyncio.wait_for(
                proc_kill.verify(child.pid, start, {child.pid: start}, wait_s=0), 10))
        assert error.value.survivors == [child.pid]
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_live_group_without_listed_member_fails_closed(tmp_path, monkeypatch):
    pidfile = tmp_path / 'pid'
    root = subprocess.Popen(
        [sys.executable, '-c',
         f"import os,subprocess,sys; open({str(pidfile)!r},'w')"
         f".write(str(subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']).pid))"],
        start_new_session=True)
    try:
        ident = proc_kill.root_identity(root.pid)
        assert ident is not None
        root.wait(timeout=3)
        # The orphaned child keeps the group alive while tmp_path lists no member.
        monkeypatch.setattr(proc_kill, 'WAIT_S', .05)
        with pytest.raises(proc_kill.KillIncomplete) as error:
            asyncio.run(asyncio.wait_for(
                proc_kill.kill_process_tree(root.pid, ident=ident, proc_root=str(tmp_path)), 10))
        assert error.value.survivors == [root.pid]
    finally:
        root.wait(timeout=3)
        if pidfile.exists():
            kid = int(pidfile.read_text())
            if proc_kill.alive(kid, None):
                os.kill(kid, signal.SIGKILL)
