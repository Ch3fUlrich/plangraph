import asyncio
import os
import subprocess
import sys
import pytest
from plangraph.spawner import proc_kill
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows creation times')
@pytest.mark.parametrize('orphan', [False, True])
def test_second_service_tree(tmp_path, orphan):
    async def run():
        marker = tmp_path / 'grandchild'
        leaf = f'import os,time; open({str(marker)!r},"w").write(str(os.getpid())); time.sleep(60)'
        middle = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{leaf!r}])'
        code = ('import subprocess,sys,time; time.sleep(.5); '
                f'p=subprocess.Popen([sys.executable,"-c",{middle if orphan else leaf!r}]); '
                + ('p.wait(); ' if orphan else '') + 'time.sleep(60)')
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        service = SpawnService(runner, tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0)
        try:
            for _ in range(500):
                record = service.result(record['run_id'])
                if marker.exists() and marker.read_text() and record.get('pid_start'):
                    break
                await asyncio.sleep(.01)
            child = int(marker.read_text())
            await asyncio.sleep(.3)  # intermediate has exited before the second service observes anything
            result = await SpawnService(SpawnerRunner(), tmp_path).cancel(record['run_id'])
            assert not result.get('survivors')
            assert not any(proc_kill.alive(p, None) for p in (record['pid'], child))
        finally:
            await runner.cancel(record['job_id'])
    asyncio.run(run())
def test_foreign_identity_and_stale_parent(monkeypatch):
    from plangraph.spawner import win_recorded as win
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as foreign:
        try:
            token = win.creation(foreign.pid)
            monkeypatch.setattr(proc_kill, '_nt_table', lambda: {foreign.pid: os.getpid()})
            assert type(token) is int
            for legacy in (None, 'nt'):
                assert asyncio.run(SpawnerRunner().kill_recorded(foreign.pid, legacy)) == [foreign.pid]
            assert asyncio.run(SpawnerRunner().kill_recorded(foreign.pid, token + 1)) == [foreign.pid]
            assert foreign.poll() is None
            with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as root:
                try:
                    monkeypatch.setattr(proc_kill, '_nt_table', lambda: {root.pid: os.getpid(), foreign.pid: root.pid})
                    assert asyncio.run(SpawnerRunner().kill_recorded(root.pid, win.creation(root.pid))) == [root.pid]
                    assert root.poll() is None  # old record: no job name, signal nothing
                    assert foreign.poll() is None
                finally:
                    root.kill()
        finally:
            foreign.kill()
