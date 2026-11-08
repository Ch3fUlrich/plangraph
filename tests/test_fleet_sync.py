import json
from pathlib import Path

import jsonschema
import pytest

from plangraph.fleet_sync import drift, genconfig, release
from plangraph.spawner import overflow, remote_ps


def test_fleet_sync_release_private_atomic(tmp_path, monkeypatch):
    path = tmp_path / 'release.json'
    def query(endpoint):
        if endpoint.endswith('required_status_checks'):
            return {'contexts': ['ci']}
        if 'check-runs' in endpoint:
            return {'check_runs': [{'name': 'ci', 'status': 'completed', 'conclusion': 'success'}]}
        return {'statuses': []}
    import plangraph.routing_support.private_io as io
    replace = io.os.replace
    seen = []
    def observe(source, target):
        assert Path(source).stat().st_mode & 0o777 == 0o600
        assert not path.exists()
        seen.append(True)
        replace(source, target)
    monkeypatch.setattr(io.os, 'replace', observe)
    release.record(query, ['a' * 40], path)
    assert seen and path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['sha'] == 'a' * 40


def test_fleet_sync_not_green(tmp_path):
    def query(endpoint):
        if endpoint.endswith('required_status_checks'):
            return {'contexts': ['ci']}
        return {'check_runs': [], 'statuses': []}
    with pytest.raises(ValueError):
        release.record(query, ['a' * 40], tmp_path / 'release.json')
    assert not list(tmp_path.iterdir())


def test_fleet_sync_configs(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    registry = {'hosts': [{'id': 'host-a', 'transport': 'ssh', 'os': 'linux',
                          'repos': {'plangraph': {'live_dir': '/srv/live'}},
                          'caps': {'max_runs': 4, 'mem_floor_mb': 3000}}]}
    assert genconfig.sync(registry, True) == 0
    hosts = remote_ps.configured_hosts()
    assert hosts == [{'name': 'host-a', 'target': 'host-a', 'src': '/srv/live/src'}]
    config = json.loads((genconfig.config_dir() / 'overflow-hosts.json').read_text())
    ready = tmp_path / '.local/state/plangraph/host-ready/host-a.json'
    ready.parent.mkdir(parents=True)
    ready.write_text(json.dumps({'host': 'host-a', 'ok': True, 'live_runs': 0,
                                 'mem_available_mb': 5000,
                                 'checked_at': release.datetime.now(release.UTC).isoformat()}))
    assert overflow.decide(4, 5000, 4, 3000, overflow.ready_records(config), True).host == 'host-a'
    assert genconfig.sync(registry) == 0
    (genconfig.config_dir() / 'ps-hosts.json').write_text('{"hosts": []}')
    assert genconfig.sync(registry) == 1


def test_fleet_sync_unreachable(capsys, tmp_path):
    from plangraph.fleet_sync.__main__ import main
    path = tmp_path / 'hosts.json'
    path.write_text(json.dumps({'hosts': [{'id': 'host-a', 'transport': 'keeper'}]}))
    assert main(['drift', '--registry', str(path)]) == 0
    assert capsys.readouterr().out.split()[-1] == 'unknown'
    def unreachable(host):
        raise OSError()
    assert drift.rows({'hosts': [{'id': 'host-a'}]}, 'a' * 40, unreachable)[0]['behind'] == 'unknown'


def test_fleet_sync_hosts_schema():
    schema = json.loads(Path('docs/schemas/hosts.schema.json').read_text())
    repo = {'root': '/srv/repo', 'worktrees_root': '/srv/trees', 'remote': 'example',
            'import_roots': [], 'cache_env': {}, 'live_dir': '/srv/live'}
    jsonschema.validate(repo, {'$ref': '#/$defs/repoEntry', '$defs': schema['$defs']})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(dict(repo, live_dir=''),
                            {'$ref': '#/$defs/repoEntry', '$defs': schema['$defs']})
