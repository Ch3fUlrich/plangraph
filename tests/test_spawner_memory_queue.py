"""Hermetic bounded memory admission and recovery regressions."""
import asyncio, json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from plangraph.spawner import cancel_recorded as q, overflow
from plangraph.spawner.service import SpawnService
@pytest.fixture
def host(tmp_path, monkeypatch):
    config = tmp_path / 'overflow.json'; config.write_text('{"queue_memory_wait":true,"queue_bound_s":30}')
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(config))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {}); monkeypatch.setattr(overflow, 'placement_policy', lambda *a: ('local', False))
    monkeypatch.setattr(overflow, 'local_limits', lambda *a: (2048, 3000)); monkeypatch.setattr(overflow, 'ready_records', lambda *a: {})
    memory, clock = [5047], [1000.]; monkeypatch.setattr(overflow, 'available_memory_mb', lambda: memory[0])
    monkeypatch.setattr(q, 'queue_now', lambda: clock[0], raising=False)
    return SimpleNamespace(service=SpawnService(state=tmp_path / 'state'), memory=memory, clock=clock, config=config)
@pytest.mark.parametrize('tick', ['timer', 'run_exit', 'read_error', 'lock_timeout', 'terminal_write', 'final_write'])
def test_queued_run_starts_when_memory_returns(host, monkeypatch, tick):
    async def exercise():
        async def pause(event, delay):
            assert 0 <= delay <= 5; host.memory[0] = 9000
            if tick == 'run_exit':
                SpawnService(state=host.service.state)._save(dict(run_id='run-exit', status='completed', ended=1001)); assert event.is_set() or event.wake_path.exists()
            await asyncio.sleep(0)
        monkeypatch.setattr(q, 'queue_pause', pause, raising=False)
        spawn = AsyncMock(side_effect=RuntimeError('launch') if tick == 'final_write' else None); monkeypatch.setattr(host.service, '_spawn', spawn); record = host.service.admit_spawn({'prompt': 'task'})
        assert record['status'] == 'queued' and record['queued_until'] == 1030
        if tick == 'read_error': monkeypatch.setattr(overflow, 'available_memory_mb', Mock(side_effect=[ValueError('transient'), 9000]))
        if tick in ('lock_timeout', 'terminal_write', 'final_write'):
            original, calls = q.queue_lock, []
            @asynccontextmanager
            async def lock(*a, **kw):
                calls.append(1)
                if len(calls) in ((3, 4) if tick == 'final_write' else (1, 2)): raise TimeoutError('transient')
                async with original(*a, **kw): yield
            monkeypatch.setattr(q, 'queue_lock', lock)
            if tick == 'terminal_write': host.clock[0] = 1030
        await asyncio.wait_for(host.service.tasks[record['run_id']], 2)
        if tick in ('terminal_write', 'final_write'):
            assert host.service.result(record['run_id'])['reason'] == ('queue_timeout' if tick == 'terminal_write' else 'queue_start_failed'); assert json.loads(host.service._path(record['run_id']).read_text())['status'] == 'failed'
            if tick == 'terminal_write': spawn.assert_not_called()
        else: assert spawn.call_args.kwargs['_queued_record']['run_id'] == record['run_id']
        assert not host.service._queued
    asyncio.run(exercise())
@pytest.mark.parametrize('bound,findings', [('bad', None), (None, None), (0, None), ('nan', None), (30, None), (30, [{'text': 'one'}, {'text': 'two'}])])
def test_memory_floor_fails_immediately_or_places(host, bound, findings):
    async def exercise():
        host.config.write_text(json.dumps(dict(queue_memory_wait=bound != 30 or bool(findings), queue_bound_s=bound)))
        record = await host.service.spawn(card=dict(task='test', findings=findings), prompt='Implement this change.')
        assert record['status'] == 'refused' and record['reason'] == 'host_memory_floor'; assert (record['mem_available_mb'], record['floor_mb'], record['role_mb']) == (5047, 3000, 2048)
        assert all(str(n) in record['error'] for n in (5047, 3000, 2048))
    asyncio.run(exercise())
def test_cancel_queued_returns_zero(host, monkeypatch):
    async def exercise():
        with pytest.raises(ValueError): await host.service.cancel('run-unknown')
        assert not host.service._path('run-unknown').parent.exists()
        host.service._save(dict(run_id='run-running', status='running'))
        with monkeypatch.context() as patch:
            patch.setattr(q, 'inbox_lock', Mock(side_effect=TimeoutError), raising=False)
            assert (await host.service.cancel('run-running'))['status'] == 'cancelled'
        record = host.service.admit_spawn({'prompt': 'task'}); assert record['status'] == 'queued'; assert (await host.service.cancel(record['run_id']))['exit_code'] == 0
        assert host.service.result(record['run_id'])['status'] == 'cancelled'
    asyncio.run(exercise())
@pytest.mark.parametrize('dispatch', [False, True])
@pytest.mark.parametrize('error', ['planning failed', None])
def test_queue_planning_failure_preserves_reason(host, monkeypatch, dispatch, error):
    from registry_route_util import _Runner, DEFAULT_LEGS
    from plangraph.spawner.runner import spawn_service
    from plangraph.planner.routing import SpawnPlan
    sink = SimpleNamespace(deliver=lambda *a: None)
    host.service = (spawn_service if dispatch else SpawnService)(runner=_Runner(), state=host.service.state, **(dict(completion_sink=sink, keeper_sink=sink) if dispatch else {}))
    legs = () if error else (next(l for l in DEFAULT_LEGS if l.family == 'qwen'),)
    monkeypatch.setattr(host.service, '_route', lambda *a, **kw: SpawnPlan(legs, (), 'test', (), (), error=error))
    monkeypatch.setattr(host.service, '_leg_prompt', lambda *a, **kw: 'review'); monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    async def identity(plan, **kw): return plan
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', identity); monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', lambda *a: identity(None))
    async def exercise():
        record = await host.service.spawn(card={'task': 'test'}, prompt='review', role='review', writer_families=['gpt'])
        assert record['status'] == 'queued'; host.memory[0] = 9000
        await asyncio.wait_for(host.service.tasks[record['run_id']], 10)
        final = host.service.result(record['run_id']); assert final['status'] != 'queued' and final.get('reason') != 'memory_recovery'
        if error: assert final['reason'] == error
        else: assert host.service.runner.calls
        assert final['queue_reason'] == 'memory_recovery'
    asyncio.run(exercise())
