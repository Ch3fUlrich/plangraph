import pytest

from plangraph.planner.routing import validate_model, SpawnRefused
from plangraph.spawner import resolver, stall_watchdog
from plangraph.spawner.runner import spawn_service


@pytest.mark.parametrize('exc,rc', [(ValueError('bad'), 1), (KeyError('bad'), 1),
                                  (OSError('bad'), 1), (SpawnRefused('bad'), 2)])
def test_cli_exception_contract(tmp_path, monkeypatch, capsys, exc, rc):
    from plangraph.spawner import cli
    from plangraph.routing_errors import RouteError
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    def fail():
        raise exc
    monkeypatch.setattr(service, 'ps', fail)
    assert cli.main(['ps']) == rc
    assert 'Traceback' not in capsys.readouterr().err


def test_routeerror_cli_contract(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import cli
    from plangraph.routing_errors import RouteError
    service = spawn_service(state=tmp_path)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    def fail():
        raise RouteError('bad')
    monkeypatch.setattr(service, 'ps', fail)
    assert cli.main(['ps']) == 1
    assert 'Traceback' not in capsys.readouterr().err


@pytest.mark.parametrize('suffix', ['flash', 'pro'])
def test_accepted_mimo_catalog_ids(suffix):
    model = 'mimo/mimo-v2.6-' + suffix
    assert validate_model(model) == model


def test_runner_default_is_admitted(monkeypatch):
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner
    seen = []
    from plangraph.planner import routing
    original = routing.validate_model
    def validate(model):
        seen.append(model)
        return original(model)
    monkeypatch.setattr(routing, 'validate_model', validate)
    asyncio.run(SpawnerRunner().spawn(prompt='test', dry_run=True))
    assert seen == ['mimo/mimo-v2.6-flash']


def test_override_refusal_has_record(tmp_path, monkeypatch):
    import asyncio
    service = spawn_service(state=tmp_path)
    with pytest.raises(SpawnRefused):
        asyncio.run(service.spawn(card={'bucket': 'S0', 'risk': 'low'}, prompt='test',
                                  leg='missing', override_reason='operator selection'))
    records = service.ps()['runs']
    assert len(records) == 1
    assert records[0]['exit_code'] == 2


def test_old_watchdog_module_not_referenced():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    old = 'plangraph.' + 'stall_watchdog'
    for directory in ('src', 'tools', 'deploy', 'docs', 'tests'):
        for path in (root / directory).rglob('*'):
            if path.is_file() and path.suffix in ('.py', '.md', '.sh', '.ps1', '.service', '.xml'):
                assert old not in path.read_text(), str(path)


def test_catalog_cannot_bypass_mimo_policy(monkeypatch):
    bad = 'omniroute/' + 'mi' + 'mo-v2.6-flash'
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [{'id': bad}])
    with pytest.raises(SpawnRefused):
        validate_model(bad)


def test_inactive_catalog_refused(monkeypatch):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [{'id': 'test/inactive', 'active': False}])
    with pytest.raises(SpawnRefused):
        validate_model('test/inactive')


def test_catalog_requests_active_rows_only():
    class Catalog:
        def list_models(self, *, active_only):
            assert active_only is True
            return []
    assert resolver.get_catalog_models(Catalog(), active_only=True) == []


@pytest.mark.parametrize('entry', ['route', 'runner', 'override', 'service', 'leg', 'cli', 'mcp'])
@pytest.mark.parametrize('kind', ['accepted', 'wrong-provider', 'inactive'])
def test_admission_entry_matrix(tmp_path, monkeypatch, capsys, entry, kind):
    import asyncio
    from dataclasses import replace
    from plangraph.planner import routing, legs
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner.cli import main
    from plangraph.spawner.mcp import MCPSpawnerServer
    model = {'accepted': 'mimo/mimo-v2.6-flash',
             'wrong-provider': 'omniroute/' + 'mi' + 'mo-v2.6-flash',
             'inactive': 'test/inactive'}[kind]
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [
        dict(id=model, active=kind != 'inactive')])
    chosen = replace(legs.DEFAULT_LEGS[0], model=model)
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', (chosen,))
    card = {'estimate_tokens': 100}
    options = dict(card=card, prompt='test', override_reason='explicit operator selection', dry_run=True)
    def invoke():
        if entry == 'route':
            return routing.plan_spawn(card, role='implement', legs=(chosen,))
        if entry == 'runner':
            return asyncio.run(SpawnerRunner().spawn('test', model_or_combo=model, dry_run=True))
        if entry == 'override':
            return routing.hand_picked(model, override_reason=options['override_reason'], log_path=tmp_path / 'audit')
        if entry in ('service', 'leg'):
            return asyncio.run(spawn_service(state=tmp_path).spawn(**options,
                **({'leg': chosen.id} if entry == 'leg' else {'model': model})))
        if entry == 'cli':
            return main(['--state', str(tmp_path), 'spawn', 'test', '--card', '{"estimate_tokens":100}',
                         '--model', model, '--override-reason', options['override_reason'], '--dry-run'])
        return asyncio.run(MCPSpawnerServer(state=tmp_path).handle_rpc_request({
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                'name': 'plangraph_agent_spawn', 'arguments': dict(options, model_or_combo=model)}}))
    if entry == 'cli':
        assert invoke() == (0 if kind == 'accepted' else 2)
    elif entry == 'mcp':
        result = invoke()
        assert ('error' in result) == (kind != 'accepted')
        if kind != 'accepted':
            assert result['error']['data']['type'] in ('SpawnRefused', 'MimoPathError')
    elif kind == 'accepted':
        invoke()
    else:
        with pytest.raises(SpawnRefused):
            invoke()


def test_watchdog_identity_ignores_age(tmp_path):
    event = dict(rule='idle-claim', item_key='PG-1', recipient='routing', age=100)
    stall_watchdog.deliver_event(tmp_path, event, now=100, interval=1000)
    next((tmp_path / 'watchdog/outbox').glob('*.json')).unlink()
    stall_watchdog.deliver_event(tmp_path, dict(event, age=101), now=101, interval=1000)
    assert not list((tmp_path / 'watchdog/outbox').glob('*.json'))


def test_retry_jobless_terminal_and_skip_delivered(tmp_path, monkeypatch):
    service = spawn_service(state=tmp_path)
    record = dict(run_id='run-abc', started=1, ended=2, status='refused', exit_code=2)
    from plangraph.spawner.service import SpawnService
    SpawnService._save(service, record)
    service.retry_completions()
    assert 'completion' in service.result('run-abc')
    saved = []
    monkeypatch.setattr(service, '_save', lambda record: saved.append(record))
    service.retry_completions()
    assert saved == []


def test_retry_does_not_rewrite_unchanged_record(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    class Offline:
        def deliver(self, *args):
            raise OSError('offline')
    service = spawn_service(state=tmp_path, completion_sink=Offline(), keeper_sink=Offline())
    service._save(dict(run_id='run-def', started=1, ended=2, status='refused', exit_code=2))
    saved = []
    monkeypatch.setattr(SpawnService, '_save', lambda self, record: saved.append(record))
    service.retry_completions()
    assert saved == []
