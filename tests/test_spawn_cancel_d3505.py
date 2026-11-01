import asyncio
import sys
from plangraph.spawner.leg_state import LegStateStore
from plangraph.spawner.runner import SpawnerRunner, spawn_service


def test_d3505_cross_process_cancel_stops_the_leg_loop(tmp_path):
    async def run():
        store, runner = LegStateStore(tmp_path / 'legs.sqlite'), SpawnerRunner()
        async def factory(*args, **kwargs):
            job_id = next(reversed(runner.jobs))
            runner.provider_response(job_id, runner.jobs[job_id].model_or_combo, 429, 'quota', {}, store=store,
                                     source='gateway_adapter', adapter_token=runner._adapter_tokens[job_id])
            for marker_dir in (tmp_path / 'runs').iterdir():   # `cli cancel` from another process
                (marker_dir / 'cancelled').touch()
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'print(1)', **kwargs)
        runner.process_factory = factory   # the quota stop alone would fall through to the next leg
        service = spawn_service(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=None)
        assert len(runner.jobs) == 1 and record['status'] == 'cancelled'
        (tmp_path / 'runs' / record['run_id'] / 'cancelled').unlink()
        await service.cancel(record['run_id'])   # the CLI/MCP cancel writes the marker itself
        assert (tmp_path / 'runs' / record['run_id'] / 'cancelled').exists()
    asyncio.run(run())
