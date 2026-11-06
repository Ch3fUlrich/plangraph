"""Cancellation preserves real outcomes, but dry runs remain cancellable."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('status,code', [
    ('dry_run', 0), ('completed', 0), ('failed', 7),
    ('stalled', 8), ('timeout', 9),
])
def test_cancel_outcomes(tmp_path, monkeypatch, status, code):
    async def run():
        runner = SpawnerRunner()
        job = SimpleNamespace(status=status, exit_code=code, survivors=[], events=[])
        runner.jobs['j'] = job
        expected = 'cancelled' if status == 'dry_run' else status
        expected_code = 1 if status == 'dry_run' else code
        assert (await runner.cancel('j'))['status'] == expected
        assert job.exit_code == expected_code
        service = SpawnService(runner, tmp_path)

        async def no_kill(*args):
            pytest.fail('non-running records must not kill a recorded pid')

        monkeypatch.setattr(runner, 'kill_recorded', no_kill)
        service._save(dict(run_id='run-outcome', status=status, exit_code=code, pid=12345))
        out = await service.cancel('run-outcome')
        assert (out['status'], out['exit_code']) == (expected, expected_code)
        assert service.result('run-outcome') == out
    asyncio.run(run())


@pytest.mark.parametrize('status', ['completed', 'failed', 'stalled', 'timeout'])
def test_terminal_cancel_preserves_newer_disk_record(tmp_path, monkeypatch, status):
    service = SpawnService(SpawnerRunner(), tmp_path)
    service._save(dict(run_id='run-race', status=status, exit_code=7))
    stale = service.result('run-race')
    newer = dict(stale, finished_detail='published by concurrent finisher')
    service._save(newer)
    read = service.result
    monkeypatch.setattr(service, 'result', lambda *args, **kwargs: stale)

    assert asyncio.run(service.cancel('run-race')) == stale
    assert read('run-race') == newer


def test_recorded_survivors_without_runner_job_are_kill_incomplete(tmp_path, monkeypatch):
    runner = SpawnerRunner()
    service = SpawnService(runner, tmp_path)
    service._save(dict(run_id='run-survivor', job_id='missing', status='failed',
                       exit_code=7, pid=12345, survivors=[12345]))

    async def no_kill(*args):
        pytest.fail('terminal records must not enter the recorded-pid kill branch')

    monkeypatch.setattr(runner, 'kill_recorded', no_kill)
    assert 'missing' not in runner.jobs
    out = asyncio.run(service.cancel('run-survivor'))
    assert (out['status'], out['reason'], out['survivors']) == (
        'failed', 'kill_incomplete', [12345])
    assert out['events'][-1] == dict(event='kill_incomplete', survivors=[12345])
    assert service.result('run-survivor') == out
