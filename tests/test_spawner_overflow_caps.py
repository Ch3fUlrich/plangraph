import json
from datetime import datetime, timezone

import pytest

from plangraph.spawner import overflow as o


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.delenv('PLANGRAPH_HOST_NAME', raising=False)
    path = o.config_dir() / 'hosts.json'
    path.parent.mkdir(parents=True)
    return path


def write_registry(path, caps):
    path.write_text(json.dumps({'self': 'local', 'hosts': [
        {'id': name, 'aliases': [name + '-alias'], 'caps': caps,
         'warmup': {'ramp_mb': {'implement': 2048, 'review': 4096}}}
        for name in ('local', 'remote')]}))


def test_present_caps(registry):
    write_registry(registry, {'max_runs': 10, 'mem_floor_mb': 4096})
    data = o.registry_caps()
    assert o.local_limits(data) == (2048, 4096)
    assert o.host_caps(data, 'REMOTE-ALIAS') == (2048, 4096)
    assert o.host_caps(data, 'remote', 'unknown') == (4096, 4096)


@pytest.mark.parametrize('text', [None, 'not json', '{}', '{"hosts": null}', '[]'])
def test_missing_or_bad_registry(registry, caplog, text):
    if text is not None:
        registry.write_text(text)
    data = o.registry_caps()
    assert o.local_limits(data) == (2048, 3000)
    assert o.host_caps(data, 'remote') is None
    assert 'defaults' in caplog.text


@pytest.mark.parametrize('caps', [None, {}, {'max_runs': 10},
    {'max_runs': 10, 'mem_floor_mb': '4096'},
    {'max_runs': 10, 'mem_floor_mb': False},
    {'max_runs': 10, 'mem_floor_mb': -1}])
def test_missing_or_bad_caps(registry, caplog, caps):
    write_registry(registry, caps)
    data = o.registry_caps()
    assert o.local_limits(data) == (2048, 3000)
    assert o.host_caps(data, 'remote') is None
    assert 'missing or malformed registry caps' in caplog.text


@pytest.mark.parametrize('live,memory,chosen', [(50, 6144, 'remote'), (10, 6143, None),
                                              (0, 4095, None)])
def test_registry_admission(registry, live, memory, chosen):
    write_registry(registry, {'max_runs': 10, 'mem_floor_mb': 4096})
    directory = o.Path.home() / '.local/state/plangraph/host-ready'
    directory.mkdir(parents=True)
    (directory / 'remote.json').write_text(json.dumps(dict(
        host='remote', ok=True, live_runs=live, mem_available_mb=memory,
        checked_at=datetime.now(timezone.utc).isoformat())))
    config = {'hosts': {'remote': {'ssh': 'placeholder', 'run_cap': 99, 'floor_mb': 0}}}
    records = o.ready_records(config)
    assert records['remote']['role_mb'] == 2048
    assert o.decide(4000, *o.local_limits(o.registry_caps()), records, True).host == chosen
    registry.unlink()
    assert o.decide(4000, 2048, 3000, o.ready_records(config), True).host is None


def test_headroom_ranking():
    def row(free, floor, live):
        return dict(ssh='fake', ok=True, fresh=True, live_runs=live,
                    mem_available_mb=free, floor_mb=floor, role_mb=2048)
    rows = {'ash': row(10000, 7000, 0), 'birch': row(9000, 3000, 50)}
    assert o.decide(4000, 2048, 3000, rows, True).host == 'birch'


@pytest.mark.parametrize('ramp', [None, {}, {'implement': 0}, {'review': '2048'}])
def test_bad_ramp(registry, ramp):
    registry.write_text(json.dumps({'self': 'local', 'hosts': [
        {'id': 'local', 'caps': {'mem_floor_mb': 3000}, 'warmup': {'ramp_mb': ramp}}]}))
    assert o.local_limits(o.registry_caps()) == (2048, 3000)
    assert o.host_caps(o.registry_caps(), 'local') is None


def test_cli_shared_ram_refusal(registry, monkeypatch, tmp_path):
    import io
    write_registry(registry, {'max_runs': 10, 'max_writers': 1, 'mem_floor_mb': 3000})
    original = o.Path.read_text
    monkeypatch.setattr(o.Path, 'read_text', lambda p, *a, **kw:
        'MemAvailable: 5168128 kB\n' if str(p) == '/proc/meminfo' else original(p, *a, **kw))
    service = o.cli.SpawnService(state=tmp_path / 'state')
    monkeypatch.setattr(service, 'route', lambda *args, **kwargs: None)
    monkeypatch.setattr(service, 'ps', lambda: {'runs': [{'status': 'running'}] * 50})
    monkeypatch.setattr(o.cli, 'SpawnService', lambda **kw: service)
    reason = 'memory: free 5047 MB, floor 3000 MB, role 2048 MB'
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(tmp_path / 'absent.json'))
    class Output(io.StringIO):
        def close(self):
            pass
    output = Output()
    monkeypatch.setattr(o.cli.sys, 'stdout', output)
    monkeypatch.setattr(o.cli.sys, 'stdin', io.StringIO(json.dumps({
        'state': str(tmp_path / 'state'), 'card': {'task': 'test'}, 'prompt': 'prompt'}) + '\n'))
    assert o.cli.main(['--supervise']) == 0
    result = json.loads(output.getvalue())
    assert reason in result['reason'] and result['status'] == 'queued'


@pytest.mark.parametrize('total,free,used', [(0, 0, 0), (1000, 100, 90), (1000, 99, 90.1)])
def test_swap_used_reader(monkeypatch, total, free, used):
    monkeypatch.setattr(o.Path, 'read_text', lambda self:
                        f'SwapTotal: {total} kB\nSwapFree: {free} kB\n')
    assert o.swap_used_pct() == used


@pytest.mark.parametrize('text', ['', 'SwapTotal: 1000 kB\n', 'SwapFree: 100 kB\n',
                                   'SwapTotal:\nSwapFree: 0 kB\n'])
def test_missing_swap_readings_have_named_refusal(monkeypatch, text):
    monkeypatch.setattr(o.Path, 'read_text', lambda self: text)
    assert o.local_health_refusal({}, 8000) == 'local_swap_unavailable'
    with pytest.raises(ValueError, match='swap readings'):
        o.swap_used_pct()


@pytest.mark.parametrize('status,exit_code,expected', [
    ('queued', 5, 0), ('running', None, 0), ('dry_run', 0, 0),
    ('completed', 0, 0), ('failed', 5, 5), ('failed', 0, 1)])
def test_spawn_exit_code(status, exit_code, expected):
    from plangraph.spawner.base_fresh import spawn_exit_code
    assert spawn_exit_code(dict(status=status, exit_code=exit_code)) == expected


@pytest.mark.parametrize('free,refused', [(29, True), (30, False), (31, False)])
def test_disk_floor_admission(tmp_path, monkeypatch, free, refused):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h, seat_spawn
    monkeypatch.delenv('PLANGRAPH_DISK_FLOOR_GB')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(tmp_path / 'shared'))
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: free)
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 99999))
    monkeypatch.setattr(o, 'registry_caps', lambda: {})
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(tmp_path / 'absent'))
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path)),
                              ps=lambda: {'runs': []})
    result = seat_spawn.admit(service, {})
    assert (result is not None) == refused
    if refused:
        assert 'disk_low' in result['reason'] and '29' in result['reason']


@pytest.mark.parametrize('busy', [False, True])
def test_shared_target_cap_idle_clears_busy_refuses(tmp_path, monkeypatch, busy):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    target = tmp_path / 'shared'
    target.mkdir()
    (target / 'artifact').write_text('cached build')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_CAP_GB', '12')
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 100)
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 13)
    service = SimpleNamespace(ps=lambda: {'runs': [{'status': 'running'}] if busy else []})
    reason = h.local_disk_refusal(tmp_path, service)
    assert bool(reason) == busy
    if busy:
        assert 'cargo_target_cap' in reason and '13' in reason
    assert (target / 'artifact').exists() == busy


def test_shared_target_env_overrides_worktree_target(tmp_path, monkeypatch):
    from plangraph.spawner.host_ready import cargo_child_env
    from plangraph.spawner.claude_env import claude_child_env
    target = tmp_path / 'shared'
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    env = cargo_child_env({'CARGO_TARGET_DIR': str(tmp_path / 'worktree/rust/target')})
    assert env['CARGO_TARGET_DIR'] == str(target)
    assert claude_child_env(env)['CARGO_TARGET_DIR'] == str(target)


def test_cache_lease_blocks_idle_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    target = tmp_path / 'shared'
    target.mkdir()
    (target / 'artifact').write_text('keep active build')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 13)
    service = SimpleNamespace(ps=lambda: {'runs': []})
    with h.cargo_lease():
        assert 'cargo_target_cap' in h.local_disk_refusal(tmp_path, service)
        assert (target / 'artifact').exists()
    assert h.local_disk_refusal(tmp_path, service) is None
    assert not target.exists()


def test_disk_measurement_covers_configured_worktree_filesystem(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    from plangraph import settings
    root = tmp_path / 'other-filesystem'
    monkeypatch.setattr(settings, '_policy_data', lambda repo: {'worktree_root': str(root)})
    monkeypatch.setenv('PLANGRAPH_DISK_FLOOR_GB', '30')
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 5 if path == str(root) else 100)
    service = SimpleNamespace(state=tmp_path / 'state')
    assert 'disk_low: free 5.00 GB' in h.local_disk_refusal(tmp_path, service)


def test_remote_low_disk_uses_next_host():
    rows = {name: dict(ssh=name, ok=True, fresh=True, live_runs=0,
                      mem_available_mb=10000, role_mb=2048, floor_mb=3000,
                      disk_free_gb=free) for name, free in [('low', 29), ('room', 40)]}
    assert o.decide(10000, 2048, 3000, rows, True,
                    local_refusal='disk_low: free 20 GB').host == 'room'
    decision = o.decide(10000, 2048, 3000, {'low': rows['low']}, True,
                        local_refusal='disk_low: free 20 GB')
    assert decision.host is None and 'disk_low: free 29 GB' in decision.reason


def test_shared_target_defaults_to_host_cache(tmp_path, monkeypatch):
    from plangraph.spawner.host_ready import cargo_child_env
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR')
    monkeypatch.setenv('HOME', str(tmp_path))
    assert cargo_child_env({})['CARGO_TARGET_DIR'] == str(tmp_path / '.cache/plangraph/cargo-target')


@pytest.mark.parametrize('field', ['disk_free_gb', 'disk_floor_gb'])
@pytest.mark.parametrize('bad', ['not numeric', {}, False, -1, float('nan'), float('inf')])
def test_bad_disk_readiness_skips_host(field, bad):
    low = dict(ssh='bad', ok=True, fresh=True, live_runs=0, mem_available_mb=12000,
               role_mb=2048, floor_mb=3000, disk_free_gb=40, disk_floor_gb=30)
    good = dict(low, ssh='good')
    low[field] = bad
    decision = o.decide(4000, 2048, 3000, {'bad': low, 'good': good}, True)
    assert decision.host == 'good'
    refused = o.decide(4000, 2048, 3000, {'bad': low}, True)
    assert refused.host is None and 'malformed readiness' in refused.reason


def test_cache_scan_ignores_vanished_build_artifacts(tmp_path, monkeypatch):
    from plangraph.spawner import host_ready as h
    def racing_walk(path, onerror):
        onerror(FileNotFoundError('incremental directory was renamed'))
        yield str(path), [], ['removed-during-build']
    monkeypatch.setattr(h.os, 'walk', racing_walk)
    assert h.cargo_target_size_gb(tmp_path) == 0


def test_cache_scan_does_not_ignore_permission_errors(tmp_path, monkeypatch):
    from plangraph.spawner import host_ready as h
    def unreadable_walk(path, onerror):
        onerror(PermissionError('cache unreadable'))
        return iter(())
    monkeypatch.setattr(h.os, 'walk', unreadable_walk)
    with pytest.raises(PermissionError):
        h.cargo_target_size_gb(tmp_path)
