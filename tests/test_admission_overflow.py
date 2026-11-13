import asyncio
import io
import json
import os
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
    service = SharedService()
    service.runner, service._save, service.state = SimpleNamespace(default_cwd=str(tmp_path)), Mock(), tmp_path
    return service, path, remote


@pytest.mark.parametrize('memory', [5048, 9000])
def test_local_room_ignores_counts(setup, monkeypatch, memory):
    service, _, remote = setup
    monkeypatch.setattr(a, 'resources', lambda service: (100, memory))
    assert service.admit_spawn({}) is None
    remote.assert_not_called()


def test_dry_run_preserves_local(setup):
    service, path, remote = setup
    assert service.admit_spawn({'dry_run': True}) is None
    remote.assert_not_called()
    service._save.assert_not_called()


def test_forward_options_and_save(setup):
    service, _, remote = setup
    card = {'diff_ref': {'repo': service.runner.default_cwd}}
    result = service.admit_spawn({'prompt': 'task', 'role': 'review', 'model': 'model',
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
    result = service.admit_spawn({})
    assert result['status'] == 'refused' and result['reason'] == 'host_memory_floor'
    assert reason in result['overflow_reason'] and '/secret' not in result['overflow_reason']
    assert remote.call_count == 2


def test_private_card_never_copies(setup):
    service, _, remote = setup
    result = service.admit_spawn({}, card={'diff_ref': {'repo': '/private'}})
    assert result['status'] == 'refused' and 'not public' in result['overflow_reason']
    remote.assert_not_called()


def test_benchmark_not_forwarded(setup):
    service, _, remote = setup
    assert 'benchmark' in service.admit_spawn({'benchmark': object()})['overflow_reason']
    remote.assert_not_called()


class SharedService(SeatSpawnMixin):
    async def _spawn(self, card=None, prompt='', **kwargs):
        raise AssertionError('local spawn')


@pytest.fixture
def cli_mixin(monkeypatch):
    from plangraph.spawner import cli
    class Input(io.StringIO):
        def close(self):
            pass
    class Supervisor:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stdout = Input(), self
        def readline(self):
            payload = json.loads(self.stdin.getvalue())
            service = cli.SpawnService(state=payload.pop('state'))
            return json.dumps(asyncio.run(service.spawn(**payload))) + '\n'
        def close(self):
            pass
    monkeypatch.setattr(cli, '_start_supervisor', Supervisor)


def test_cli_mixin_admits_once(setup, monkeypatch, capsys, cli_mixin):
    from plangraph.spawner import cli
    service, path, remote = setup
    shared = SharedService()
    shared.runner, shared._save, shared.state = service.runner, service._save, path.parent
    shared.route, shared.save_refusal = Mock(), Mock()
    async def local_spawn(card=None, prompt='', **kwargs):
        return {'run_id': 'run-local', 'status': 'completed', 'exit_code': 0}
    monkeypatch.setattr(shared, '_spawn', local_spawn)
    monkeypatch.setattr(a, 'resources', lambda service: (100, 6000))
    admission = Mock(wraps=a.admit)
    monkeypatch.setattr(a, 'admit', admission)
    public_admission = Mock(wraps=shared.admit_spawn)
    monkeypatch.setattr(shared, 'admit_spawn', public_admission)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: shared)
    assert cli.main(['spawn', '--card', '{"task":"test"}', 'Implement the requested change.']) == 0
    assert json.loads(capsys.readouterr().out)['run_id'] == 'run-local'
    admission.assert_called_once()
    public_admission.assert_called_once()
    remote.assert_not_called()


@pytest.mark.parametrize('entry', ['seat', 'mcp', 'cli'])
def test_no_config_below_floor_refuses_spawn(setup, monkeypatch, capsys, cli_mixin, entry):
    service, path, remote = setup
    path.unlink()
    monkeypatch.setattr(overflow, 'ready_records', lambda config, role: {})
    shared = SharedService()
    shared.runner, shared._save, shared.state = service.runner, service._save, path.parent
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
                         'Implement the requested change.']) == 2
        result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'refused' and result['exit_code'] == 2
    assert result['overflow_reason'] == ('memory: free 4000 MB, floor 3000 MB, role 2048 MB'
                                '; queue: repo is not public (absent or unknown allowlist)')
    assert result['reason'] == 'host_memory_floor'
    remote.assert_not_called()
    shared._save.assert_called_once_with(result)


def test_direct_cli_below_floor_attempts_overflow(setup, monkeypatch, capsys, cli_mixin):
    from plangraph.spawner import cli
    service, _, remote = setup
    service.route = Mock()
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
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
    monkeypatch.setattr(overflow, 'available_memory_mb', lambda: 4000)
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


@pytest.fixture
def orchestration(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PLANGRAPH_HOST', 'host-a')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.delenv('PLANGRAPH_HOST_NAME', raising=False)
    registry = overflow.config_dir() / 'hosts.json'
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'self': 'host-a', 'hosts': [
        dict(id=name, orchestration_only=name == 'host-a',
             caps={'mem_floor_mb': 1024}, warmup={'ramp_mb': {'implement': 1024}})
        for name in ('host-a', 'host-b')]}))
    path = tmp_path / 'overflow.json'
    config = {'public_repos': [str(tmp_path)], 'hosts': {'host-b': {'ssh': 'host-b'}}}
    path.write_text(json.dumps(config))
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(path))
    monkeypatch.setattr(overflow, 'available_memory_mb', lambda: 8000)
    monkeypatch.setattr(overflow, 'swap_used_pct', lambda: 0, raising=False)
    rows = {'host-b': dict(ssh='host-b', ok=True, fresh=True, live_runs=1000,
                          mem_available_mb=9000, role_mb=1024, floor_mb=1024)}
    monkeypatch.setattr(overflow, 'ready_records', lambda config, role: rows)
    remote = Mock(return_value={'run_id': 'run-remote', 'status': 'running'})
    monkeypatch.setattr(overflow, 'remote_spawn', remote)
    service = SpawnService(state=tmp_path / 'state')
    service.runner.default_cwd = str(tmp_path)
    return service, rows, remote, path, registry


@pytest.mark.parametrize('role', ['implement', 'review', 'research'])
def test_orchestration_remote_first(orchestration, role):
    service, _, remote, _, _ = orchestration
    record = service.admit_spawn({'role': role, 'prompt': 'task'})
    assert record['placement'] == dict(host='host-b', reason='remote_most_free_ram', free_mb=9000)
    assert service.result(record['run_id'])['placement'] == record['placement']
    assert remote.call_args.args[-1] == ['--role', role]


@pytest.mark.parametrize('headroom,tied', [(False, False), (True, False), (False, True)])
def test_orchestration_remote_ranking(orchestration, headroom, tied):
    service, rows, _, path, _ = orchestration
    # The first entry wins ties, not the alphabetically smaller name.
    rows['host-c'] = dict(rows['host-b'], mem_available_mb=9000 if tied else 10000,
                          floor_mb=8000 if headroom else 1024)
    config = json.loads(path.read_text())
    config['hosts']['host-c'] = {'ssh': 'host-c'}
    if tied:
        config['hosts']['host-b'] = config['hosts'].pop('host-b')
    path.write_text(json.dumps(config))
    record = a.admit(service, {})
    assert record['host'] == ('host-b' if headroom else 'host-c')


@pytest.mark.parametrize('memory,swap,reason', [
    (4095, 0, 'local_min_available_mb'), (8000, 90.1, 'local_max_swap_used_pct'),
    (4096, 90, 'local_fallback_no_remote'),
])
def test_orchestration_local_fallback(orchestration, monkeypatch, memory, swap, reason):
    service, rows, remote, _, _ = orchestration
    rows['host-b']['mem_available_mb'] = 1024
    monkeypatch.setattr(overflow, 'available_memory_mb', lambda: memory)
    monkeypatch.setattr(overflow, 'swap_used_pct', lambda: swap)
    options = {}
    record = service.admit_spawn(options)
    if reason == 'local_fallback_no_remote':
        assert record is None
        placement = options['_placement']
    else:
        assert record['status'] == 'refused' and record['exit_code'] == 2
        placement = service.result(record['run_id'])['placement']
    assert placement == dict(host='host-a', reason=reason, free_mb=memory)
    remote.assert_not_called()


def test_orchestration_route_record(orchestration, monkeypatch):
    from plangraph.planner.routing import SpawnPlan
    service, _, remote, _, _ = orchestration
    placement = a.admit(service, {}, preview=True)
    monkeypatch.setattr(service, '_route', lambda *args, **kwargs: SpawnPlan((), (), '', (), (), placement=placement))
    assert service.route({}).to_dict()['placement']['host'] == 'host-b'
    remote.assert_not_called()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX fake SSH transport')
def test_orchestration_real_remote_launch(orchestration, monkeypatch, tmp_path):
    import sys
    service, _, _, path, _ = orchestration
    monkeypatch.setattr(overflow, 'remote_spawn', real_remote_spawn)
    commands = tmp_path / 'bin'
    commands.mkdir()
    scripts = {
        'ssh': '#!/bin/sh\nexec /bin/sh -c "$3"\n',
        'scp': f'#!{sys.executable}\nimport shlex, shutil, sys\n'
               'for path in sys.argv[2:4]: shutil.copy(path, shlex.split(sys.argv[4].split(":", 1)[1])[0])\n',
        'python3': f'#!{sys.executable}\nimport json, pathlib, sys\n'
                   'assert json.loads(pathlib.Path(sys.argv[5][1:]).read_text()) == {"task": "test"}\n'
                   'assert sys.argv[6] == "--prompt-file"\n'
                   'assert pathlib.Path(sys.argv[7]).read_text() == "task"\n'
                   'assert "task" not in sys.argv\n'
                   'print(json.dumps({"run_id": "run-real", "status": "running"}))\n',
    }
    for name, script in scripts.items():
        executable = commands / name
        executable.write_text(script)
        executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(commands) + ':/usr/bin:/bin')
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    config = json.loads(path.read_text())
    config['hosts']['host-b'].update(repo=str(tmp_path), spawner_src=str(tmp_path))
    path.write_text(json.dumps(config))
    record = service.admit_spawn({'prompt': 'task'}, card={'task': 'test'})
    assert record['run_id'] == 'run-real' and record['placement']['host'] == 'host-b'
    assert service.result('run-real')['placement'] == record['placement']


real_remote_spawn = overflow.remote_spawn


def test_orchestration_config_thresholds(orchestration, monkeypatch):
    service, rows, _, path, registry = orchestration
    data = json.loads(registry.read_text())
    data['hosts'][0].pop('orchestration_only')
    registry.write_text(json.dumps(data))
    config = json.loads(path.read_text())
    config.update(local_min_available_mb=9000, local_max_swap_used_pct=50)
    config['hosts']['host-a'] = {'orchestration_only': True}
    path.write_text(json.dumps(config))
    rows['host-b']['ok'] = False
    assert a.admit(service, {})['placement']['reason'] == 'local_min_available_mb'
    monkeypatch.setattr(overflow, 'available_memory_mb', lambda: 10000)
    monkeypatch.setattr(overflow, 'swap_used_pct', lambda: 51)
    assert a.admit(service, {})['placement']['reason'] == 'local_max_swap_used_pct'


def test_orchestration_local_spawn_record(orchestration, monkeypatch):
    service, rows, _, _, _ = orchestration
    rows['host-b']['ok'] = False
    async def local(**options):
        record = dict(run_id='run-local', placement=options['_placement'])
        service._save(record)
        return record
    monkeypatch.setattr(service, '_spawn', local)
    result = asyncio.run(service.spawn(prompt='task'))
    assert service.result(result['run_id'])['placement'] == dict(
        host='host-a', reason='local_fallback_no_remote', free_mb=8000)


@pytest.mark.parametrize('entry', ['cli', 'overflow'])
def test_orchestration_cli_remote_first(orchestration, monkeypatch, capsys, cli_mixin, entry):
    from plangraph.spawner import cli
    service, _, remote, path, _ = orchestration
    service.route = Mock()
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    monkeypatch.setattr(service, '_spawn', Mock(side_effect=AssertionError('local launch')))
    args = ['spawn', '--card', '{"task":"test"}', 'task']
    if entry == 'overflow':
        card, prompt = path.parent / 'card.json', path.parent / 'prompt.txt'
        card.write_text('{"task":"test"}')
        prompt.write_text('task')
        args = ['spawn', '--repo', str(path.parent), '--card', str(card), '--prompt', str(prompt)]
    assert (overflow.main if entry == 'overflow' else cli.main)(args) == 0
    assert json.loads(capsys.readouterr().out)['placement']['host'] == 'host-b'
    remote.assert_called_once()
