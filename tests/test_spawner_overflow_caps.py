import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from plangraph.spawner import overflow as o
from plangraph.spawner.service import SpawnService
from test_spawner_overflow import healthy, inputs  # noqa: F401 shared fixture


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
    assert o.cli.main(['--supervise']) == 2
    result = json.loads(output.getvalue())
    assert reason in result['reason'] and result['status'] == 'refused'


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


@pytest.mark.parametrize('orchestration', [False, True])
def test_overflow_local_cli_preserves_caller_state(inputs, monkeypatch, capsys, orchestration):
    root, _, args = inputs
    monkeypatch.setattr(o, 'local_resources', lambda state: (0, 9000))
    monkeypatch.setattr(o, 'placement_policy', lambda *a: ('invented-local', orchestration))
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    monkeypatch.setattr(o.cli, 'SpawnService', SpawnService)

    async def spawn(service, **options):
        assert service.state == root / 'state'
        assert options['card'] == json.loads((root / 'card.json').read_text())
        assert options['prompt'] == 'a prompt'
        record = dict(run_id='run-local', status='dry_run', exit_code=0, ended=1001)
        service._save(record)
        return record

    monkeypatch.setattr(SpawnService, 'spawn', spawn)
    assert o.main(args + ['--dry-run', '--leg', 'invented']) == 0
    record = json.loads(capsys.readouterr().out)
    assert SpawnService(state=root / 'state').result(record['run_id']) == record
    assert not (root / 'default-state').exists()


def test_overflow_defaults_to_service_state(inputs, monkeypatch):
    root, _, args = inputs
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    index = args.index('--state')
    monkeypatch.setattr(o, 'local_resources', lambda state=None: (0, 9000))
    monkeypatch.setattr(o, 'placement_policy', lambda *a: ('local', False))
    def local(argv):
        assert '--state' not in argv
        assert SpawnService().state == root / 'default-state'
        return 0
    monkeypatch.setattr(o.cli, 'main', local)
    assert o.main(args[:index] + args[index + 2:]) == 0


def test_overflow_remote_does_not_forward_local_state(inputs, monkeypatch, capsys):
    root, path, args = inputs
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': {'ash': {}}}))
    monkeypatch.setattr(o, 'ready_records', lambda *a: {'ash': healthy()})
    remote = Mock(return_value=dict(run_id='run-remote', status='running'))
    monkeypatch.setattr(o, 'remote_spawn', remote)
    assert o.main(args + ['--role', 'review']) == 0
    assert remote.call_args.args[-1] == ['--role', 'review']
    assert json.loads(capsys.readouterr().out)['host'] == 'ash'


@pytest.mark.parametrize('detail', ['private repo', 'malformed hosts', 'refused: retry limit'])
def test_nonmemory_refusal_reason_and_caller_state(inputs, monkeypatch, capsys, detail):
    root, path, args = inputs
    path.write_text('{}')
    monkeypatch.setattr(o, 'decide', lambda *a, **kw: o.Decision(None, detail))
    monkeypatch.setattr(o, 'memory_refusal', lambda *a: None)
    monkeypatch.setattr(o, 'ready_records', lambda *a: {
        'ash': {'refusal': 'remote spawn failed'}, 'birch': {'refusal': 'remote copy failed'}})
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    assert o.main(args) == 2
    output = capsys.readouterr()
    record = json.loads(output.out)
    expected = detail + '; ash: remote spawn failed; birch: remote copy failed'
    assert record['reason'] == record['error'] == record['overflow_reason'] == expected
    assert output.err.strip() == expected
    assert 'host_memory_floor' not in output.out + output.err
    assert SpawnService(state=root / 'state').result(record['run_id']) == record
    assert not (root / 'default-state').exists()


def test_overflow_retry_limit_preserves_host_refusals(inputs, monkeypatch, capsys):
    root, path, args = inputs
    names = ('ash', 'birch', 'cedar')
    path.write_text(json.dumps({'public_repos': [str(root)], 'hosts': dict.fromkeys(names, {})}))
    monkeypatch.setattr(o, 'ready_records', lambda *a: {name: healthy() for name in names})
    remote = Mock(side_effect=[None, o.RemoteRefusal('remote copy failed')])
    monkeypatch.setattr(o, 'remote_spawn', remote)
    assert o.main(args) == 2
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record['status'] == 'refused' and record['reason'] == 'host_memory_floor'
    assert record['exit_code'] == 2 and record['ended'] is not None
    assert (record['mem_available_mb'], record['floor_mb'], record['role_mb']) == (4000, 3000, 2048)
    for text in ('refused: retry limit', 'ash: remote spawn failed', 'birch: remote copy failed'):
        assert text in record['overflow_reason'] and text in record['error'] and text in output.err
    assert [call.args[0] for call in remote.call_args_list] == ['ash', 'birch']
    assert SpawnService(state=root / 'state').result(record['run_id']) == record


@pytest.mark.parametrize('free,refused', [(4.99, True), (5, False), (5.01, False)])
def test_disk_floor_admission(tmp_path, monkeypatch, free, refused):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h, seat_spawn
    monkeypatch.delenv('PLANGRAPH_DISK_FLOOR_GB')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(tmp_path / 'shared'))
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: free)
    monkeypatch.setattr(seat_spawn, 'resources', lambda service: (0, 99999))
    monkeypatch.setattr(o, 'registry_caps', lambda: {})
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(tmp_path / 'absent'))
    service = SimpleNamespace(runner=SimpleNamespace(default_cwd=str(tmp_path / 'checkout')),
                              ps=lambda: {'runs': []})
    result = seat_spawn.admit(service, {})
    assert (result is not None) == refused
    if refused:
        assert 'disk_low' in result['reason'] and '4.99' in result['reason']


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
    reason = h.local_disk_refusal(tmp_path / 'checkout', service)
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
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_CAP_GB', '12')
    monkeypatch.setenv('PLANGRAPH_CARGO_CLEANUP_WAIT_S', '0')
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 13)
    service = SimpleNamespace(ps=lambda: {'runs': []})
    with h.cargo_lease():
        assert 'cargo_target_cap' in h.local_disk_refusal(tmp_path / 'checkout', service)
        assert (target / 'artifact').exists()
    assert h.local_disk_refusal(tmp_path / 'checkout', service) is None
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
    assert 'disk_low: free 5.00 GB' in h.local_disk_refusal(tmp_path / 'checkout', service)


def test_remote_low_disk_uses_next_host():
    rows = {name: dict(ssh=name, ok=True, fresh=True, live_runs=0,
                      mem_available_mb=10000, role_mb=2048, floor_mb=3000,
                      disk_free_gb=free) for name, free in [('low', 4), ('room', 5)]}
    assert o.decide(10000, 2048, 3000, rows, True,
                    local_refusal='disk_low: free 20 GB').host == 'room'
    decision = o.decide(10000, 2048, 3000, {'low': rows['low']}, True,
                        local_refusal='disk_low: free 20 GB')
    assert decision.host is None and 'disk_low: free 4 GB' in decision.reason


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


@pytest.mark.parametrize('kind', ['inside', 'ancestor', 'home-link', 'source-link'])
def test_cache_cleanup_rejects_source_paths(tmp_path, monkeypatch, kind):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    worktree = tmp_path / 'checkout'
    worktree.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    if kind == 'inside':
        target = worktree / 'src'
        target.mkdir()
    elif kind == 'ancestor':
        target = tmp_path
    elif kind == 'home-link':
        link = tmp_path / 'home-alias'
        link.symlink_to(home, target_is_directory=True)
        monkeypatch.setenv('HOME', str(link))
        target = home
    else:
        link = tmp_path / 'source-alias'
        link.symlink_to(worktree, target_is_directory=True)
        target = link / 'src'
        target.mkdir()
    artifact = target / 'precious'
    artifact.write_text('source')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 13)
    reason = h.local_disk_refusal(worktree, SimpleNamespace(ps=lambda: {'runs': []}))
    assert reason and 'configuration unavailable' in reason
    assert artifact.read_text() == 'source'

@pytest.mark.parametrize('source', ['argument', 'card'])
def test_local_disk_uses_launch_cwd(tmp_path, monkeypatch, source):
    from plangraph.spawner import host_ready as h
    root = tmp_path
    card = root / 'card.json'
    card.write_text('{}')
    prompt = root / 'prompt.txt'
    prompt.write_text('task')
    args = ['spawn', '--repo', str(root), '--card', str(card), '--prompt', str(prompt)]
    monkeypatch.setattr(o, 'registry_caps', lambda: {})
    monkeypatch.setenv('PLANGRAPH_OVERFLOW_HOSTS', str(root / 'missing'))
    cwd = str(root / 'other-filesystem')
    o.Path(cwd).mkdir()
    o.subprocess.run(['git', 'init', '-q', cwd], check=True)
    o.subprocess.run(['git', '-C', cwd, '-c', 'user.name=Test', '-c',
                      'user.email=test@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
    if source == 'argument':
        args += ['--cwd', cwd]
    else:
        card = root / 'card.json'
        card.write_text(json.dumps({'cwd': cwd}))
    monkeypatch.setattr(o, 'local_resources', lambda: (0, 6000))
    seen = []
    def disk(worktree, service):
        seen.append(worktree)
        return None if worktree == cwd else 'disk_low'
    monkeypatch.setattr(h, 'local_disk_refusal', disk)
    monkeypatch.setattr(o.cli, 'main', lambda argv: 3)
    assert o.main(args) == 3
    assert seen == [cwd]


def test_disk_low_runs_cleanup_once_and_raises_floor_alarm(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    monkeypatch.setenv('PLANGRAPH_DISK_FLOOR_GB', '5')
    target = tmp_path / 'shared'
    target.mkdir()
    (target / 'artifact').write_text('stale build')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    state = tmp_path / 'state'
    state.mkdir()
    service = SimpleNamespace(state=state, ps=lambda: {'runs': []})
    free_values = [4.0]
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: free_values[0])

    # When cleanup does not free enough disk, refusal happens after 1 cleanup and records alarm.
    reason = h.local_disk_refusal(tmp_path / 'checkout', service)
    assert reason and reason.startswith('disk_low:')
    assert not target.exists()
    alarm = json.loads((state / 'disk-hygiene.json').read_text())
    assert alarm['alarm'] is True

    # When cleanup raises free space >= floor, admission succeeds without refusal.
    target.mkdir()
    (target / 'artifact').write_text('stale build')
    def cleanup_frees(worktree, svc):
        free_values[0] = 6.0
        return True
    monkeypatch.setattr(h, '_cleanup_before_disk_refusal', cleanup_frees)
    free_values[0] = 4.0
    assert h.local_disk_refusal(tmp_path / 'checkout', service) is None


def test_cleanup_touches_only_the_pinned_cache_roots(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    home = tmp_path / 'home'
    real = home / '.cache/pg-target-po'
    real.mkdir(parents=True)
    (real / 'artifact').write_text('real host build state')
    monkeypatch.setenv('HOME', str(home))
    pinned = tmp_path / 'pinned-po'
    pinned.mkdir()
    (pinned / 'artifact').write_text('rebuildable')
    monkeypatch.setattr(h, '_legacy_cache_roots', lambda: [pinned])
    target = tmp_path / 'shared'
    target.mkdir()
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 100)
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 99)
    service = SimpleNamespace(ps=lambda: {'runs': []})
    assert h.local_disk_refusal(tmp_path / 'checkout', service) is None
    assert not pinned.exists()
    assert (real / 'artifact').read_text() == 'real host build state'


def test_pool_slots_are_cache_roots_cleaned_over_the_cap(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import host_ready as h
    target = tmp_path / 'cache/cargo-target'
    slot = tmp_path / 'cache/cargo-target-pool/slot-1'
    for path in (target, slot):
        path.mkdir(parents=True)
        (path / 'artifact').write_text('x')
    (tmp_path / 'cache/cargo-target-pool/slot-1.lease.json').write_text('{}')
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(target))
    assert h._shared_cache_roots()[:1] == [target]
    assert slot in h._shared_cache_roots()
    assert not any(str(p).endswith('.lease.json') for p in h._shared_cache_roots())
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_CAP_GB', '12')
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 100)
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 13 if path == slot else 1)
    assert h.local_disk_refusal(tmp_path / 'checkout', SimpleNamespace(ps=lambda: {'runs': []})) is None
    assert not slot.exists() and (target / 'artifact').exists()


def test_cap_default_is_one_value_shared_with_the_rust_pool(monkeypatch):
    import re
    from pathlib import Path
    from plangraph.spawner import host_ready as h
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_CAP_GB', raising=False)
    source = (Path(__file__).parents[1] / 'rust/worker-core/src/cargo_pool.rs').read_text()
    rust = int(re.search(r'DEFAULT_CARGO_TARGET_CAP_GB: u64 = (\d+);', source).group(1))
    assert h.cargo_target_cap_gb() == rust == h.DEFAULT_CARGO_TARGET_CAP_GB


def _drive_state(monkeypatch, free):
    from plangraph.spawner import host_ready as h
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: free[path])
    return h


def test_select_work_drive_cleans_once_then_picks_or_refuses(monkeypatch):
    free = {'/d1': 4.0, '/d2': 4.5}
    h = _drive_state(monkeypatch, free)
    calls = []
    def cleanup(freed=None):
        calls.append(1)
        free.update(freed or {})
    drives = [('/d1', 5.0), ('/d2', 5.0)]
    assert h.select_work_drive(drives, lambda: cleanup({'/d2': 6.0})) == ('/d2', None)
    assert calls == [1]
    free.update({'/d1': 4.0, '/d2': 4.5})
    assert h.select_work_drive(drives, cleanup) == (None, (4.0, 5.0))
    assert calls == [1, 1]
    free.update({'/d1': 9.0})
    assert h.select_work_drive(drives, lambda: pytest.fail('no cleanup needed')) == ('/d1', None)


def test_local_work_drives_branch_admits_selected_drive_and_records_alarm(tmp_path, monkeypatch):
    from types import SimpleNamespace
    free = {'/d1': 4.0, '/d2': 8.0}
    h = _drive_state(monkeypatch, free)
    monkeypatch.setenv('PLANGRAPH_WORK_DRIVES', json.dumps(
        [{'path': '/d1', 'disk_floor_gb': 5}, {'path': '/d2', 'disk_floor_gb': 5}]))
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 0)
    state = tmp_path / 'state'
    service = SimpleNamespace(state=state, ps=lambda: {'runs': []})
    assert h.local_disk_refusal(tmp_path / 'checkout', service) is None
    free['/d2'] = 4.5
    reason = h.local_disk_refusal(tmp_path / 'checkout', service)
    assert reason == 'disk_low: free 4.00 GB, floor 5 GB'
    assert json.loads((state / 'disk-hygiene.json').read_text())['alarm'] is True


def test_remote_work_drives_selects_eligible_drive_or_overflows(tmp_path):
    low_drive_only = dict(
        ssh='a', ok=True, fresh=True, live_runs=0, mem_available_mb=10000,
        role_mb=2048, floor_mb=3000, disk_free_gb=2, disk_floor_gb=5,
        work_drives=[{'path': '/d1', 'disk_free_gb': 2, 'disk_floor_gb': 5}],
    )
    second_drive_ok = dict(
        ssh='b', ok=True, fresh=True, live_runs=0, mem_available_mb=10000,
        role_mb=2048, floor_mb=3000, disk_free_gb=2, disk_floor_gb=5,
        work_drives=[
            {'path': '/d1', 'disk_free_gb': 2, 'disk_floor_gb': 5},
            {'path': '/d2', 'disk_free_gb': 12, 'disk_floor_gb': 5},
        ],
    )
    assert o.decide(10000, 2048, 3000, {'a': low_drive_only, 'b': second_drive_ok}, True,
                    local_refusal='disk_low: free 2 GB').host == 'b'
    no_host = o.decide(10000, 2048, 3000, {'a': low_drive_only}, True,
                       local_refusal='disk_low: free 2 GB')
    assert no_host.host is None and 'disk_low' in no_host.reason
