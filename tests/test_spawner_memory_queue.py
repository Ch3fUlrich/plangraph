"""Hermetic admission, bounded memory-recovery queue, and queued cancellation."""
import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
        gate, clock = asyncio.Event(), [0.0]
        monkeypatch.setattr(q, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
        async def wait():
            paused.set()
            await gate.wait()
            if tick == 'timer': clock[0] = 5.0
            raise asyncio.TimeoutError
        async def launch(*, _queued_record, **options):
            launched.set()
            await finish.wait()
        spawn = AsyncMock(side_effect=launch)
        monkeypatch.setattr(host.service, '_spawn', spawn)
        record = host.service.admit_spawn({'prompt': 'task'})
        monkeypatch.setattr(host.service._queued[record['run_id']], 'wait', wait)
        assert record['status'] == 'queued' and record['reason'] == 'memory_recovery'
        assert record['queue_bound_s'] == 900 and record['queued_until'] == 1900
        await asyncio.wait_for(paused.wait(), 1)
        assert not launched.is_set() and host.service.result(record['run_id'])['status'] == 'queued'
        host.memory[0] = 5048
        if tick == 'timer':
            gate.set()  # real queue_pause observes its monotonic deadline
        else:
            SpawnService(state=host.service.state)._save(dict(run_id='run-exit', status='completed', ended=1001, exit_code=0))
            gate.set()  # only queue-wake changed; the local event and clock did not
        await asyncio.wait_for(launched.wait(), 1)
        claimed = spawn.call_args.kwargs['_queued_record']
        assert (claimed['run_id'], claimed['queued_until']) == (record['run_id'], 1900)
        assert spawn.call_args.kwargs['wait_seconds'] is None
        assert not host.service._queued
        finish.set()
        await asyncio.gather(*host.service.tasks.values())
    asyncio.run(exercise())


def test_queue_timeout_is_bounded(host, monkeypatch):
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true,"queue_bound_s":30}')
        spawn = AsyncMock()
        monkeypatch.setattr(host.service, '_spawn', spawn)
        record = host.service.admit_spawn({'prompt': 'task'})
        assert record['queued_until'] == 1030 and record['queue_bound_s'] == 30
        await asyncio.sleep(0)
        assert host.service.result(record['run_id'])['status'] == 'queued'
        host.clock[0] = 1030
        host.memory[0] = 9000  # deadline wins even when memory returned
        host.service._queued[record['run_id']].set()
        await asyncio.wait_for(host.service.tasks[record['run_id']], 1)
        final = host.service.result(record['run_id'], expire=False)
        assert final['status'] == 'failed' and final['reason'] == 'queue_timeout'
        assert final['ended'] == 1030 and final['exit_code'] != 0
        assert not host.service._queued
        spawn.assert_not_called()
    asyncio.run(exercise())
    host.service._save(dict(run_id='run-lost', status='queued', queued_until=1029, queue_bound_s=30))
    assert next(r for r in host.service.ps()['runs'] if r['run_id'] == 'run-lost')['reason'] == 'queue_timeout'
    assert host.service.result('run-lost')['status'] == 'failed'
    host.service._save(dict(run_id='run-legacy', status='queued', exit_code=5))
    assert host.service.result('run-legacy')['reason'] == 'queue_timeout'


def test_cancel_queued_returns_zero(host, capsys, monkeypatch):
    # Also covers old queued records with no resumable payload or supervisor.
    host.service._save(dict(run_id='run-old', status='queued', exit_code=5))
    assert cli.main(['cancel', 'run-old']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'cancelled'
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        spawn = AsyncMock()
        monkeypatch.setattr(host.service, '_spawn', spawn)
        record = host.service.admit_spawn({'prompt': 'task'})
        other = SpawnService(state=host.service.state)  # independent CLI process
        result = await other.cancel(record['run_id'])
        assert result['status'] == 'cancelled' and result['exit_code'] == 0
        host.service._queued[record['run_id']].set()
        host.memory[0] = 9000
        await asyncio.wait_for(host.service.tasks[record['run_id']], 1)
        assert not host.service._queued
        assert host.service.tasks[record['run_id']].done()
        spawn.assert_not_called()
    asyncio.run(exercise())


def dispatch_service(host, monkeypatch, dispatch):
    from registry_route_util import _Runner
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
    return service


@pytest.mark.parametrize('dispatch', [False, True])
def test_queue_preserves_identity_through_real_dispatch(host, monkeypatch, dispatch):
    from registry_route_util import CARD
    service = dispatch_service(host, monkeypatch, dispatch)
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        record = await service.spawn(card=CARD, prompt='review', role='review', writer_families=['gpt'])
        assert record['status'] == 'queued'
        await asyncio.sleep(0)
        assert service.result(record['run_id'])['status'] == 'queued' and not service.runner.calls
        host.memory[0] = 9000
        service._queued[record['run_id']].set()
        await asyncio.wait_for(service.tasks[record['run_id']], 10)
        final = service.result(record['run_id'])
        assert service.runner.calls and final['status'] != 'queued' and final['ended'] is not None
        assert final['queued_until'] == record['queued_until']
        assert len(service.ps()['runs']) == 1
        assert not service._queued
    asyncio.run(exercise())


def test_queue_reads_do_not_publish_or_lock(host, monkeypatch):
    host.service._save(dict(run_id='run-expired', status='queued', queued_until=999))
    raw = host.service._path('run-expired').read_bytes()
    save = Mock(side_effect=AssertionError('read published a record'))
    monkeypatch.setattr(host.service, '_save', save)
    monkeypatch.setattr(q, 'inbox_lock', Mock(side_effect=AssertionError('read acquired a lock')))
    assert host.service.result('run-expired')['reason'] == 'queue_timeout'
    assert host.service.ps()['runs'][0]['reason'] == 'queue_timeout'
    assert host.service._path('run-expired').read_bytes() == raw
    save.assert_not_called()


@pytest.mark.parametrize('stage', ['queued', 'claimed'])
@pytest.mark.parametrize('terminal', [None, 'cancelled', 'completed', 'running'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_queue_failure_preserves_identity_and_terminal_outcomes(host, monkeypatch, terminal, interrupted, stage):
    async def launch(*args, _queued_record=None, **kw):
        _queued_record = _queued_record or host.service.result(next(iter(host.service.tasks)))
        fresh = dict(_queued_record, plan={'legs': []}, job_id='invented-job', card_hash='invented-hash')
        if terminal: fresh.update(status=terminal, exit_code=None if terminal == 'running' else 0,
                                  ended=None if terminal == 'running' else 1001)
        host.service._save(fresh)
        raise asyncio.CancelledError if interrupted else RuntimeError('launch detail retained')
    monkeypatch.setattr(host.service, '_spawn', launch)
    if stage == 'queued': monkeypatch.setattr(q, 'queue_pause', launch)
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        record = host.service.admit_spawn({'prompt': 'task'})
        if stage == 'claimed': host.memory[0] = 9000
        await asyncio.gather(host.service.tasks[record['run_id']], return_exceptions=True)
        final = host.service.result(record['run_id'])
        assert (final['plan'], final['job_id'], final['card_hash']) == ({'legs': []}, 'invented-job', 'invented-hash')
        assert final['status'] == (terminal or 'failed')
        assert final['exit_code'] == (None if terminal == 'running' else 0 if terminal else 1)
        if not terminal and not interrupted:
            assert final['reason'] == 'queue_start_failed' and final['error'] == 'launch detail retained'
    asyncio.run(exercise())


@pytest.mark.parametrize('dispatch', [False, True])
def test_cancel_claimed_before_execute_returns_zero(host, monkeypatch, dispatch):
    from registry_route_util import CARD
    service = dispatch_service(host, monkeypatch, dispatch)
    entered, release = threading.Event(), threading.Event()
    def annotate(*a):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', annotate)
    async def exercise():
        host.config.write_text('{"queue_memory_wait":true}')
        record = await service.spawn(card=CARD, prompt='review', role='review', writer_families=['gpt'])
        host.memory[0] = 9000
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            assert service.result(record['run_id'])['status'] == 'queue_claimed'
            result = await SpawnService(state=service.state).cancel(record['run_id'])
        finally:
            release.set()
        await service.tasks[record['run_id']]
        assert result['status'] == 'cancelled' and result['exit_code'] == 0
        assert service.result(record['run_id'])['status'] == 'cancelled' and not service.runner.calls
    asyncio.run(exercise())


def test_queue_lock_contention_is_bounded_and_yields(host):
    held, release = threading.Event(), threading.Event()
    def hold():
        with q.inbox_lock(host.service._path('run-locked')):
            held.set()
            release.wait(1)
    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(1)
    async def exercise():
        pulse = asyncio.create_task(asyncio.sleep(.001))
        with pytest.raises(TimeoutError):
            async with q.queue_lock(host.service, 'run-locked', timeout=.02):
                pytest.fail('contended lock acquired')
        assert pulse.done()
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        thread.join()
