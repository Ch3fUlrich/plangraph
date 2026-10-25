import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from plangraph.spawner.risk import evaluate_risk
from plangraph.planner.routing import plan_spawn, SpawnRefused, BenchRequired, BenchDataError
from plangraph.spawner.leg_state import LegStateStore
from plangraph.spawner.runner import SpawnerRunner


def test_tags_do_not_downgrade():
    assert evaluate_risk(['keeper/engine.py'], has_tests=False, risk_tags=['live-step']).risk == 'critical'


@pytest.mark.parametrize('path', ['keeper/tests/engine.py', 'docs/install.sh', 'keeper/schema.py'])
def test_structural_paths(path):
    assert evaluate_risk([path]).risk == 'high'


def test_untrusted_doc_flag():
    with pytest.raises(SpawnRefused):
        plan_spawn(dict(estimate_tokens=100, paths=['src/main.py'], bucket='S0', is_pure_doc=True, reviewer_count=0), role='implement')


@pytest.mark.parametrize('field,value', [('paths', 1), ('risk_tags', 'auth'), ('bucket', []), ('has_tests', 'yes'), ('estimate_tokens', False), ('reviewer_count', '0')])
def test_card_types(field, value):
    with pytest.raises(SpawnRefused, match=field):
        plan_spawn(dict(estimate_tokens=100, **{field: value}) if field != 'estimate_tokens' else {field: value}, role='implement')


def test_bench_siblings():
    assert not issubclass(BenchDataError, BenchRequired)


def test_empty_state(tmp_path):
    path = tmp_path / 'state.db'
    path.touch()
    assert LegStateStore(path).leg_status('bare')['state'] == 'ok'


def test_slashless_state(tmp_path):
    store = LegStateStore(tmp_path / 'state.db')
    store.record_provider_response('bare', 429, 'limited', {}, 'test', now=1)
    assert store.leg_status('bare', now=2)['state'] == 'limited'


def test_closing_stdin():
    runner = SpawnerRunner()
    stdin = Mock()
    stdin.is_closing.return_value = True
    runner.jobs['job'] = SimpleNamespace(steer_messages=[], status='running')
    runner._processes['job'] = SimpleNamespace(stdin=stdin)
    result = asyncio.run(runner.steer('job', 'hello'))
    assert result['steered'] is False
    assert result['ok'] is False
    stdin.write.assert_not_called()


@pytest.mark.parametrize('suffix,expected', [(' v2.6', True), ('+x', True), ('dev', False), ('sa', False)])
def test_mimo_tokens(suffix, expected):
    from plangraph.mimo_policy import is_mimo_id
    assert is_mimo_id('mi' + 'mo' + suffix) is expected


def test_mimo_at():
    from plangraph.mimo_policy import is_mimo_id
    assert is_mimo_id('x@' + 'mi' + 'mo')


@pytest.mark.parametrize('text', ['{"x": /* unfinished', '{"x": [}'])
def test_sweep_bad_structure(text, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('sweep', 'tools/mimo_path_sweep.py')
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    (tmp_path / 'bad.jsonc').write_text(text)
    assert any('error' in hit for hit in sweep.scan(tmp_path, []))


def test_override_own_leg(tmp_path):
    from plangraph.spawner.service import SpawnService
    from plangraph.planner.legs import DEFAULT_LEGS
    target = next(l for l in DEFAULT_LEGS if l.harness == 'qodercli')
    result = asyncio.run(SpawnService(state=tmp_path).spawn(card={'estimate_tokens': 100}, model=target.model, override_reason='test audited override', dry_run=True))
    assert result['chosen_leg']['harness'] == target.harness
    assert result['chosen_leg']['tier'] == target.tier
    assert result['chosen_leg']['windows'] == list(target.windows)


def test_missing_card_parity(tmp_path, capsys):
    from plangraph.spawner.cli import main
    from plangraph.spawner.mcp import MCPSpawnerServer
    assert main(['--state', str(tmp_path), 'spawn', 'task', '--model', 'Qwen3.8-Flash', '--override-reason', 'audited override']) == 2
    response = asyncio.run(MCPSpawnerServer(state=tmp_path).handle_rpc_request({
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'plangraph_agent_spawn', 'arguments': {'prompt': 'task', 'model_or_combo': 'Qwen3.8-Flash'}}}))
    assert response['error']['data']['type'] == 'ParameterRefused'
    assert response['error']['code'] == -32602
    assert response['error']['message'] == capsys.readouterr().err.strip()


def test_preflight_persisted(tmp_path):
    from plangraph.spawner.runner import spawn_service
    service = spawn_service(state=tmp_path)
    with pytest.raises(SpawnRefused):
        asyncio.run(service.spawn(card={'estimate_tokens': False}))
    records = service.ps()['runs']
    assert len(records) == 1
    assert records[0]['exit_code'] == 2
    assert records[0]['status'] == 'refused'


@pytest.mark.parametrize('payload', [[], None, {}, {'method': 1}, {'method': 'tools/list', 'params': []}])
def test_invalid_rpc(payload, tmp_path):
    from plangraph.spawner.mcp import MCPSpawnerServer
    response = asyncio.run(MCPSpawnerServer(state=tmp_path).handle_rpc_request(payload))
    assert response['error']['code'] == -32600
