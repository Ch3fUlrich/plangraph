import json
import os
import subprocess
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
        assert args == ('rev-list', '--first-parent', '--max-count=50', 'origin/main')
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


def test_fleet_sync_release_skips_merged_pr_heads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    def git(*args, data=None):
        return subprocess.check_output(['git', *args], input=data, text=True, env=env).strip()
    git('init', '--quiet')
    tree = git('mktree', data='')
    dates = iter(range(5))
    def commit(title, *parents):
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'200{next(dates)}-01-01T00:00:00Z'
        return git('commit-tree', tree, *[arg for p in parents for arg in ('-p', p)],
                   data=title + '\n')
    base = commit('base')
    merged_pr = commit('merged PR', base)
    merge = commit('green merge on main', base, merged_pr)
    pr_head = commit('newer PR head', merge)
    tip = commit('pending merge on main', merge, pr_head)
    git('update-ref', 'refs/remotes/origin/main', tip)
    queried = []
    def query(endpoint):
        if endpoint.endswith('required_status_checks'):
            return {'contexts': ['ci']}
        sha = endpoint.split('/')[4]
        queried.append(sha)
        return {'check_runs': [], 'statuses': [
            {'context': 'ci', 'state': 'failure' if sha == tip else 'success'}]}
    path = tmp_path / 'release.json'
    assert release.record(query, path=path)['sha'] == merge
    assert json.loads(path.read_text())['sha'] == merge
    assert pr_head not in queried


def test_fleet_sync_configs(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    registry = {'hosts': [{'id': 'host-a', 'transport': 'ssh', 'os': 'linux',
                          'repos': {'plangraph': {'live_dir': '/srv/live'}},
                          'caps': {'max_runs': 4, 'mem_floor_mb': 3000},
                          'warmup': {'ramp_mb': {'implement': 2048}}}]}
    assert genconfig.sync(registry, True) == 0
    (genconfig.config_dir() / 'hosts.json').write_text(json.dumps(registry))
    hosts = remote_ps.configured_hosts()
    assert hosts == [{'name': 'host-a', 'target': 'host-a', 'src': '/srv/live/src'}]
    config = json.loads((genconfig.config_dir() / 'overflow-hosts.json').read_text())
    ready = tmp_path / '.local/state/plangraph/host-ready/host-a.json'
    ready.parent.mkdir(parents=True)
    ready.write_text(json.dumps({'host': 'host-a', 'ok': True, 'live_runs': 0,
                                 'mem_available_mb': 5048,
                                 'checked_at': release.datetime.now(release.UTC).isoformat()}))
    assert 'run_cap' not in config['hosts']['host-a']
    assert overflow.decide(50, 5000, 2048, 3000, overflow.ready_records(config), True).host == 'host-a'
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
            'import_roots': [], 'cache_env': {}, 'live_dir': '/srv/live/current'}
    jsonschema.validate(repo, {'$ref': '#/$defs/repoEntry', '$defs': schema['$defs']})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(dict(repo, live_dir=''),
                            {'$ref': '#/$defs/repoEntry', '$defs': schema['$defs']})


@pytest.mark.parametrize('contents', [None, '{', '{}', '{"hosts": null}'])
@pytest.mark.parametrize('json_output', [False, True])
def test_fleet_sync_bad_registry_fails(tmp_path, capsys, contents, json_output):
    from plangraph.fleet_sync.__main__ import main
    path = tmp_path / 'hosts.json'
    if contents is not None:
        path.write_text(contents)
    assert main(['drift', '--registry', str(path)] + (['--json'] if json_output else [])) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert 'failed' in captured.err


@pytest.mark.parametrize('live', ['C:/fleet/live/current', r'C:\fleet\live\current'])
def test_fleet_sync_windows_config(tmp_path, monkeypatch, live):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    registry = {'hosts': [{'id': 'win-host', 'transport': 'ssh', 'os': 'windows',
                          'repos': {'plangraph': {'live_dir': live}}}]}
    assert genconfig.sync(registry, True) == 0
    assert remote_ps.configured_hosts() == [
        {'name': 'win-host', 'target': 'win-host', 'win_src': live + '/src'}]
    config = json.loads((genconfig.config_dir() / 'overflow-hosts.json').read_text())
    assert config == {'hosts': {}, 'public_repos': []}
    assert overflow.ready_records(config) == {}
