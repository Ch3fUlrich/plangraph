import asyncio
import json

from plangraph.spawner.cli import main
from plangraph.spawner.service import SpawnService
from plangraph.spawner.runner import SpawnerRunner, kill_process_tree
from plangraph.spawner.leg_state import LegStateStore
import pytest
import sys
import os


def test_route_and_refusal(tmp_path, capsys):
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1000}', '--role', 'implement', '--json']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['first_leg']['model'] == 'omniroute/cx-gpt-6.1-sol'
    assert set(plan) == {'legs', 'first_leg', 'skipped', 'p_source', 'review_seats', 'reasons', 'error'}
    assert main(['--state', str(tmp_path), 'spawn', 'task']) == 2
    assert 'spawn by card' in capsys.readouterr().err


def test_records_and_review(tmp_path):
    async def run():
        service = SpawnService(state=tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', dry_run=True)
        assert service.result(record['run_id'])['status'] == 'dry_run'
        assert service.ps()['runs'][0]['run_id'] == record['run_id']
        plan = service.route({'estimate_tokens': 1000}, role='review', review_of=record['run_id'])
        assert all(l.family != record['writer_family'] for l in plan.legs)
    asyncio.run(run())


@pytest.mark.parametrize('provider_stop', [True, False])
def test_transport_only_fallthrough(tmp_path, provider_stop):
    async def run():
        store = LegStateStore(tmp_path / 'legs.sqlite')
        runner = SpawnerRunner()
        async def factory(*args, **kwargs):
            if provider_stop and len(runner.jobs) == 1:
                job_id = next(iter(runner.jobs))
                runner.provider_response(job_id, 'omniroute/cx-gpt-6.1-sol', 429,
                    'provider quota exhausted', {}, store=store)
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'print("429")', **kwargs)
        runner.process_factory = factory
        service = SpawnService(runner, tmp_path, store)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=None)
        assert len(record['attempts']) == (2 if provider_stop else 1)
        assert store.leg_status('omniroute/cx-gpt-6.1-sol')['state'] == ('limited' if provider_stop else 'ok')
        if provider_stop:
            assert service.route({'estimate_tokens': 1000}).first_leg.id != 'cx-gpt-6.1-sol'
    asyncio.run(run())


def test_windows_killer():
    async def run():
        calls = []
        class Process:
            async def communicate(self):
                return b'', b''
        async def factory(*args, **kwargs):
            calls.append(args)
            return Process()
        await kill_process_tree(123, platform='nt', command_factory=factory)
        assert calls == [('taskkill', '/PID', '123', '/T', '/F')]
    asyncio.run(run())


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process group integration')
def test_cancel_real_child(tmp_path):
    async def run():
        async def factory(*args, **kwargs):
            return await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', **kwargs)
        runner = SpawnerRunner(process_factory=factory)
        service = SpawnService(runner, tmp_path)
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='task', wait_seconds=0.05)
        process = runner._processes[record['job_id']]
        assert (await service.cancel(record['run_id']))['status'] == 'cancelled'
        await asyncio.wait_for(process.wait(), 3)
        assert process.returncode != 0
    asyncio.run(run())


def test_override_and_unknown_review(tmp_path):
    async def run():
        service = SpawnService(state=tmp_path)
        with pytest.raises(ValueError, match='unknown run'):
            await service.spawn(card={'estimate_tokens': 1000}, review_of='run-unknown', dry_run=True)
        await service.spawn(model='mimo/mimo-v2.6-pro', override_reason='explicit test override', dry_run=True)
        assert json.loads((tmp_path / 'spawn-overrides.jsonl').read_text())['reason'] == 'explicit test override'
        with pytest.raises(ValueError):
            await service.spawn(model='omniroute/mimo-v2.6-pro', override_reason='explicit test override', dry_run=True)
    asyncio.run(run())
