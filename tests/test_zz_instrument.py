import asyncio
import sys
import time

from plangraph.spawner.service import SpawnService
from plangraph.spawner.runner import SpawnerRunner


def test_instrument_cancel(tmp_path, capsys):
    async def run():
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        service = SpawnService(runner, tmp_path)
        real_spawn = runner.spawn

        async def timed_spawn(*a, **kw):
            t0 = time.monotonic()
            print(f"SPAWN-ENTER client={kw.get('client')} model={kw.get('model_or_combo')}", file=sys.stderr)
            try:
                job = await real_spawn(*a, **kw)
                print(f"SPAWN-EXIT {time.monotonic()-t0:.3f}s job={job.job_id}", file=sys.stderr)
                return job
            except Exception as exc:
                print(f"SPAWN-RAISE {time.monotonic()-t0:.3f}s {exc!r}", file=sys.stderr)
                raise
        runner.spawn = timed_spawn
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0.05)
        print("RECORD", 'job_id' in record, [ (a['leg']['id'], a['status'], a.get('skip_reason')) for a in record.get('attempts', []) ], file=sys.stderr)
        await asyncio.sleep(3)
        final = service.result(record['run_id'])
        print("FINAL", 'job_id' in final, [(a['leg']['id'], a['status'], a.get('skip_reason')) for a in final.get('attempts', [])], file=sys.stderr)
        await service.cancel(record['run_id'])
    asyncio.run(run())
