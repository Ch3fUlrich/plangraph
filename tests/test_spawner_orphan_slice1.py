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


@pytest.mark.parametrize('status,code,survivors', [('completed', 0, []), ('running', None, [123])])
def test_cancel_publication_preserves_terminal_and_guards_survivors(status, code, survivors):
    async def run():
        runner = SpawnerRunner()
        job = SimpleNamespace(status=status, exit_code=code, survivors=survivors, events=[])
        runner.jobs['j'] = job
        result = await runner.cancel('j')
        assert result['status'] == ('failed' if survivors else status)
        assert job.exit_code == (1 if survivors else code)
        if survivors:
            assert job.events[-1]['event'] == 'kill_incomplete'
    asyncio.run(run())


def test_real_reaper_publishes_survivors_as_failed():
    async def run():
        from plangraph.spawner.reap import reap
        runner = SpawnerRunner()
        job = SimpleNamespace(status='running', exit_code=None, survivors=[], events=[])
        runner.jobs['j'] = job
        await reap(runner, 'j', SimpleNamespace(pid=os.getpid()))
        assert job.status == 'failed' and job.exit_code == 1
        assert os.getpid() in job.survivors
        assert job.events[-1]['event'] == 'kill_incomplete'
    asyncio.run(run())


def test_probe_cleanup_reaches_later_jobs_after_error(tmp_path):
    async def run():
        from plangraph.spawner.probe_cleanup import cleanup
        jobs = {k: SimpleNamespace(job_id=k, cwd=str(tmp_path), survivors=[]) for k in ('a', 'b', 'foreign')}
        called = []
        async def cancel(jid):
            called.append(jid)
            if jid == 'a':
                raise RuntimeError('first cancel failed')
        runner = SimpleNamespace(jobs=jobs, cancel=cancel)
        with pytest.raises(RuntimeError, match='directory retained'):
            await cleanup(runner, None, jobs['a'], str(tmp_path), {'a', 'b'})
        assert called == ['a', 'b']
        assert tmp_path.exists()
    asyncio.run(run())


@pytest.mark.parametrize('blocked', ['spawn', 'cancel', 'monitor'])
def test_probe_cleanup_timeout_is_bounded_and_retains_dir(tmp_path, monkeypatch, blocked):
    async def run():
        from plangraph.spawner import probe_cleanup
        monkeypatch.setattr(probe_cleanup, 'WAIT_S', .01, raising=False)
        job = SimpleNamespace(job_id='j', survivors=[])
        pending = asyncio.create_task(asyncio.Event().wait())
        async def cancel(jid):
            if blocked == 'cancel':
                await asyncio.Event().wait()
        runner = SimpleNamespace(jobs={'j': job}, cancel=cancel,
                                 _tasks={'j': pending} if blocked == 'monitor' else {})
        try:
            with pytest.raises(RuntimeError, match='directory retained'):
                await asyncio.wait_for(probe_cleanup.cleanup(
                    runner, pending if blocked == 'spawn' else None,
                    None if blocked == 'spawn' else job, str(tmp_path)), .3)
            assert tmp_path.exists()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(run())


def test_probe_cleanup_does_not_claim_same_cwd(tmp_path):
    async def run():
        from plangraph.spawner.probe_cleanup import cleanup
        async def cancel(jid):
            pytest.fail('unowned job cancelled')
        runner = SimpleNamespace(jobs={'other': SimpleNamespace(job_id='other', cwd=str(tmp_path))}, cancel=cancel)
        await cleanup(runner, None, None, str(tmp_path))
        assert not tmp_path.exists()
    asyncio.run(run())


def test_cancel_recorded_preserves_completed(tmp_path):
    async def run():
        service = SpawnService(SpawnerRunner(), tmp_path)
        service._save(dict(run_id='run-finished', status='completed', exit_code=0))
        out = await service.cancel('run-finished')
        assert out['status'] == 'completed' and out['exit_code'] == 0
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
