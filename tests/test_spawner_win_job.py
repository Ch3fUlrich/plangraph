"""Windows Job Object lifecycle through the real spawn/monitor path (simulated nt)."""
import asyncio
import tempfile

import pytest
from fake_pid import FAKE_PID


class FakeWinJob:
    """Fake win_job_api recording calls; can fail assignment (F3 hermetic tests)."""

    def __init__(self, fail_assign=False):
        self.calls, self.fail_assign = [], fail_assign

    def pids(self, handle):   # the live job is the ownership proof; empty once terminated
        return []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args[-1] if args else None))
            if name == 'assign' and self.fail_assign:
                raise RuntimeError('assign refused')
            return 'job-handle' if name == 'create_job' else None
        return call


class FakeChild:
    def __init__(self, pid=FAKE_PID, hang=False):
        self.pid, self.returncode = pid, 0
        self._unblock = asyncio.Event() if hang else None

    async def communicate(self, input=None):
        if self._unblock is not None:
            await self._unblock.wait()
        return b'stdout', b'stderr'


def _run_nt(monkeypatch, api, proc, timeout=None):
    """One job through the real spawn/monitor path on simulated nt (hook, never os.name)."""
    from plangraph.spawner import runner as runner_mod
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner import proc_kill
    monkeypatch.setattr(runner_mod, '_is_nt', lambda: True)
    monkeypatch.setattr(proc_kill, 'escapees', lambda root, job_pids, **k: {})   # hermetic: no Toolhelp scan; root stays unproven
    if timeout:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', timeout)
    killed = []

    async def main():
        async def factory(*a, **kw):
            return proc

        async def tree_killer(pid):  # the taskkill fallback path
            killed.append(pid)
            if proc._unblock is not None:
                proc._unblock.set()

        r = SpawnerRunner(default_cwd=directory, process_factory=factory, tree_killer=tree_killer)
        r.win_job_api = api
        job = await r.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        if job.job_id in r._tasks:   # a refused run has no monitor
            await r._tasks[job.job_id]
        return r, job, killed

    with tempfile.TemporaryDirectory() as directory:
        return asyncio.run(main())


@pytest.mark.parametrize('timeout,hang', [(None, False), ('0.05', True)])
def test_win_job_assigned_on_launch_and_terminated_once_at_reap(monkeypatch, timeout, hang):
    """assign(child pid) once at launch; terminate+close exactly once at reap on
    every exit path (normal, timeout); the job is popped (F3)."""
    api = FakeWinJob()
    runner, job, killed = _run_nt(monkeypatch, api, FakeChild(hang=hang), timeout=timeout)
    assert job.exit_code == (124 if hang else 0)
    lifecycle = [call for call in api.calls if call[0] != 'cpu_seconds']
    assert lifecycle == [('create_job', None), ('assign', FAKE_PID),
                         ('terminate', 'job-handle'), ('close', 'job-handle')]
    # CPU sampling is read-only, but must use the assigned, still-open job.
    for index, (operation, handle) in enumerate(api.calls):
        if operation == 'cpu_seconds':
            assert handle == 'job-handle'
            assert 1 < index < api.calls.index(('terminate', 'job-handle'))
    assert killed == [FAKE_PID] and runner._win_jobs == {}  # taskkill fallback; job popped


def test_win_job_assign_failure_refuses_run(monkeypatch):
    api = FakeWinJob(fail_assign=True)
    runner, job, killed = _run_nt(monkeypatch, api, FakeChild())
    assert job.status == 'failed' and job.survivors == [FAKE_PID]   # fail closed: no unguarded run
    assert {'event': 'win_job_unavailable'} in job.events   # r3 (V2 LOW): exact event restored
    assert [c[0] for c in api.calls] == ['create_job', 'assign', 'close']  # handle released
    assert killed == [] and runner._win_jobs == {}




def test_win_job_close_runs_when_terminate_raises(monkeypatch):
    class TerminateFails(FakeWinJob):
        def terminate(self, handle):
            self.calls.append(('terminate', handle))
            raise OSError('TerminateJobObject failed')
    api = TerminateFails()
    runner, job, killed = _run_nt(monkeypatch, api, FakeChild())
    assert ('close', 'job-handle') in api.calls  # handle never leaks
    assert {'event': 'reap_failed', 'error': 'OSError'} in job.events
    assert killed == [FAKE_PID] and runner._win_jobs == {}


def test_win_job_api_constructor_failure_refuses_run(monkeypatch):
    from plangraph.spawner import runner as runner_mod

    def no_kernel32():
        raise OSError('kernel32 unavailable')
    monkeypatch.setattr(runner_mod, 'WinJobApi', no_kernel32)
    runner, job, killed = _run_nt(monkeypatch, None, FakeChild())
    assert job.status == 'failed' and job.survivors == [FAKE_PID]
    assert {'event': 'win_job_unavailable'} in job.events   # r3 (V2 LOW): exact event restored
    assert killed == [] and runner._win_jobs == {}
