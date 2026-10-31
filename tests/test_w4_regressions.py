"""W4 (SPAWNER-STALL-WATCHDOG): run-record and wait defects in the dispatch loop.

Terminal-reason and quota-key policy live in test_spawner_exit_outcome.py and
test_leg_liveness.py. This module pins the run record end to end: every
per-attempt job is released from _running_records (Q13/M8), a cancelled run is
terminal with reason 'cancelled' (Q14), a watchdog stall/timeout records its
attempt row with the leg id (Q15/M16), and wait(timeout=None) terminates once
the dispatcher/supervisor is gone (Q21/M14).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid

import pytest

from plangraph.spawner.leg_state import LegStateStore
from plangraph.spawner.runner import SpawnerRunner, spawn_service


@pytest.fixture(autouse=True)
def _gateway_url(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_GATEWAY_URL', 'http://127.0.0.1:9/v1')   # hermetic: I6


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


def _open_catalog(monkeypatch):
    """Quota fallback independent of the host catalog's admission state."""
    from plangraph.spawner import resolver
    rows = resolver.get_catalog_models()
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        dict(row, available=1, active=True) for row in rows])


def test_running_records_release_every_attempt_job(tmp_path, monkeypatch):
    """Q13/M8: a run that fell through releases every attempt's job, not just the
    record's last job_id (the leak kept saving a stale record on every sample)."""
    from test_spawner_exit_outcome import QUOTA
    _open_catalog(monkeypatch)
    calls = []

    async def factory(*args, **kwargs):
        code = ('import sys; print(' + repr(QUOTA) + ', file=sys.stderr); sys.exit(3)'
                if not calls else 'print("done")')
        calls.append(code)
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)

    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        runner = SpawnerRunner(process_factory=factory, default_cwd=str(tmp_path))
        service = spawn_service(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=None)
        assert record['status'] == 'completed', record
        assert len(runner.jobs) == 2, sorted(runner.jobs)   # quota fallthrough launched leg 2
        assert service._running_records == {}
    asyncio.run(run())


def test_cancelled_run_stays_cancelled_and_never_falls_through(tmp_path, monkeypatch):
    """Q14: cancel is terminal. The record ends status='cancelled', reason
    'cancelled' -- never 'failed' carrying the earlier fallthrough leg's reason."""
    from test_spawner_exit_outcome import QUOTA
    _open_catalog(monkeypatch)
    calls = []

    async def factory(*args, **kwargs):
        code = ('import sys; print(' + repr(QUOTA) + ', file=sys.stderr); sys.exit(3)'
                if not calls else 'import time; time.sleep(30)')
        calls.append(code)
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code, **kwargs)

    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        runner = SpawnerRunner(process_factory=factory, default_cwd=str(tmp_path))
        service = spawn_service(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0.05)
        for _ in range(200):
            if len(runner.jobs) >= 2:
                break
            await asyncio.sleep(0.05)
        assert len(runner.jobs) == 2, sorted(runner.jobs)   # leg 1 fell through on quota evidence
        real = runner._reap_process

        async def settle(job_id, proc):
            await real(job_id, proc)
            for _ in range(10):
                await asyncio.sleep(0.05)   # the monitor settles inside the reap window

        runner._reap_process = settle
        job_id = service.result(record['run_id'])['job_id']
        await asyncio.wait_for(runner.cancel(job_id), 10)
        await asyncio.wait_for(service.tasks[record['run_id']], 10)
        final = service.result(record['run_id'])
        assert final['status'] == 'cancelled', final
        assert final['reason'] == 'cancelled', final
        assert len(runner.jobs) == 2, sorted(runner.jobs)   # no leg launched after the cancel
    asyncio.run(run())


def test_watchdog_timed_out_leg_records_its_attempt_row(tmp_path, monkeypatch):
    """Q15/M16: a watchdog stall/timeout ends the run AND records the attempt row
    with the leg id and the served model/family -- the launched leg must not be
    listed as 'skipped, run_terminated_before_leg'."""
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '0.3')
    monkeypatch.setenv('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL', '0.05')

    async def factory(*args, **kwargs):
        return await asyncio.create_subprocess_exec(
            sys.executable, '-c', 'import time; time.sleep(30)', **kwargs)

    async def run():
        runner = SpawnerRunner(process_factory=factory, default_cwd=str(tmp_path))
        service = spawn_service(runner, tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=None)
        assert record['status'] == 'timeout', record
        rows = [a for a in record['attempts'] if a.get('status') in ('stalled', 'timeout')]
        assert len(rows) == 1, record['attempts']
        assert rows[0]['leg']['id'] == record['plan']['legs'][0]['id']
        assert rows[0]['served_model'] and rows[0]['served_family']
    asyncio.run(run())


def test_wait_returns_when_the_supervisor_is_gone(tmp_path):
    """Q21/M14: wait(timeout=None) must terminate once the dispatcher that owned
    the run is dead; the record may not stay 'running' forever."""
    async def run():
        service = spawn_service(state=tmp_path)
        run_id = 'run-' + uuid.uuid4().hex
        log = tmp_path / ('supervisor-' + uuid.uuid4().hex + '.stderr.log')
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        (tmp_path / (log.name + '.pid')).write_text(str(dead.pid))
        service._save(dict(run_id=run_id, status='running', started=time.time(), ended=None,
                           attempts=[], job_id='j', pid=None, supervisor_stderr_path=str(log)))
        final = await asyncio.wait_for(service.wait(run_id), 5)
        assert final['status'] != 'running' and final['ended'] is not None, final
        assert 'orphan' in (str(final.get('error', '')) + str(final.get('reason', ''))), final
    asyncio.run(run())
