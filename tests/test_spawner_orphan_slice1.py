"""Real Linux processes for cancellation ordering and disposable probe cleanup."""
import asyncio
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.spawner import leg_probe, proc_kill
from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService
from plangraph.routing_support.leg_state import LegStateStore

pytestmark = pytest.mark.skipif(not Path('/proc/self/stat').exists(), reason='Linux procfs')
TREE = ("import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n")


async def ready(path):
    async with asyncio.timeout(5):
        while not path.exists() or not path.read_text():
            await asyncio.sleep(.01)
    return int(path.read_text())


def test_unmocked_cancel_leaves_no_live_descendant(tmp_path):
    async def run():
        async def factory(*args, **kw):
            kw['cwd'] = str(tmp_path)
            return await asyncio.create_subprocess_exec(sys.executable, '-c', TREE, **kw)
        runner = SpawnerRunner(process_factory=factory)
        job = await runner.spawn('test', model_or_combo='mimo/mimo-v2.6-flash', wait_seconds=0)
        proc = runner._processes[job.job_id]
        try:
            kid = await ready(tmp_path / 'child.pid')
            result = await runner.cancel(job.job_id)
            assert result['status'] == 'cancelled'
            assert not proc_kill.alive(kid, None)
            assert not proc_kill.alive(proc.pid, None)
        finally:
            await runner.cancel(job.job_id)
    asyncio.run(run())


@pytest.mark.parametrize('cross_process', [False, True])
def test_unmocked_survivor_never_publishes_cancelled(tmp_path, cross_process):
    async def run():
        async def factory(*args, **kw):
            return await asyncio.create_subprocess_exec(
                sys.executable, '-c', 'import time; time.sleep(60)', **kw)
        # Deliberately unprovable identity: a real survivor must not be signalled.
        runner = SpawnerRunner(process_factory=factory)
        runner.identity_provider = lambda *a, **k: None
        job = await runner.spawn('test', model_or_combo='mimo/mimo-v2.6-flash', wait_seconds=0)
        proc = runner._processes[job.job_id]
        service = SpawnService(SpawnerRunner() if cross_process else runner, tmp_path)
        record = dict(run_id='run-survivor', job_id=job.job_id, status='running', pid=proc.pid)
        observed = []

        async def observe():
            while True:
                observed.append(runner.jobs[job.job_id].status)
                observed.append(service.result('run-survivor')['status'])
                await asyncio.sleep(0)

        watcher = None
        try:
            service._save(record)
            watcher = asyncio.create_task(observe())
            out = await service.cancel('run-survivor')
            assert out['status'] == 'failed' and out['reason'] == 'kill_incomplete'
            assert out['survivors'] == [proc.pid]
            assert proc_kill.alive(proc.pid, None)
            assert 'cancelled' not in observed
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            os.kill(proc.pid, signal.SIGKILL)
            await proc.wait()
            await runner.cancel(job.job_id)
    asyncio.run(run())


@pytest.mark.parametrize('exit_path', ['timeout', 'exception', 'cancel'])
def test_unmocked_probe_exit_cleans_tree_and_dir(tmp_path, exit_path):
    async def run():
        launched = asyncio.Event()
        box = {}

        async def factory(*args, **kw):
            box['root'] = Path(kw['cwd'])
            box['proc'] = await asyncio.create_subprocess_exec(sys.executable, '-c', TREE, **kw)
            box['kid'] = await ready(box['root'] / 'child.pid')
            launched.set()
            return box['proc']

        class Runner(SpawnerRunner):
            async def spawn(self, **kw):
                job = await super().spawn(**kw)
                if exit_path == 'exception':
                    raise RuntimeError('registered launch failed')
                return job

        runner = Runner(process_factory=factory)
        task = asyncio.create_task(leg_probe.probe(
            runner, SimpleNamespace(model='mimo/mimo-v2.6-flash', harness='opencode'),
            timeout_s=1, store=LegStateStore(tmp_path / 'legs.sqlite')))
        try:
            await asyncio.wait_for(launched.wait(), 5)
            if exit_path == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert not (await asyncio.wait_for(task, 10))['ok']
            assert not box['root'].exists()
            assert not proc_kill.alive(box['kid'], None)
            assert not proc_kill.alive(box['proc'].pid, None)
        finally:
            for job in runner.jobs.values():
                await runner.cancel(job.job_id)
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
