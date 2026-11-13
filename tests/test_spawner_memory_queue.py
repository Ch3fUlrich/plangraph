"""Hermetic admission, bounded memory-recovery queue, and queued cancellation."""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plangraph.spawner import cancel_recorded as q, cli, overflow
from plangraph.spawner.service import SpawnService


@pytest.fixture
def host(tmp_path, monkeypatch):
    config = tmp_path / 'overflow.json'
    config.write_text('{}')
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setenv('PLANGRAPH_HOST', 'invented-local')
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'local_limits', lambda *a: (2048, 3000))
    memory, clock = [5047], [1000.0]
    monkeypatch.setattr(overflow, 'available_memory_mb', lambda: memory[0])
    monkeypatch.setattr(q, 'queue_now', lambda: clock[0], raising=False)
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {})
    remote = Mock(side_effect=AssertionError('no eligible placement'))
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    service = SpawnService(state=tmp_path / 'state')
    service.runner.default_cwd = str(tmp_path)
    service.route = Mock()
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    return SimpleNamespace(service=service, memory=memory, clock=clock, config=config, remote=remote)


def test_memory_floor_fails_immediately_or_places(host, monkeypatch, capsys):
    class Input(io.StringIO):
        def close(self): pass
    class Supervisor:
        def __init__(self, *a, **kw):
            self.stdin, self.stdout = Input(), self
        def readline(self):
            payload = json.loads(self.stdin.getvalue())
            payload.pop('state')
            return json.dumps(asyncio.run(host.service.spawn(**payload)))
        def close(self): pass
    monkeypatch.setattr(cli, '_start_supervisor', Supervisor)
    assert cli.main(['spawn', '--card', '{"task":"test"}', 'Implement this change.']) == 2
    captured = capsys.readouterr()
    record = json.loads(captured.out)
    assert record['status'] == 'refused' and record['ended'] is not None
    assert record['reason'] == 'host_memory_floor'
    assert (record['mem_available_mb'], record['floor_mb'], record['role_mb']) == (5047, 3000, 2048)
    assert all(text in captured.err for text in ('host_memory_floor', '5047', '3000', '2048'))
    assert host.service.result(record['run_id']) == record
    host.remote.assert_not_called()
    host.config.write_text(json.dumps({'public_repos': [host.service.runner.default_cwd],
                                      'hosts': {'other': {}}}))
    monkeypatch.setattr(overflow, 'ready_records', lambda *a: {'other': dict(
        ssh='fake', ok=True, fresh=True, live_runs=0, mem_available_mb=7000,
        role_mb=2048, floor_mb=3000)})
    host.remote.configure_mock(side_effect=None, return_value=dict(run_id='run-remote', status='running'))
    assert host.service.admit_spawn({})['host'] == 'other'


@pytest.mark.parametrize('tick', ['timer', 'run_exit'])
def test_queued_run_starts_when_memory_returns(host, monkeypatch, tick):
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        paused, launched, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        timer = []
        async def pause(event, delay):
            timer.append((event, delay))
            paused.set()
            await event.wait()
        monkeypatch.setattr(q, 'queue_pause', pause, raising=False)
        async def launch(*, _queued_record, **options):
            _queued_record.update(status='running', started=host.clock[0], exit_code=None)
            host.service._save(_queued_record)
            launched.set()
            await finish.wait()
        monkeypatch.setattr(host.service, '_spawn', launch)
        record = host.service.admit_spawn({'prompt': 'task'})
        assert record['status'] == 'queued' and record['reason'] == 'memory_recovery'
        assert record['queue_bound_s'] == 900 and record['queued_until'] == 1900
        await asyncio.wait_for(paused.wait(), 1)
        assert not launched.is_set() and 0 < timer[0][1] <= 5
        host.memory[0] = 5048
        if tick == 'timer':
            timer[0][0].set()  # fake timer expiration, no wall-clock sleep
        else:
            host.service._save(dict(run_id='run-exit', status='completed', ended=1001, exit_code=0))
        await asyncio.wait_for(launched.wait(), 1)
        assert host.service.result(record['run_id'])['status'] == 'running'
        assert not host.service._queued
        finish.set()
        await asyncio.gather(*host.service.tasks.values())
    asyncio.run(exercise())


def test_queue_timeout_is_bounded(host):
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true,"queue_bound_s":30}')
        record = host.service.admit_spawn({'prompt': 'task'})
        assert record['queued_until'] == 1030 and record['queue_bound_s'] == 30
        host.clock[0] = 1030
        host.memory[0] = 9000  # deadline wins even when memory returned
        await asyncio.wait_for(host.service.tasks[record['run_id']], 1)
        final = host.service.result(record['run_id'])
        assert final['status'] == 'failed' and final['reason'] == 'queue_timeout'
        assert final['ended'] == 1030 and final['exit_code'] != 0
        assert not host.service._queued
    asyncio.run(exercise())
    host.service._save(dict(run_id='run-lost', status='queued', queued_until=1029, queue_bound_s=30))
    assert host.service.ps()['runs'][0]['reason'] == 'queue_timeout'
    assert host.service.result('run-lost')['status'] == 'failed'
    host.service._save(dict(run_id='run-legacy', status='queued', exit_code=5))
    assert host.service.result('run-legacy')['reason'] == 'queue_timeout'


def test_cancel_queued_returns_zero(host, capsys):
    # Also covers old queued records with no resumable payload or supervisor.
    host.service._save(dict(run_id='run-old', status='queued', exit_code=5))
    assert cli.main(['cancel', 'run-old']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'cancelled'
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        record = host.service.admit_spawn({'prompt': 'task'})
        other = SpawnService(state=host.service.state)  # independent CLI process
        result = await other.cancel(record['run_id'])
        assert result['status'] == 'cancelled' and result['exit_code'] == 0
        host.service._queued[record['run_id']].set()
        host.memory[0] = 9000
        await asyncio.wait_for(host.service.tasks[record['run_id']], 1)
        assert not host.service._queued
        assert host.service.tasks[record['run_id']].done()
    asyncio.run(exercise())


@pytest.mark.parametrize('dispatch', [False, True])
def test_queue_preserves_identity_through_real_dispatch(host, monkeypatch, dispatch):
    from registry_route_util import _Runner, CARD
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import spawn_service
    sink = SimpleNamespace(deliver=lambda *a: None)
    service = (spawn_service(runner=_Runner(), state=host.service.state, completion_sink=sink, keeper_sink=sink)
               if dispatch else SpawnService(runner=_Runner(), state=host.service.state))
    chosen = next(leg for leg in DEFAULT_LEGS if leg.family == 'qwen')
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan((chosen,), (), 'test', (), ()))
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'review')
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    async def identity(plan, **kw):
        return plan
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', identity)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', lambda *a: identity(None))
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        record = await service.spawn(card=CARD, prompt='review', role='review', writer_families=['gpt'])
        assert record['status'] == 'queued'
        host.memory[0] = 9000
        await asyncio.wait_for(service.tasks[record['run_id']], 10)
        final = service.result(record['run_id'])
        assert service.runner.calls and final['status'] != 'queued' and final['ended'] is not None
        assert final['queued_until'] == record['queued_until']
        assert len(service.ps()['runs']) == 1
        assert not service._queued
    asyncio.run(exercise())
