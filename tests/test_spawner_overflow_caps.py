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
"""Standalone overflow refusal detail and invoking-state regressions."""
import json
from unittest.mock import Mock

import pytest

from plangraph.spawner import overflow as o
from plangraph.spawner.service import SpawnService
from test_spawner_overflow import healthy, inputs


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


def test_overflow_requires_caller_state(inputs, monkeypatch, capsys):
    root, _, args = inputs
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(root / 'default-state'))
    index = args.index('--state')
    with pytest.raises(SystemExit) as exc:
        o.main(args[:index] + args[index + 2:])
    assert exc.value.code == 2 and '--state' in capsys.readouterr().err
    assert not (root / 'default-state').exists()


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
    monkeypatch.setattr(o, 'decide', lambda *a: o.Decision(None, detail))
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
