"""Runner monitor pins: cleanup, spend capture and failure summary (D-3258/D-3264).

Split out of tests/test_spawn_cli.py (file-size gate): these exercise the
runner's per-job lifecycle -- the monitor's cleanup on a dropped job record,
spend capture/ledger writes off the event loop, and the failure summary that
reads attempts AND skipped.
"""
import asyncio
import os
import sys

from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.runner import spawn_service
import pytest


@pytest.fixture(autouse=True)
def _gateway_url(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'http://127.0.0.1:9/v1')  # hermetic: I6 refuses a gateway leg with no base URL
    # Legacy quota/fallthrough assertions deliberately exercise non-credit routes.
    from plangraph.planner import routing
    original = routing.plan_spawn
    monkeypatch.setattr(routing, 'plan_spawn', lambda *a, **kw:
                        original(*a, **dict(kw, legs=tuple(l for l in kw.get('legs', routing.DEFAULT_LEGS)
                                                          if l.tier != 'credit'))))


@pytest.fixture(autouse=True)
def fake_host_binaries(tmp_path, monkeypatch):
    """Keep real probes (including worker processes) off the host installation."""
    from plangraph.planner.legs import DEFAULT_LEGS
    directory = tmp_path / 'bin'
    directory.mkdir()
    for name in ('opencode', 'agy'):
        models = '\n'.join(leg.model for leg in DEFAULT_LEGS if leg.harness == name)
        binary = directory / name
        binary.write_text('#!' + sys.executable + '\nimport sys\n'
                          'if sys.argv[1:] == ["models"]:\n'
                          '    print(' + repr(models) + ')\n'
                          'else:\n    raise SystemExit("unexpected real harness launch")\n')
        binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(directory) + os.pathsep + os.environ['PATH'])


# ---------------------------------------------------------------------------
# D-3258/D-3259/D-3264 (r3): monitor cleanup and failure-summary pins.
# ---------------------------------------------------------------------------
def _quiet_host_probe(monkeypatch):
    from plangraph.spawner import resolver

    async def probe(*args, **kwargs):
        return None
    monkeypatch.setattr(resolver, 'host_skip_reason_async', probe)
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)


def _fake_process_factory():
    from types import SimpleNamespace

    async def factory(*args, **kwargs):
        async def communicate(*a, **k):
            return b'ok', b''
        return SimpleNamespace(pid=123, returncode=0, communicate=communicate)
    return factory


def test_monitor_cleanup_survives_a_dropped_job(tmp_path, monkeypatch):
    """The cancel path may drop the job record mid-run (D-3258): the monitor's
    cleanup must still run and leak no per-job state."""
    from plangraph.planner.legs import DEFAULT_LEGS
    _quiet_host_probe(monkeypatch)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=_fake_process_factory())

    async def run():
        job = await runner.spawn('p', model_or_combo=DEFAULT_LEGS[0].model, client='agy',
                                cwd=str(tmp_path), extra_env={}, wait_seconds=0)
        runner.jobs.pop(job.job_id)      # the cancel path drops the record
        await runner._tasks[job.job_id]  # must finish without raising
        return job
    job = asyncio.run(run())
    for state in ('_child_envs', '_processes', '_adapter_tokens', '_nonces'):
        assert job.job_id not in getattr(runner, state), state


def test_capture_spend_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    """Spend capture may block (store retries); it never runs on the loop."""
    import threading
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner import credit
    _quiet_host_probe(monkeypatch)
    seen = {}
    monkeypatch.setattr(credit, 'capture_spend',
                        lambda job, env, **kw: seen.setdefault('tid', threading.get_ident()))
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=_fake_process_factory())

    async def run():
        job = await runner.spawn('p', model_or_combo=DEFAULT_LEGS[0].model, client='agy',
                                cwd=str(tmp_path), extra_env={}, wait_seconds=0)
        await runner._tasks[job.job_id]
    asyncio.run(run())
    assert 'tid' in seen, 'the monitor must attempt a capture'
    assert seen['tid'] != threading.get_ident(), 'spend capture blocked the event loop'


def test_record_spend_runs_off_the_event_loop_thread(tmp_path, monkeypatch):
    """Ledger writes may busy-wait on the lock (D-3264): never on the loop."""
    import threading
    from types import SimpleNamespace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import credit
    from plangraph.spawner.runner import JobInfo
    leg = next(leg for leg in DEFAULT_LEGS if leg.tier == 'credit')
    plan = SpawnPlan((leg,), (), 'test', (), ())
    seen = {}
    monkeypatch.setattr(credit, 'record_spend',
                        lambda *a, **kw: seen.setdefault('tid', threading.get_ident()))
    async def spawn(**kw):
        return JobInfo('j1', 'completed', 'x', kw['model_or_combo'])
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = spawn_service(runner=runner, state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    asyncio.run(asyncio.wait_for(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None), 5))
    assert 'tid' in seen, 'a credit leg must record its spend'
    assert seen['tid'] != threading.get_ident(), 'the ledger write blocked the event loop'


def test_failure_summary_reads_attempts_and_skipped(tmp_path, monkeypatch):
    """A failed run's error names every skip reason -- attempts AND skipped --
    and is never empty (D-3258/D-3264)."""
    from types import SimpleNamespace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import JobInfo
    _quiet_host_probe(monkeypatch)
    legs = tuple(leg for leg in DEFAULT_LEGS if leg.tier != 'credit')[:2]
    plan = SpawnPlan(legs, ({'leg': 'agy', 'reason': 'harness_missing: agy'},), 'test', (), ())
    ids = iter(('j1', 'j2'))

    async def spawn(**kw):
        return JobInfo(next(ids), 'failed', 'x', kw['model_or_combo'], exit_code=1, stderr='boom')
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={},
                             provider_responses={'j1': {'status': 429}, 'j2': {'status': 429}})
    service = spawn_service(runner=runner, state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    result = asyncio.run(asyncio.wait_for(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None), 5))
    assert (result['status'], result['exit_code']) == ('failed', 1)
    assert result['error'], 'a failed run never has an empty error'
    assert 'boom' in result['error'], result['error']
    assert 'harness_missing: agy' in result['error'], result['error']
