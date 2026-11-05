"""A cancel from another process must kill a run started on a named leg: record.pid_start proves its root."""
import asyncio
import sys

from plangraph.spawner.runner import SpawnerRunner
from plangraph.spawner.service import SpawnService


def test_cancel_from_a_second_service_kills_the_child(tmp_path):
    async def run():
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        service = SpawnService(runner, tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0)
        for _ in range(500):
            record = service.result(record['run_id'])
            if record.get('job_id') in runner._processes and record.get('pid'):
                break
            await asyncio.sleep(0.01)
        process = runner._processes[record['job_id']]
        assert record['pid_start']
        other = SpawnService(SpawnerRunner(), tmp_path)   # no shared memory, like `cli cancel`
        cancelled = await other.cancel(record['run_id'])
        assert cancelled['status'] == 'cancelled' and not cancelled.get('survivors')
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode != 0
    asyncio.run(run())
