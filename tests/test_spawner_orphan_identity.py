"""Fail-closed pinned signalling and unavailable-handle diagnostics."""
import asyncio
import os
import signal
import subprocess
import sys

import pytest

from plangraph.spawner import proc_kill


@pytest.mark.parametrize('race', ['reuse', 'vanished', 'read'])
def test_pinned_signal_fails_closed(monkeypatch, race):
    sent = []
    monkeypatch.setattr(os, 'pidfd_open', lambda pid: 42)
    monkeypatch.setattr(os, 'close', lambda fd: None)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *args: sent.append(args))
    monkeypatch.setattr(proc_kill, '_stat', lambda *a: ('S', 1, 5, 5, 999) if race == 'reuse' else None)
    proc_kill.signal_recorded(5, 123)
    assert sent == []


def test_unavailable_pidfd_reports_real_survivor(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        start = proc_kill.root_identity(child.pid)
        monkeypatch.delattr(os, 'pidfd_open')
        proc_kill.signal_recorded(child.pid, start + 1)  # fallback must reject reused identity
        with pytest.raises(proc_kill.KillIncomplete) as error:
            asyncio.run(proc_kill.verify(child.pid, start, {child.pid: start}, wait_s=0))
        assert error.value.survivors == [child.pid]
    finally:
        child.kill()
        child.wait(timeout=5)


def test_default_factory_subreaper_does_not_reap_unrelated_child():
    from plangraph.spawner.runner import SpawnerRunner
    async def run():
        unrelated = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(17)'])
        proc = await SpawnerRunner().process_factory(
            sys.executable, '-c', 'raise SystemExit(3)', start_new_session=True)
        try:
            assert await asyncio.wait_for(proc.wait(), 5) == 3
            assert unrelated.wait(timeout=5) == 17
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if unrelated.poll() is None:
                unrelated.kill()
                unrelated.wait(timeout=5)
    asyncio.run(run())
