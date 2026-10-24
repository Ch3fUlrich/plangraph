import pytest

from plangraph.spawner.env_redact import redact_env_values

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner.runner import JobInfo, spawn_service
from plangraph.spawner.service import SpawnService


@pytest.mark.parametrize('factory', [SpawnService, spawn_service])
@pytest.mark.parametrize('rc', [0, 1])
def test_entrypoint_attempt_parity(tmp_path, monkeypatch, factory, rc):
    legs = tuple(replace(DEFAULT_LEGS[0], id=n, model='test/' + n) for n in ('a', 'b'))
    calls = []
    async def spawn(**kw):
        calls.append(kw['model_or_combo'])
        code = rc if len(calls) == 1 else 0
        return JobInfo(str(len(calls)), 'completed' if code == 0 else 'failed',
                       'x', kw['model_or_combo'], exit_code=code,
                       stderr='Model unavailable: x' if len(calls) == 1 else '')
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = factory(runner=runner, state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan(legs, (), 'test', (), ()))
    from plangraph.spawner import resolver
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert calls == [l.model for l in legs[:1 if rc == 0 else 2]]
    assert len(result['attempts']) == len(calls)
    assert result['status'] == 'completed'


def test_child_config_and_cleanup(tmp_path):
    runner = SimpleNamespace(_child_envs={'j': {
        'OPENCODE_CONFIG_CONTENT': '{"providers":{"root":{"env":["ODD_VALUE"]}}}',
        'ODD_VALUE': 'opaque-child-value'}})
    service = SpawnService(runner=runner, state=tmp_path)
    record = dict(run_id='run-child', job_id='j', status='completed', ended=1,
                  stdout_tail='opaque-child-value', stderr_tail='opaque-child-value')
    service._save(record)
    assert 'opaque-child-value' not in service._path('run-child').read_text()
    assert runner._child_envs == {}


def test_missing_harness_recorded(tmp_path, monkeypatch):
    service = spawn_service(state=tmp_path)
    reason = 'harness_missing: agy'
    plan = SpawnPlan((), ({'leg': 'agy', 'reason': reason},), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert (result['status'], result['exit_code']) == ('failed', 1)
    assert result['attempts'] == [dict(leg='agy', status='skipped', skip_reason=reason)]
    assert reason in result['error']


def test_repeating_router_terminates(tmp_path, monkeypatch):
    leg = DEFAULT_LEGS[0]
    calls = []
    async def spawn(**kw):
        calls.append(kw)
        return JobInfo('j', 'failed', 'x', leg.model, exit_code=1, stderr='provider throttled')
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={},
                             provider_responses={'j': {'status': 429}})
    service = spawn_service(runner=runner, state=tmp_path)
    routes = []
    def route(*a, **kw):
        routes.append(True)
        return SpawnPlan((leg,), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', route)
    from plangraph.spawner import resolver
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    result = asyncio.run(asyncio.wait_for(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None), 2))
    assert len(calls) == 1
    assert result['status'] == 'failed'
    assert len(routes) == 2
    assert result['error'] == 'router repeated exhausted legs'


def test_probe_does_not_block_loop(tmp_path, monkeypatch):
    import time
    from plangraph.spawner import resolver
    leg = DEFAULT_LEGS[0]
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: SpawnPlan((leg,), (), 'test', (), ()))
    def probe(plan, **kw):
        time.sleep(.3)
        return SpawnPlan((), ({'leg': leg.id, 'reason': 'harness_missing: fixture'},), 'test', (), ())
    monkeypatch.setattr(resolver, 'plan_for_launch', probe)
    async def run():
        ticks = []
        async def tick():
            while True:
                await asyncio.sleep(.01)
                ticks.append(True)
        ticker = asyncio.create_task(tick())
        try:
            result = await service.spawn(card={'estimate_tokens': 100}, wait_seconds=None)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        assert len(ticks) >= 20
        assert result['status'] == 'failed'
        assert 'harness_missing: fixture' in result['error']
    asyncio.run(run())


def test_iteration_cap_for_router_inventing_legs(tmp_path, monkeypatch):
    calls = []
    routes = []
    async def spawn(**kw):
        calls.append(kw)
        return JobInfo('j', 'failed', 'x', kw['model_or_combo'], exit_code=1)
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={},
                             provider_responses={'j': {'status': 429}})
    service = spawn_service(runner=runner, state=tmp_path)
    def route(*a, **kw):
        routes.append(True)
        leg = replace(DEFAULT_LEGS[0], id=str(len(routes)))
        return SpawnPlan((leg,), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', route)
    from plangraph.spawner import resolver
    monkeypatch.setattr(resolver, 'host_skip_reason', lambda *a, **kw: None)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100}, wait_seconds=None))
    assert len(calls) == 3
    assert (result['status'], result['exit_code']) == ('failed', 1)
    assert result['error'] == 'attempt iteration limit exceeded'


@pytest.mark.parametrize('stdout,stderr', [
    ('', 'Model unavailable: omniroute/x'),
    ('begin\n{"error":{"message":"model unavailable"}}\nend', ''),
    ('begin\nHTTP 404: unknown model\nend', ''),
    ('ERROR: unknown model', ''),
])
def test_unavailable_any_line(stdout, stderr):
    from plangraph.spawner.env_redact import unavailable_reason
    assert unavailable_reason(1, stdout, stderr)
    assert unavailable_reason(0, stdout, stderr) is None
    assert unavailable_reason(1, 'Review complete.\nunknown model was mentioned in the diff', '') is None
    assert unavailable_reason(1, 'Edited parser; TODO: handle unknown model string in fixture', '') is None


@pytest.mark.parametrize('name', ['MY_TOKEN', 'GATEWAY_AUTH', 'CUSTOM_BEARER'])
def test_short_credentials(name):
    env = {name: 'abcd', 'OPENCODE_CONFIG_CONTENT': '{"providers":{"x":{"env":["' + name + '"]}}}'}
    assert 'abcd' not in redact_env_values('value=abcd', env)
    assert redact_env_values('value=abcd', {name: 'abcd'}) == 'value=abcd'


def test_save_nested_collections_and_boundary(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    monkeypatch.setenv('MY_TOKEN', 'opaque-secret-value')
    service = SpawnService(state=tmp_path)
    record = dict(run_id='run-boundary', status='failed',
                  error='x' * 290 + 'opaque-secret-value',
                  nested=({'opaque-secret-value'},))
    service._save(record)
    text = service._path(record['run_id']).read_text()
    assert 'opaque-secret' not in text
