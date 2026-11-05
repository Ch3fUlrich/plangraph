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
        exited = tmp_path / 'intermediate-exited'
        leaf = f'import os,time; open({str(marker)!r},"w").write(str(os.getpid())); time.sleep(60)'
        middle = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{leaf!r}])'
        code = ('import subprocess,sys,time; time.sleep(.5); '
                f'p=subprocess.Popen([sys.executable,"-c",{middle if orphan else leaf!r}]); '
                + (f'p.wait(); open({str(exited)!r},"w").write("done"); ' if orphan else '') + 'time.sleep(60)')
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        service = SpawnService(runner, tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0)
        try:
            for _ in range(500):
                record = service.result(record['run_id'])
                if marker.exists() and marker.read_text() and record.get('pid_start') and (not orphan or exited.exists()):
                    break
                await asyncio.sleep(.01)
            child = int(marker.read_text())
            assert not orphan or exited.exists()  # root waited for intermediate exit
            result = await SpawnService(SpawnerRunner(), tmp_path).cancel(record['run_id'])
            assert not result.get('survivors')
            assert not any(proc_kill.alive(p, None) for p in (record['pid'], child))
        finally:
            await runner.cancel(record['job_id'])
    asyncio.run(run())
def test_foreign_identity_and_stale_parent(monkeypatch):
    from plangraph.spawner import win_recorded as win
    from plangraph.spawner.winjob import WinJobApi
    api = WinJobApi()
    job = api.create_job()
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as foreign:
        try:
            token = win.creation(foreign.pid)
            monkeypatch.setattr(proc_kill, '_nt_table', lambda: {foreign.pid: os.getpid()})
            assert type(token) is int
            for legacy in (None, 'nt'):
                assert asyncio.run(SpawnerRunner().kill_recorded(foreign.pid, legacy)) == [foreign.pid]
            assert asyncio.run(SpawnerRunner().kill_recorded(foreign.pid, token + 1)) == [foreign.pid]
            assert asyncio.run(win.kill(foreign.pid, token + 1, api.name)) == [foreign.pid]
            assert asyncio.run(win.kill(foreign.pid, token, api.name)) == [foreign.pid]
            assert foreign.poll() is None
            with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as root:
                try:
                    monkeypatch.setattr(proc_kill, '_nt_table', lambda: {root.pid: os.getpid(), foreign.pid: root.pid})
                    assert asyncio.run(SpawnerRunner().kill_recorded(root.pid, win.creation(root.pid))) == [root.pid]
                    assert root.poll() is None  # old record: no job name, signal nothing
                    api.assign(job, root.pid)
                    assert asyncio.run(win.kill(root.pid, win.creation(root.pid), api.name)) == []
                    assert foreign.poll() is None
                finally:
                    root.kill()
        finally:
            foreign.kill()
            api.close(job)
