import asyncio
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from registry_route_util import use_projected_overflow_config

from plangraph.spawner import seat_spawn as a, overflow
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'public_repos': [str(tmp_path)], 'hosts': {'ash': {}, 'birch': {}}}))
    use_projected_overflow_config(monkeypatch, path)
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


@pytest.mark.parametrize('options,card,movable', [
    ({}, None, True),
    ({'cwd': '/named/checkout'}, None, False),
    ({}, {'cwd': '/named/checkout'}, False),
    ({'role': 'review'}, {'diff_ref': {'repo': '/named/checkout'}}, False),
    ({}, {'repo': '/named/checkout'}, False),
])
def test_admission_passes_the_placement_decision_to_the_disk_check(setup, monkeypatch, options, card, movable):
    from plangraph.spawner import host_ready
    service, _, _ = setup
    monkeypatch.setattr(a, 'resources', lambda service: (100, 9000))
    seen = []
    def disk(worktree, svc, role='implement', movable=False):
        seen.append(movable)
        return None, None
    monkeypatch.setattr(host_ready, 'local_disk_admission', disk)
    assert service.admit_spawn(options, card=card) is None
    assert seen == [movable]


@pytest.fixture
def alternate_checkout(tmp_path):
    import subprocess
    path = tmp_path / 'alternate'
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'init'], check=True)
    return path


@pytest.mark.parametrize('queued', [False, True])
def test_unnamed_spawn_launches_on_the_admitted_drive(setup, monkeypatch, tmp_path, queued, alternate_checkout):
    from pathlib import Path
    from plangraph.spawner import host_ready
    from plangraph.spawner.service import SpawnService

    default = tmp_path / 'default'
    alternate = alternate_checkout
    service = SpawnService(state=tmp_path / 'run-state')
    service.runner.default_cwd = str(default)
    _, path, remote = setup
    path.unlink()
    monkeypatch.setattr(overflow, 'ready_records', lambda config, role: {})
    monkeypatch.setenv('PLANGRAPH_WORK_DRIVES', json.dumps([
        {'path': str(default), 'disk_floor_gb': 5}, {'path': str(alternate), 'disk_floor_gb': 5}]))
    free = {default: 1, alternate: 1 if queued else 20}
    monkeypatch.setattr(host_ready, 'disk_free_gb', lambda p: free.get(Path(p), 20))
    monkeypatch.setattr(host_ready, 'cargo_target_size_gb', lambda p: 0)
    monkeypatch.setattr(host_ready, '_cleanup_before_disk_refusal', lambda *args: None)
    monkeypatch.setattr(a, 'resources', lambda service: (0, 8000))
    monkeypatch.setattr(a, 'admission_window', lambda: 30 if queued else 0)
    recheck = Mock(side_effect=lambda delay: free.update({alternate: 20}))
    monkeypatch.setattr(a, '_sleep', recheck)

    async def launch(card=None, prompt='', *, cwd=None):
        return {'status': 'running', 'cwd': cwd}
    monkeypatch.setattr(service, '_spawn', launch)
    result = asyncio.run(service.spawn(prompt='Implement the requested change.'))
    assert result == {'status': 'running', 'cwd': str(alternate)}
    assert recheck.call_count == int(queued)
    remote.assert_not_called()


@pytest.mark.parametrize('resource', ['cargo-target', 'state', 'state/trees', 'state/worktrees'])
def test_healthy_work_drive_cannot_mask_another_filesystem_below_floor(
        setup, monkeypatch, tmp_path, resource, alternate_checkout):
    from pathlib import Path
    from plangraph.spawner import host_ready

    service, path, remote = setup
    path.unlink()
    service.state = tmp_path / 'state'
    monkeypatch.setenv('PLANGRAPH_DISK_FLOOR_GB', '5')
    target = tmp_path / 'cargo-target'
    monkeypatch.setattr(host_ready, 'shared_cargo_target', lambda: target)
    monkeypatch.setattr(host_ready, 'unsafe_cache_dir', lambda *args: None)
    monkeypatch.setattr(host_ready, 'cargo_target_size_gb', lambda p: 0)
    monkeypatch.setattr(host_ready, 'disk_free_gb', lambda p: 1 if Path(p) == tmp_path / resource else 20)
    monkeypatch.setattr(host_ready, '_cleanup_before_disk_refusal', lambda *args: None)
    monkeypatch.setattr(overflow, 'ready_records', lambda config, role: {})
    monkeypatch.setattr(a, 'resources', lambda service: (0, 8000))
    monkeypatch.setattr(a, 'admission_window', lambda: 0)
    monkeypatch.setenv('PLANGRAPH_WORK_DRIVES', json.dumps([{'path': str(alternate_checkout), 'disk_floor_gb': 5}]))

    result = service.admit_spawn({})
    assert result['status'] == 'refused'
    assert 'disk_low: free 1.00 GB, floor 5 GB' in result['reason']
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
        '--role', 'review', '--model', 'model', '--exclude-family', 'family', '--seats'])
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
    assert result['status'] == 'refused'
    assert reason in result['reason'] and '/secret' not in result['reason']
    assert remote.call_count == 2


def test_queue_timeout_keeps_the_target_lane_reason_across_readiness_refreshes(setup, monkeypatch):
    service, _, remote = setup
    service.result = Mock(side_effect=OSError('record not saved by this fixture'))  # the record stays queued
    held = 'lane_collision: demo:HELD is held by live lane run-held (run)'
    remote.side_effect = overflow.RemoteRefusal(held)
    monkeypatch.setenv(a.ADMISSION_WAIT_ENV, '0.2')
    monkeypatch.setenv(a.ADMISSION_POLL_ENV, '0.01')
    loads = []
    def stale_after_the_first_load(config, role):
        loads.append(role)  # the readiness of both hosts goes stale once the target has refused
        return {name: dict(ssh=name, ok=True, fresh=len(loads) == 1, live_runs=100,
                           mem_available_mb=6000, role_mb=2048, floor_mb=3000) for name in ('ash', 'birch')}
    monkeypatch.setattr(overflow, 'ready_records', stale_after_the_first_load)
    result = service.admit_spawn({'role': 'implement', 'prompt': 'task'}, card={'diff_ref': {'repo': service.runner.default_cwd}})
    assert result['status'] == 'refused' and 'admission window' in result['error']
    assert result['error'].startswith(held), result
    assert remote.call_count == 2 and len(loads) > 2  # no host was left to try after the refresh


def test_a_later_generic_failure_of_the_same_host_does_not_replace_its_lane_refusal(setup, monkeypatch):
    service, _, remote = setup
    service.result = Mock(side_effect=OSError('record not saved by this fixture'))  # the record stays queued
    held = 'lane_collision: demo:HELD is held by live lane run-held (run)'
    seen = set()
    def refuse(name, *args):
        # The target refuses as a lane once; every retry of that host fails before admission.
        first, seen_name = name not in seen, seen.add(name)
        raise overflow.RemoteRefusal(held if first else 'remote preparation failed before launch')
    remote.side_effect = refuse
    monkeypatch.setenv(a.ADMISSION_WAIT_ENV, '0.2')
    monkeypatch.setenv(a.ADMISSION_POLL_ENV, '0.01')
    result = service.admit_spawn({'role': 'implement', 'prompt': 'task'}, card={'diff_ref': {'repo': service.runner.default_cwd}})
    assert result['status'] == 'refused' and 'admission window' in result['error']
    assert result['error'].startswith(held), result
    assert remote.call_count > 2  # the hosts were retried after their first refusal


def test_private_card_never_copies(setup):
    service, _, remote = setup
    result = service.admit_spawn({}, card={'diff_ref': {'repo': '/private'}})
    assert result['status'] == 'refused' and 'not public' in result['reason']
    remote.assert_not_called()


def test_benchmark_not_forwarded(setup):
    service, _, remote = setup
    assert 'benchmark' in service.admit_spawn({'benchmark': object()})['reason']
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
        server = MCPSpawnerServer(state=service.state)
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
    assert result['reason'] == ('memory: free 4000 MB, floor 3000 MB, role 2048 MB'
                                '; queue: repo is not public (absent or unknown allowlist)')
    assert result['overflow_reason'] == result['reason']
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
        server = MCPSpawnerServer(state=service.state)
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
    use_projected_overflow_config(monkeypatch, path)
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


def test_remote_first_final_refusal_keeps_the_target_lane_reason_and_holder(orchestration, monkeypatch):
    from plangraph.spawner import host_ready
    service, rows, remote, _, _ = orchestration
    # The local host is unhealthy too, so the origin has no room to fall back to.
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *a, **k: (None, 'cargo_target_cap: cache full'))
    held = 'lane_collision: demo:HELD is held by live lane run-held (run)'
    remote.side_effect = overflow.RemoteRefusal(held)
    record = service.admit_spawn({'role': 'implement', 'prompt': 'task'})
    assert record['status'] == 'refused' and record['exit_code'] == 2
    assert record['error'].startswith(held), record
    assert rows['host-b']['refusal'] == held
    # A refusal that is not a lane keeps its record as it was.
    remote.side_effect = overflow.RemoteRefusal('remote copy failed')
    rows['host-b'].pop('refusal')
    assert 'error' not in service.admit_spawn({'role': 'implement', 'prompt': 'task'})


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
        'ssh': '#!/bin/sh\nfor arg do script="$arg"; done\nexec /bin/sh -c "$script"\n',
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
real_ready_records = overflow.ready_records


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
        args = ['spawn', '--state', str(service.state), '--repo', str(path.parent),
                '--card', str(card), '--prompt', str(prompt)]
    assert (overflow.main if entry == 'overflow' else cli.main)(args) == 0
    assert json.loads(capsys.readouterr().out)['placement']['host'] == 'host-b'
    remote.assert_called_once()


def test_build_review_requires_worker_with_disk_and_ram(orchestration):
    service, rows, remote, path, registry = orchestration
    config = json.loads(path.read_text())
    config['hosts']['host-c'] = {'ssh': 'host-c'}
    path.write_text(json.dumps(config))
    data = json.loads(registry.read_text())
    data['hosts'].append(dict(data['hosts'][1], id='host-c'))
    registry.write_text(json.dumps(data))
    rows['host-b'].update(disk_free_gb=4)
    rows['host-c'] = dict(rows['host-b'], ssh='host-c', disk_free_gb=30)
    card = {'review_workspace': True, 'repository_review_required': True,
            'diff_ref': {'repo': config['public_repos'][0], 'base': 'main', 'head': 'topic'}}
    options = {'role': 'review', 'prompt': 'review the exact head'}
    record = service.admit_spawn(options, card=card)
    assert record['host'] == record['placement']['host'] == 'host-c'
    assert remote.call_args.args[0] == 'host-c'
    assert remote.call_args.args[2] == card

    remote.reset_mock()
    rows['host-c']['mem_available_mb'] = 1024
    record = service.admit_spawn(options, card=card)
    assert record['status'] == 'refused'
    assert 'disk_low' in record['reason'] and 'memory:' in record['reason']
    remote.assert_not_called()


def test_dry_run_build_review_names_the_worker_host_without_spawning(orchestration):
    service, rows, remote, path, registry = orchestration
    rows['host-b']['disk_free_gb'] = 30
    config = json.loads(path.read_text())
    card = {'review_workspace': True, 'repository_review_required': True,
            'diff_ref': {'repo': config['public_repos'][0], 'base': 'main', 'head': 'topic'}}
    options = {'role': 'review', 'dry_run': True, 'prompt': 'review the exact head'}
    assert service.admit_spawn(options, card=card) is None
    assert options['_placement']['host'] == 'host-b'
    assert options['_placement']['reason']
    remote.assert_not_called()
    plain = {'role': 'review', 'dry_run': True}
    assert service.admit_spawn(plain, card={'diff_ref': card['diff_ref']}) is None
    assert '_placement' not in plain


@pytest.mark.parametrize('local,projected_only,admitted', [
    ('host-b', False, True), ('host-a', False, False), ('host-b', True, False),
])
def test_placed_build_review_uses_host_role(orchestration, monkeypatch, local, projected_only, admitted):
    service, rows, remote, path, registry = orchestration
    config = json.loads(path.read_text())
    if projected_only:
        config['hosts'][local]['orchestration_only'] = True
        data = json.loads(registry.read_text())
        next(host for host in data['hosts'] if host['id'] == local).pop('orchestration_only')
        registry.write_text(json.dumps(data))
    path.write_text(json.dumps(config))
    rows.clear()
    monkeypatch.setenv('PLANGRAPH_HOST_NAME', local)
    monkeypatch.setenv('PLANGRAPH_PLACED_HOST', local)
    options = {'role': 'review'}
    card = {'review_workspace': True, 'repository_review_required': True,
            'diff_ref': {'repo': config['public_repos'][0], 'base': 'main', 'head': 'topic'}}
    record = service.admit_spawn(options, card=card)
    if admitted:
        assert record is None
        assert options['_placement']['host'] == local
    else:
        assert record['status'] == 'refused'
        assert 'requires a registered worker host' in record['reason']
    remote.assert_not_called()


def test_build_review_refuses_remote_orchestration_route(orchestration, monkeypatch):
    service, rows, remote, path, registry = orchestration
    config = json.loads(path.read_text())
    config['hosts']['host-b']['orchestration_only'] = True
    path.write_text(json.dumps(config))
    data = json.loads(registry.read_text())
    data['hosts'][1].pop('orchestration_only')
    registry.write_text(json.dumps(data))
    monkeypatch.setattr(overflow, 'ready_records', real_ready_records)
    monkeypatch.setattr(overflow, 'read_readiness', lambda name: dict(rows[name], disk_free_gb=30))
    card = {'review_workspace': True, 'repository_review_required': True,
            'diff_ref': {'repo': config['public_repos'][0], 'base': 'main', 'head': 'topic'}}
    record = service.admit_spawn({'role': 'review'}, card=card)
    assert record['status'] == 'refused'
    assert 'host-b: review workspace requires a registered worker host' in record['reason']
    remote.assert_not_called()


def test_native_disk_refusal_is_terminal_and_notifies_named_parent(tmp_path, monkeypatch):
    from plangraph.spawner import host_ready, seat_spawn

    state = tmp_path / 'state'
    keeper = Mock()
    service = overflow.cli.SpawnService(state=state, keeper_sink=keeper)
    monkeypatch.setenv('PLANGRAPH_ADMISSION_WAIT_S', '0')
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 50000))
    monkeypatch.setattr(overflow, 'placement_policy', lambda *args: ('fixture', False))
    monkeypatch.setattr(overflow, 'placement_config', lambda *args: {'hosts': {}})
    monkeypatch.setattr(overflow, 'ready_records', lambda *args: {})
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *args, **kwargs:
                        (None, 'cargo_target_cap: cache full'))
    card = dict(run_kind='native-codex', repo=str(tmp_path), item_key='demo:A',
                parent_session='fixture-parent')

    refusal = service.admit_spawn({'role': 'implement'}, card=card)
    assert overflow.cli.spawn_exit_code(refusal) != 0
    assert refusal['status'] == 'refused' and refusal['ended'] >= refusal['started']
    event = json.loads((state / 'inbox/fixture-parent.jsonl').read_text())
    assert event['event'] == 'REFUSED' and event['run_id'] == refusal['run_id']
    assert event['item'] == 'demo:A' and 'cargo_target_cap' in event['reason']
    keeper.deliver.assert_called_once_with('keeper', event)


def test_native_queue_checks_the_lane_before_it_publishes_an_owning_record(tmp_path, monkeypatch):
    from plangraph.routing_errors import SpawnRefused
    from plangraph.spawner import host_ready, seat_spawn

    service = overflow.cli.SpawnService(state=tmp_path / 'state', keeper_sink=Mock())
    monkeypatch.setenv('PLANGRAPH_ADMISSION_WAIT_S', '60')
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 100))
    monkeypatch.setattr(overflow, 'placement_policy', lambda *args: ('fixture', False))
    monkeypatch.setattr(overflow, 'placement_config', lambda *args: {'hosts': {}})
    monkeypatch.setattr(overflow, 'ready_records', lambda *args: {})
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *args, **kwargs: (None, None))
    held = Mock(side_effect=SpawnRefused('lane_collision: demo:HELD is held by live lane run-held (run)'))
    monkeypatch.setattr(overflow, 'lane_admit', held)
    card = dict(run_kind='native-codex', repo=str(tmp_path), item_key='demo:HELD')
    with pytest.raises(SpawnRefused, match='lane_collision: demo:HELD is held by live lane run-held'):
        service.admit_spawn({'role': 'implement', 'prompt': '### demo:HELD | t\n'}, card=card)
    held.assert_called_once_with(service, card, '### demo:HELD | t\n')
    assert service.ps()['runs'] == []


def test_native_spawn_cli_returns_nonzero_for_zero_exit_refusal(tmp_path, monkeypatch, capsys):
    receipt = dict(status='refused', exit_code=0, reason='cargo_target_cap: cache full')
    child = Mock(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(receipt) + '\n'))
    monkeypatch.setattr(overflow.cli, '_start_supervisor', Mock(return_value=child))
    card = json.dumps(dict(run_kind='native-codex', repo=str(tmp_path)))
    assert overflow.cli.main(['--state', str(tmp_path), 'spawn', '--card', card, 'fixture']) == 1
    assert json.loads(capsys.readouterr().out) == receipt


def test_placed_native_writer_retries_a_cleared_cache_cap(tmp_path, monkeypatch):
    from plangraph.spawner import host_ready, seat_spawn

    service = overflow.cli.SpawnService(state=tmp_path / 'state', keeper_sink=Mock())
    monkeypatch.setenv('PLANGRAPH_PLACED_HOST', 'fixture')
    monkeypatch.setenv('PLANGRAPH_ADMISSION_WAIT_S', '60')
    monkeypatch.setattr(seat_spawn, '_sleep', lambda _: None)
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 50000))
    monkeypatch.setattr(overflow, 'placement_policy', lambda *args: ('fixture', False))
    monkeypatch.setattr(overflow, 'placement_config', lambda *args: {'hosts': {}})
    monkeypatch.setattr(overflow, 'ready_records', lambda *args: {})
    monkeypatch.setattr(overflow, 'lane_admit', lambda service, card, prompt: card)  # no pg-spawnd on the runner
    disk = Mock(side_effect=[(None, 'cargo_target_cap: cache full'), (None, None)])
    monkeypatch.setattr(host_ready, 'local_disk_admission', disk)
    options = {'role': 'implement'}
    card = dict(run_kind='native-codex', repo=str(tmp_path))
    assert service.admit_spawn(options, card=card) is None
    assert disk.call_count == 2
    assert options['_placement']['host'] == 'fixture'
    assert service.ps()['runs'] == []


def test_native_shared_cache_cap_scales_and_probes_symlink_target(tmp_path, monkeypatch):
    from plangraph.spawner import host_ready as h

    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'target-link'
    link.symlink_to(target)
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(link))
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_CAP_GB', '12')
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 20)
    probe = Mock(return_value=10)
    monkeypatch.setattr(h, 'disk_free_gb', probe)
    monkeypatch.setattr(h, 'cargo_run_busy', lambda row: True)
    service = SimpleNamespace(ps=lambda: {'runs': [dict(role='implement', status='running')]})
    assert h.local_disk_refusal(tmp_path / 'checkout', service, native=True) is None
    probe.assert_any_call(target)


def test_final_native_target_refusal_keeps_the_target_run(orchestration, monkeypatch, tmp_path):
    from plangraph.spawner import host_ready

    service, rows, remote, _, _ = orchestration
    rows['host-b'].update(disk_free_gb=30, sandbox_ok=True)
    monkeypatch.setattr(host_ready, 'local_disk_admission', lambda *args, **kwargs:
                        (None, 'cargo_target_cap: local cache full'))
    monkeypatch.setattr(overflow, 'worker_host_refusal', lambda *args: None)
    target = dict(run_id='run-target', run_kind='native-codex', host='host-b',
                  status='refused', ended=2, exit_code=2, reason='cargo_target_cap: target cache full')
    refusal = overflow.RemoteRefusal(target['reason'])
    refusal.receipt = target
    remote.side_effect = refusal

    card = dict(run_kind='native-codex', repo=str(tmp_path), item_key='demo:A')
    record = service.admit_spawn({'role': 'implement', 'prompt': 'fixture'}, card=card)

    remote.assert_called_once()
    assert record['run_id'] == target['run_id']
    assert record['host'] == record['placement']['host'] == 'host-b'
    assert record['origin_receipt'] is True
    assert record['ended'] == target['ended'] and record['reason'] == target['reason']
    assert [run['run_id'] for run in service.ps()['runs']] == [target['run_id']]


@pytest.mark.parametrize('queued', [False, True])
def test_final_native_lane_refusal_survives_a_later_generic_target_refusal(
        orchestration, monkeypatch, tmp_path, queued):
    from plangraph.spawner import host_ready

    service, rows, remote, config_path, registry_path = orchestration
    config, registry = map(lambda path: json.loads(path.read_text()), (config_path, registry_path))
    config['hosts']['host-c'] = {'ssh': 'host-c'}
    registry['hosts'].append(dict(registry['hosts'][-1], id='host-c'))
    registry['hosts'][0]['orchestration_only'] = not queued
    config_path.write_text(json.dumps(config))
    registry_path.write_text(json.dumps(registry))
    rows['host-b'].update(disk_free_gb=30, sandbox_ok=True)
    rows['host-c'] = dict(rows['host-b'], ssh='host-c', mem_available_mb=8500)
    monkeypatch.setattr(host_ready, 'local_disk_admission',
                        lambda *args, **kwargs: (None, 'cargo_target_cap: local cache full'))
    monkeypatch.setattr(overflow, 'worker_host_refusal', lambda *args: None)
    held = 'lane_collision: demo:A is held by live lane run-owner (run)'
    first = dict(run_id='run-lane', host='host-b', error=held)
    second = dict(run_id='run-cap', host='host-c', error='cargo_target_cap: target cache full')
    for receipt in (first, second):
        receipt.update(run_kind='native-codex', status='refused', ended=2, exit_code=2)
    remote.side_effect = [overflow.RemoteRefusal(held, receipt=first),
                          overflow.RemoteRefusal('target rejected admission', receipt=second)]
    if queued:
        monkeypatch.setattr(overflow, 'native_queue_card', lambda service, card, options: card)
        monkeypatch.setattr(overflow, 'ready_records', Mock(side_effect=[rows, {}]))
        clock = SimpleNamespace(now=0)
        monkeypatch.setattr(a, 'time', SimpleNamespace(time=lambda: clock.now))
        monkeypatch.setattr(a, '_sleep', lambda _: setattr(clock, 'now', a.admission_window()))

    card = dict(run_kind='native-codex', repo=str(tmp_path), item_key='demo:A')
    record = service.admit_spawn({'role': 'implement', 'prompt': 'fixture'}, card=card)

    assert [call.args[0] for call in remote.call_args_list] == ['host-b', 'host-c']
    assert record['run_id'] == 'run-lane' and record['error'] == held
    assert record['host'] == record['placement']['host'] == 'host-b'
    assert record['origin_receipt'] is True
    assert [run['run_id'] for run in service.ps()['runs']] == ['run-lane']
