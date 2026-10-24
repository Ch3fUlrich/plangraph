import asyncio
import importlib.util
import pytest
from plangraph.spawner.service import SpawnService
from plangraph.planner.routing import BenchRequired, SpawnRefused, route_card, RouteError


def test_refusal_role_and_evidence(tmp_path, monkeypatch):
    service = SpawnService(state=tmp_path)
    async def refuse(*args, **kwargs):
        raise BenchRequired('missing', [{'leg': 'x', 'reason': 'missing'}])
    monkeypatch.setattr(service, '_spawn', refuse)
    with pytest.raises(BenchRequired):
        asyncio.run(service.spawn({}, '', 'review'))
    record = service.ps()['runs'][0]
    assert record['role'] == 'review'
    assert record['legs'] == ['x']
    assert record['evidence'] == [{'leg': 'x', 'reason': 'missing'}]


def test_explicit_leg_preserved(tmp_path, monkeypatch):
    from dataclasses import replace
    from plangraph.planner import legs
    from plangraph.planner.legs import DEFAULT_LEGS
    target = replace(DEFAULT_LEGS[0], id='duplicate-model-leg')
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (*DEFAULT_LEGS, target))
    result = asyncio.run(SpawnService(state=tmp_path).spawn(card={'estimate_tokens': 100}, leg=target.id, override_reason='audited test override', dry_run=True))
    assert result['chosen_leg']['id'] == target.id


def test_empty_state_read_only(tmp_path):
    from plangraph.spawner.leg_state import LegStateStore
    path = tmp_path / 'state.db'
    path.touch()
    assert LegStateStore(path).leg_status('x')['state'] == 'ok'
    assert path.read_bytes() == b''


@pytest.mark.parametrize('text', ['"unfinished', '/*/'])
def test_tokenizer_rejects_incomplete(text):
    spec = importlib.util.spec_from_file_location('sweep', 'tools/mimo_path_sweep.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError):
        module.model_key_exemptions(text)


def test_typed_exclusion():
    with pytest.raises(RouteError) as caught:
        route_card({'estimate_tokens': 100}, legs=())
    assert caught.value.exclusion_reasons == {}


def test_missing_rpc_version_explicit(tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer
    response = asyncio.run(MCPSpawnerServer(state=tmp_path).handle_rpc_request({'id': 1, 'method': 'tools/list'}))
    assert response['error']['code'] == -32600


def test_mimo_glue():
    from plangraph.mimo_policy import check_mimo_path, MimoPathError
    for value in ['omniroute/' + 'mi' + 'mov2.6-flash', '(' + 'mi' + 'mo)', 'mi' + 'mo,v2.6-flash']:
        with pytest.raises(MimoPathError):
            check_mimo_path(value)


def test_unknown_steer_shape():
    from plangraph.spawner.runner import SpawnerRunner
    result = asyncio.run(SpawnerRunner().steer('missing', 'hello'))
    assert result == {'ok': False, 'steered': False, 'error': 'unknown job'}


def test_ast_unreadable_unknown(tmp_path, monkeypatch):
    from pathlib import Path
    from plangraph.spawner.measure import ast_caller_fanout
    (tmp_path / 'bad.py').touch()
    def fail(*args, **kwargs):
        raise OSError('unreadable')
    monkeypatch.setattr(Path, 'read_text', fail)
    assert ast_caller_fanout(tmp_path, ['module.py']) is None


def test_cli_early_refusal_recorded(tmp_path):
    from plangraph.spawner.cli import main
    assert main(['--state', str(tmp_path), 'spawn', 'task']) == 2
    assert SpawnService(state=tmp_path).ps()['runs'][0]['exit_code'] == 2


def test_mcp_early_refusal_recorded(tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    with pytest.raises(SpawnRefused):
        asyncio.run(server.call_tool('plangraph_agent_spawn', {'prompt': 'task'}))
    assert server.service.ps()['runs'][0]['exit_code'] == 2


def test_limit_exit_distinct_from_benchmark(tmp_path, monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import spawn_service
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr(service, 'route', lambda *a, **k: SpawnPlan((), (), 'operator-order', (), ()))
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}))
    assert record['status'] == 'limited'
    assert record['exit_code'] == 4


def test_save_failure_preserves_refusal(tmp_path, monkeypatch):
    service = SpawnService(state=tmp_path)
    def fail(record):
        raise OSError('storage unavailable')
    monkeypatch.setattr(service, '_save', fail)
    with pytest.raises(SpawnRefused, match='card required'):
        asyncio.run(service.spawn())


def test_existing_safety_contracts():
    from plangraph.planner.routing import plan_spawn, BenchDataError
    from plangraph.mimo_policy import check_mimo_path, MimoPathError
    from plangraph.spawner.risk import evaluate_risk
    assert not issubclass(BenchDataError, BenchRequired)
    with pytest.raises(SpawnRefused, match='estimate_tokens'):
        plan_spawn({'estimate_tokens': True}, role='implement')
    risk = evaluate_risk(['src/code.py'], bucket='S0', is_pure_doc=True)
    assert risk.review_exempt is False
    with pytest.raises(SpawnRefused, match='zero reviewers'):
        plan_spawn({'estimate_tokens': 100, 'paths': ['src/code.py'],
                    'review_exempt': True, 'reviewer_count': 0}, role='implement')
    for suffix in ('flash', 'pro'):
        exact = 'mi' + 'mo/mi' + 'mo-v2.6-' + suffix
        assert check_mimo_path(exact) == exact
        with pytest.raises(MimoPathError):
            check_mimo_path(exact.upper())
    assert check_mimo_path('foo/' + 'mi' + 'modev').endswith('dev')
    assert check_mimo_path('mi' + 'mosa').endswith('sa')


def test_provider_requires_authenticated_source():
    from plangraph.spawner.runner import SpawnerRunner
    runner = SpawnerRunner()
    runner.jobs['known'] = object()
    with pytest.raises(ValueError):
        runner.provider_response('known', 'model', 429, 'quota', {})


def test_benchmark_preflight_persisted(tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.planner.routing import BenchDataError
    server = MCPSpawnerServer(state=tmp_path)
    with pytest.raises(BenchDataError):
        asyncio.run(server.call_tool('plangraph_agent_spawn', {
            'prompt': 'task', 'card': {'estimate_tokens': 100},
            'bench_db': str(tmp_path / 'missing.db')}))
    assert server.service.ps()['runs'][0]['exit_code'] == 3


def test_override_missing_harness_refused(tmp_path, monkeypatch):
    from plangraph.spawner import resolver
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        {'id': 'test/custom', 'tier': 'paid', 'context_tokens': 131072}])
    with pytest.raises(SpawnRefused, match='registry data'):
        asyncio.run(SpawnService(state=tmp_path).spawn(
            card={'estimate_tokens': 100}, model='test/custom',
            override_reason='audited test override', dry_run=True))


def test_steer_does_not_succeed_without_write():
    from types import SimpleNamespace
    from unittest.mock import Mock, AsyncMock
    from plangraph.spawner.runner import SpawnerRunner
    runner = SpawnerRunner()
    stdin = Mock()
    stdin.is_closing.side_effect = [False, True]
    stdin.drain = AsyncMock()
    runner.jobs['job'] = SimpleNamespace(steer_messages=[], events=[])
    runner._processes['job'] = SimpleNamespace(stdin=stdin)
    result = asyncio.run(runner.steer('job', 'hello'))
    assert result['steered'] is (stdin.write.call_count == 1)
