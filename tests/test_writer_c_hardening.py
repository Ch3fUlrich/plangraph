import ast
from pathlib import Path

import pytest

from plangraph.planner.routing import plan_spawn
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.leg_state import LegStateStore


def test_corrupt_leg_state_is_absent(tmp_path):
    path = tmp_path / 'state.sqlite'
    path.write_bytes(b'not sqlite')
    assert LegStateStore(path).leg_status('model') == {'state': 'ok', 'evidence': None}
    assert path.read_bytes() == b'not sqlite'


def test_planner_has_no_spawner_imports():
    for path in Path('src/plangraph/planner').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('plangraph.spawner'), path


def test_risk_reaches_planner_ranking(monkeypatch):
    from plangraph.spawner import resolver
    from plangraph.planner.legs import DEFAULT_LEGS
    seen = []
    original = resolver.rank_candidates
    def rank(*args, **kwargs):
        seen.append(kwargs.get('risk'))
        return original(*args, **kwargs)
    monkeypatch.setattr(resolver, 'rank_candidates', rank)
    plan_spawn({'estimate_tokens': 1000, 'risk': 'critical'}, role='implement',
               leg_reader=lambda *a, **kw: {'state': 'ok', 'evidence': None},
               benchmark={leg.model: {'aa_intelligence': 90, 'verified_tok_per_s': 100}
                          for leg in DEFAULT_LEGS})
    assert seen and set(seen) == {'critical'}


def test_list_leg_id_is_typed_refusal():
    from plangraph.planner.routing import next_leg_after_provider_stop, SpawnPlan
    with pytest.raises(SpawnRefused):
        next_leg_after_provider_stop(SpawnPlan((), (), '', (), ()), [], 429, 'quota', {}, 'test')


def test_cli_tuple_list_diagnostics(tmp_path, monkeypatch, capsys):
    from plangraph.spawner.cli import main
    from plangraph.spawner.service import SpawnService
    class Plan:
        def to_dict(self):
            return {'skipped': ({'leg': 'a', 'reason': 'limited'},),
                    'attempts': [{'leg': {'id': 'b'}, 'skip_reason': 'untried'}]}
    monkeypatch.setattr(SpawnService, 'route', lambda *a, **kw: Plan())
    assert main(['--state', str(tmp_path), 'route', '--card', '{"estimate_tokens":1000}']) == 0
    captured = capsys.readouterr()
    assert 'skip a: limited' in captured.err
    assert 'skip b: untried' in captured.err
    assert 'skip ' not in captured.out


def test_empty_model_validates_before_launch(tmp_path, monkeypatch, capsys):
    from plangraph.spawner.cli import main
    from plangraph.spawner.service import SpawnService
    def forbidden(*args, **kwargs):
        pytest.fail('empty model must never route or launch')
    monkeypatch.setattr(SpawnService, 'route', forbidden)
    assert main(['--state', str(tmp_path), 'spawn', 'task', '--model', '',
                 '--card', '{"estimate_tokens":1000}']) == 2
    assert 'nonempty string' in capsys.readouterr().err


def test_parameter_refusal_is_typed():
    from plangraph.routing_errors import ParameterRefused
    assert issubclass(ParameterRefused, SpawnRefused)


def test_private_and_leg_state_contention(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from plangraph.spawner.private_io import atomic_write_private
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_probe_pass('model', 'test')
    def write(i):
        store.record_provider_response('model', 429, 'quota', {}, 'test', now=i)
        atomic_write_private(tmp_path / 'record', str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    assert store.leg_status('model', now=40)['state'] == 'limited'
    assert (tmp_path / 'record').read_text().isdigit()


def test_windows_replace_contention_retries(tmp_path, monkeypatch):
    from plangraph.spawner import private_io
    original = private_io.os.replace
    calls = []
    def replace(source, target):
        calls.append(1)
        if len(calls) < 3:
            exc = PermissionError('sharing violation')
            exc.winerror = 32
            raise exc
        return original(source, target)
    monkeypatch.setattr(private_io.os, 'replace', replace)
    private_io.atomic_write_private(tmp_path / 'state', 'complete')
    assert (tmp_path / 'state').read_text() == 'complete'
    assert len(calls) == 3


@pytest.mark.real_probe
def test_five_minutes_is_probe_ttl_not_delivery_sla(monkeypatch):
    from plangraph.spawner import resolver
    resolver._served_cache.clear()
    calls = []
    monkeypatch.setattr(resolver, '_probe_served_models', lambda config: calls.append(config) or {'model'})
    now = [0]
    for timestamp in (0, 299, 300):
        now[0] = timestamp
        assert resolver.served_models('{}', clock=lambda: now[0]) == {'model'}
    assert len(calls) == 2
    assert 'not a five-minute completion or delivery SLA' in Path('docs/SPAWNER.md').read_text()


def test_mcp_business_message_cannot_impersonate_parameter_error(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner.mcp import MCPSpawnerServer
    server = MCPSpawnerServer(state=tmp_path)
    async def refuse(*args, **kwargs):
        raise SpawnRefused('spawn by card; card required')
    monkeypatch.setattr(server, 'call_tool', refuse)
    response = asyncio.run(server.handle_rpc_request({'jsonrpc': '2.0', 'id': 1,
        'method': 'tools/call', 'params': {'name': 'plangraph_agent_spawn', 'arguments': {}}}))
    assert response['error']['code'] == -32000


@pytest.mark.parametrize('method,args', [('status', ([],)), ('cancel', ([],)), ('steer', ([], 'message'))])
def test_list_job_ids_are_typed_refusals(method, args):
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner
    with pytest.raises(SpawnRefused):
        asyncio.run(getattr(SpawnerRunner(), method)(*args))
