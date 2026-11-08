import json
from pathlib import Path

import jsonschema
import pytest

from plangraph.fleet_sync import drift, genconfig, release
from plangraph.spawner import overflow, remote_ps


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))


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


def test_fleet_sync_release_api_paths(tmp_path, monkeypatch):
    calls = []
    def output(command, **kwargs):
        calls.append(command)
        assert command[:2] == ['gh', 'api']
        endpoint = command[2]
        assert endpoint.startswith('repos/:owner/:repo/')
        if endpoint.endswith('required_status_checks'):
            return json.dumps({'contexts': ['ci']})
        if 'check-runs' in endpoint:
            return json.dumps({'check_runs': []})
        return json.dumps({'statuses': [{'context': 'ci', 'state': 'success'}]})
    monkeypatch.setattr(release.subprocess, 'check_output', output)
    release.record(candidates=['abc'], path=tmp_path / 'release.json')
    assert len(calls) == 3


@pytest.mark.parametrize('kind', ['completed', 'started', 'status', 'mixed'])
@pytest.mark.parametrize('reverse', [False, True])
def test_fleet_sync_newest_result_decides(tmp_path, kind, reverse):
    old = '2026-01-01T00:00:00Z'
    new = '2026-01-02T00:00:00Z'
    runs = [{'name': 'ci', 'status': 'completed', 'conclusion': 'success',
             'completed_at': old, 'started_at': old}]
    statuses = []
    if kind in ('completed', 'started'):
        runs.append({'name': 'ci', 'status': 'completed' if kind == 'completed' else 'in_progress',
                     'conclusion': 'failure' if kind == 'completed' else None,
                     'completed_at': new if kind == 'completed' else None, 'started_at': new})
    else:
        statuses.append({'context': 'ci', 'state': 'failure', 'created_at': new})
        if kind == 'status':
            runs = []
            statuses.append({'context': 'ci', 'state': 'success', 'created_at': old})
    def query(endpoint):
        if endpoint.endswith('required_status_checks'):
            return {'contexts': ['ci']}
        return {'check_runs': list(reversed(runs)) if reverse else runs,
                'statuses': list(reversed(statuses)) if reverse else statuses}
    with pytest.raises(ValueError, match='no main commit'):
        release.record(query, ['abc'], tmp_path / 'release.json')


def test_fleet_sync_scan_bounded_and_stops(tmp_path, monkeypatch):
    calls = []
    green = set()
    def git(*args):
        assert args == ('rev-list', '--max-count=50', 'origin/main')
        return '\n'.join(str(i) for i in range(100))
    def query(endpoint):
        if endpoint.endswith('required_status_checks'):
            return {'contexts': ['ci']}
        calls.append(endpoint)
        sha = endpoint.split('/')[4]
        return {'check_runs': [], 'statuses': [
            {'context': 'ci', 'state': 'success' if sha in green else 'failure'}]}
    monkeypatch.setattr(release, 'git', git)
    with pytest.raises(ValueError, match='no main commit'):
        release.record(query, path=tmp_path / 'release.json')
    assert len(calls) == 100
    calls.clear()
    green.update(['1', '2'])
    assert release.record(query, iter(['0', '1', '2']), tmp_path / 'release.json',
                          max_commits=3)['sha'] == '1'
    assert len(calls) == 4
    calls.clear()
    with pytest.raises(ValueError, match='no main commit'):
        release.record(query, iter(['0', '1']), tmp_path / 'release.json', max_commits=1)
    assert len(calls) == 2


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
