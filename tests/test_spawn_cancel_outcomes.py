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
def test_cancel_outcomes(tmp_path, status, code):
    async def run():
        runner = SpawnerRunner()
        job = SimpleNamespace(status=status, exit_code=code, survivors=[], events=[])
        runner.jobs['j'] = job
        expected = 'cancelled' if status == 'dry_run' else status
        expected_code = 1 if status == 'dry_run' else code
        assert (await runner.cancel('j'))['status'] == expected
        assert job.exit_code == expected_code
        service = SpawnService(runner, tmp_path)
        service._save(dict(run_id='run-outcome', status=status, exit_code=code))
        out = await service.cancel('run-outcome')
        assert (out['status'], out['exit_code']) == (expected, expected_code)
        assert service.result('run-outcome') == out
    asyncio.run(run())
