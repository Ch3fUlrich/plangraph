import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plangraph.spawner import admission_overflow as a
from plangraph.spawner import overflow
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'hosts.json'
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(path))
    monkeypatch.setattr(a, 'resources', lambda service: (4, 6000))
    path.write_text(json.dumps({'public_repos': [str(tmp_path)],
                                'hosts': {'host-a': {}, 'host-b': {}}}))
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path)), _save=Mock())
    remote = Mock(return_value={'run_id': 'run-abc', 'status': 'running'})
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    monkeypatch.setattr(overflow, 'ready_records', lambda config: {
        'host-a': {'ssh': 'host-a', 'ok': True, 'fresh': True, 'live_runs': 0, 'mem_available_mb': 6000}})
    return service, path, remote


def test_local_room_never_overflows(setup, monkeypatch):
    service, _, remote = setup
    monkeypatch.setattr(a, 'resources', lambda service: (0, 6000))
    monkeypatch.setattr(overflow, 'decide', Mock(side_effect=AssertionError('overflow called')))
    assert a.admit(service, {}) is None
    remote.assert_not_called()


@pytest.mark.parametrize('resources', [(4, 6000), (0, 2999)])
def test_cap_or_floor_remote_once(setup, monkeypatch, resources):
    service, _, remote = setup
    monkeypatch.setattr(a, 'resources', lambda service: resources)
    result = a.admit(service, {'card': {}, 'prompt': 'task'})
    assert result['host'] == 'host-a'
    remote.assert_called_once()
    service._save.assert_called_once_with(result)


def test_unconfigured_preserves_local(setup):
    service, path, remote = setup
    path.unlink()
    assert a.admit(service, {}) is None
    assert service._overflow_reason == 'overflow unconfigured'
    remote.assert_not_called()


def test_all_refusals_queue(setup, monkeypatch):
    service, _, remote = setup
    monkeypatch.setattr(overflow, 'ready_records', lambda config: {
        'host-a': {'refusal': 'stale readiness'}, 'host-b': {'refusal': 'memory floor'}})
    result = a.admit(service, {})
    assert result['status'] == 'queued'
    assert 'host-a: stale readiness' in result['reason']
    assert 'host-b: memory floor' in result['reason']
    remote.assert_not_called()


class SharedService(SeatSpawnMixin):
    async def _spawn(self, card=None, prompt='', **kwargs):
        raise AssertionError('local spawn')


def test_cli_spawn_shared_admission(setup, monkeypatch):
    from plangraph.spawner import cli
    service, _, remote = setup
    shared = SharedService()
    shared.tasks = {}
    shared.runner, shared._save = service.runner, service._save
    shared.route = lambda *args, **kw: None
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: shared)
    import io
    class Output(io.StringIO):
        def close(self):
            pass
    output = Output()
    monkeypatch.setattr(cli.sys, 'stdout', output)
    # Exercise the same supervisor entry used by detached CLI spawn.
    monkeypatch.setattr(cli.sys, 'stdin', __import__('io').StringIO(json.dumps({
        'state': 'unused', 'card': {}, 'prompt': 'Implement the requested change.'}) + '\n'))
    assert cli.main(['--supervise']) == 0
    assert json.loads(output.getvalue())['host'] == 'host-a'
    remote.assert_called_once()


def test_mcp_spawn_shared_admission(setup):
    from plangraph.spawner.mcp import MCPSpawnerServer
    service, _, remote = setup
    shared = SharedService()
    shared.runner, shared._save = service.runner, service._save
    server = object.__new__(MCPSpawnerServer)
    server.service = shared
    result = asyncio.run(server.call_tool('plangraph_agent_spawn', {
        'card': {'task': 'test'}, 'prompt': 'Implement the requested change.'}))
    assert result['host'] == 'host-a'
    remote.assert_called_once()
