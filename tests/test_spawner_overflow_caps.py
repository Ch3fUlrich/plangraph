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
