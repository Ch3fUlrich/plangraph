import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plangraph.spawner import seat_spawn as a, overflow
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'public_repos': [str(tmp_path)], 'hosts': {'ash': {}, 'birch': {}}}))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(path))
    monkeypatch.setattr(overflow, 'local_limits', lambda registry, role: (2048, 3000))
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(a, 'resources', lambda service: (100, 4000))
    monkeypatch.setattr(overflow, 'ready_records', lambda config, role: {
        name: dict(ssh=name, ok=True, fresh=True, live_runs=100,
                   mem_available_mb=6000, role_mb=2048, floor_mb=3000)
        for name in ('ash', 'birch')})
    remote = Mock(return_value={'run_id': 'run-remote', 'status': 'running'})
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path)), _save=Mock())
    return service, path, remote


@pytest.mark.parametrize('memory', [5048, 9000])
def test_local_room_ignores_counts(setup, monkeypatch, memory):
    service, _, remote = setup
    monkeypatch.setattr(a, 'resources', lambda service: (100, memory))
    assert a.admit(service, {}) is None
    remote.assert_not_called()


def test_dry_run_preserves_local(setup):
    service, path, remote = setup
    assert a.admit(service, {'dry_run': True}) is None
    remote.assert_not_called()
    service._save.assert_not_called()


def test_forward_options_and_save(setup):
    service, _, remote = setup
    card = {'diff_ref': {'repo': service.runner.default_cwd}}
    result = a.admit(service, {'prompt': 'task', 'role': 'review', 'model': 'model',
                              'writer_families': ['family'], 'seats': True}, card=card)
    assert result['host'] == 'ash' and result['overflow_remote']
    assert remote.call_args.args[2:] == (card, 'task', [
        '--role', 'review', '--model', 'model', '--writer-family', 'family', '--seats'])
    service._save.assert_called_once_with(result)


@pytest.mark.parametrize('failure,reason', [
    (overflow.RemoteRefusal('remote copy failed'), 'remote copy failed'),
    (OSError(2, 'private /secret/path'), 'remote spawn OS operation failed:'),
    (ValueError('/secret/path'), 'invalid value'),
    (KeyError('/secret/path'), 'required field'),
    (TypeError('/secret/path'), 'invalid type'),
])
def test_failures_queue_with_safe_reasons(setup, failure, reason):
    service, _, remote = setup
    remote.side_effect = failure
    result = a.admit(service, {})
    assert result['status'] == 'queued'
    assert reason in result['reason'] and '/secret' not in result['reason']
    assert remote.call_count == 2


def test_private_card_never_copies(setup):
    service, _, remote = setup
    result = a.admit(service, {}, card={'diff_ref': {'repo': '/private'}})
    assert result['status'] == 'queued' and 'not public' in result['reason']
    remote.assert_not_called()


def test_benchmark_not_forwarded(setup):
    service, _, remote = setup
    assert 'benchmark' in a.admit(service, {'benchmark': object()})['reason']
    remote.assert_not_called()


class SharedService(SeatSpawnMixin):
    async def _spawn(self, card=None, prompt='', **kwargs):
        raise AssertionError('local spawn')


@pytest.mark.parametrize('entry', ['seat', 'mcp', 'cli'])
def test_no_config_below_floor_refuses_spawn(setup, monkeypatch, capsys, entry):
    service, path, remote = setup
    path.unlink()
    monkeypatch.setattr(overflow, 'ready_records', lambda config, role: {})
    shared = SharedService()
    shared.runner, shared._save = service.runner, service._save
    shared.route = Mock()
    if entry == 'seat':
        result = asyncio.run(shared.spawn(prompt='Implement the requested change.'))
    elif entry == 'mcp':
        from plangraph.spawner.mcp import MCPSpawnerServer
        server = object.__new__(MCPSpawnerServer)
        server.service = shared
        result = asyncio.run(server.call_tool('plangraph_agent_spawn', {
            'card': {'task': 'test'}, 'prompt': 'Implement the requested change.'}))
    else:
        from plangraph.spawner import cli
        monkeypatch.setattr(cli, 'SpawnService', lambda **kw: shared)
        assert cli.main(['spawn', '--card', '{"task":"test"}',
                         'Implement the requested change.']) == 5
        result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'queued' and result['exit_code'] == 5
    assert result['reason'] == ('memory: free 4000 MB, floor 3000 MB, role 2048 MB'
                                '; queue: repo is not public (absent or unknown allowlist)')
    remote.assert_not_called()
    shared._save.assert_called_once_with(result)


def test_direct_cli_below_floor_attempts_overflow(setup, monkeypatch, capsys):
    from plangraph.spawner import cli
    service, _, remote = setup
    service.route = Mock()
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    monkeypatch.setattr(cli, '_start_supervisor', Mock(side_effect=AssertionError('local supervisor')))
    assert cli.main(['spawn', '--card', '{"task":"test"}',
                     'Implement the requested change.']) == 0
    assert json.loads(capsys.readouterr().out)['host'] == 'ash'
    remote.assert_called_once()


@pytest.mark.parametrize('arguments,message', [
    (['--card', '{"task":"test"}', '--review-scope', 'batch', 'task'],
     'review_scope only valid for role=review'),
    (['task'], 'spawn by card; card required'),
    (['--card', '{"task":"test"}'], 'prompt'),
    (['--card', '{"task":"test"}', 'bad\x01prompt'], 'prompt contains control U+0001'),
])
def test_cli_validates_before_overflow(setup, monkeypatch, capsys, arguments, message):
    from plangraph.spawner import cli
    service, _, remote = setup
    service.save_refusal = Mock()
    service.route = Mock()
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    assert cli.main(['spawn', *arguments]) == 2
    assert message in capsys.readouterr().err
    remote.assert_not_called()
    service._save.assert_not_called()
    service.save_refusal.assert_called_once()


def test_cli_route_validates_before_overflow(setup, monkeypatch, capsys):
    from plangraph.spawner import cli
    from plangraph.planner.routing import SpawnRefused
    service, _, remote = setup
    service.route = Mock(side_effect=SpawnRefused('invalid route'))
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    assert cli.main(['spawn', '--card', '{"task":"test"}', 'task']) == 2
    assert capsys.readouterr().err.strip() == 'invalid route'
    remote.assert_not_called()
    service._save.assert_not_called()


@pytest.mark.parametrize('entry', ['cli', 'mcp'])
def test_shared_spawn_admission(setup, monkeypatch, entry):
    from plangraph.spawner import cli
    from plangraph.spawner.mcp import MCPSpawnerServer
    service, _, remote = setup
    shared = SharedService()
    shared.runner, shared._save = service.runner, service._save
    if entry == 'mcp':
        server = object.__new__(MCPSpawnerServer)
        server.service = shared
        result = asyncio.run(server.call_tool('plangraph_agent_spawn', {
            'card': {'task': 'test'}, 'prompt': 'Implement the requested change.'}))
    else:
        import io
        shared.tasks = {}
        shared.route = lambda *args, **kw: None
        monkeypatch.setattr(cli, 'SpawnService', lambda **kw: shared)
        class Output(io.StringIO):
            def close(self):
                pass
        output = Output()
        monkeypatch.setattr(cli.sys, 'stdout', output)
        monkeypatch.setattr(cli.sys, 'stdin', io.StringIO(json.dumps({
            'state': 'unused', 'card': {}, 'prompt': 'Implement the requested change.'}) + '\n'))
        assert cli.main(['--supervise']) == 0
        result = json.loads(output.getvalue())
    assert result['host'] == 'ash'
    remote.assert_called_once()
