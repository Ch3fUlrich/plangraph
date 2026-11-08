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
        {'id': name, 'aliases': [name + '-alias'], 'caps': caps}
        for name in ('local', 'remote')]}))


def test_present_caps(registry):
    write_registry(registry, {'max_runs': 10, 'mem_floor_mb': 4096})
    data = o.registry_caps()
    assert o.local_limits(data) == (10, 4096)
    assert o.host_caps(data, 'REMOTE-ALIAS') == (10, 4096)


@pytest.mark.parametrize('text', [None, 'not json', '{}', '{"hosts": null}', '[]'])
def test_missing_or_bad_registry(registry, caplog, text):
    if text is not None:
        registry.write_text(text)
    data = o.registry_caps()
    assert o.local_limits(data) == (4, 3000)
    assert o.host_caps(data, 'remote') is None
    assert 'defaults' in caplog.text


@pytest.mark.parametrize('caps', [None, {}, {'max_runs': 10},
    {'max_runs': '10', 'mem_floor_mb': 4096},
    {'max_runs': True, 'mem_floor_mb': 4096},
    {'max_runs': 10, 'mem_floor_mb': '4096'},
    {'max_runs': 10, 'mem_floor_mb': False},
    {'max_runs': 0, 'mem_floor_mb': 4096},
    {'max_runs': 10, 'mem_floor_mb': -1}])
def test_missing_or_bad_caps(registry, caplog, caps):
    write_registry(registry, caps)
    data = o.registry_caps()
    assert o.local_limits(data) == (4, 3000)
    assert o.host_caps(data, 'remote') is None
    assert 'missing or malformed registry caps' in caplog.text


@pytest.mark.parametrize('live,memory,chosen', [(9, 4096, 'remote'), (10, 9000, None),
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
    assert records['remote']['run_cap'] == 10
    assert o.decide(10, 9000, *o.local_limits(o.registry_caps()), records, True).host == chosen
    registry.unlink()
    assert o.decide(4, 9000, 4, 3000, o.ready_records(config), True).host is None
