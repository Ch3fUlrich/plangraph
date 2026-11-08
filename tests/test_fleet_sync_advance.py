import json
import os
from pathlib import Path

import pytest

from plangraph.fleet_sync.advance import advance, worker


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    binpath = tmp_path / 'bin'
    binpath.mkdir()
    git = binpath / 'git'
    git.write_text('#!/usr/bin/python3\nimport sys,pathlib\na=sys.argv\n'
                   'if a[1]=="clone": pathlib.Path(a[-1]).mkdir()\n'
                   'elif a[-1]=="HEAD": print("a"*40)\n')
    git.chmod(0o755)
    ssh = binpath / 'ssh'
    ssh.write_text('#!/usr/bin/python3\nimport subprocess,sys\n'
                   'sys.exit(subprocess.call(sys.argv[-1],shell=True))\n')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH', str(binpath) + os.pathsep + os.defpath)
    hosts = []
    for name in ('host-a', 'host-b'):
        root = tmp_path / name
        old = root / 'releases' / ('b' * 40)
        old.mkdir(parents=True)
        (root / 'current').symlink_to(old)
        hosts.append({'id': name, 'os': 'linux', 'transport': 'ssh', 'repos': {
            'plangraph': {'root': str(tmp_path / 'repo'), 'live_dir': str(root / 'current'),
                          'worktrees_root': str(tmp_path / 'trees'),
                          'health_checks': {k: ['/bin/true', '--dry-run']
                                            for k in ('doctor', 'whoami', 'spawn')}}}})
    record = tmp_path / 'release.json'
    record.write_text(json.dumps({'sha': 'a' * 40}))
    return {'hosts': hosts}, record


def invoke(fleet, **kwargs):
    registry, record = fleet
    return advance(registry, kwargs.pop('sha', 'a' * 40), ['host-a', 'host-b'],
                   kwargs.pop('go', 'D-1234'), record=record, **kwargs)


@pytest.mark.parametrize('change', [{'go': None}, {'sha': 'c' * 40}])
def test_fleet_sync_advance_refuses(fleet, change):
    with pytest.raises(ValueError):
        invoke(fleet, **change)
    assert not (Path(fleet[1]).parent / 'host-a/releases' / ('a' * 40)).exists()


@pytest.mark.parametrize('phase', ['pre', 'post'])
def test_fleet_sync_advance_rollback(fleet, phase):
    registry, record = fleet
    check = record.parent / 'health'
    check.write_text('#!/usr/bin/python3\nimport pathlib,sys\n'
                     f'p=pathlib.Path({str(record.parent / "calls")!r})\n'
                     'n=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\n'
                     f'sys.exit(1 if n=={1 if phase == "pre" else 2} else 0)\n')
    check.chmod(0o755)
    registry['hosts'][0]['repos']['plangraph']['health_checks']['doctor'] = [str(check)]
    assert invoke(fleet) == 1
    assert (record.parent / 'host-a/current').resolve().name == 'b' * 40
    assert not (record.parent / 'host-b/releases' / ('a' * 40)).exists()
    events = [json.loads(line) for line in
              (record.parent / '.local/state/plangraph/fleet-sync.log').read_text().splitlines()]
    assert events[-1] == {'host': 'host-a', 'sha': 'a' * 40, 'go': 'D-1234',
                          'step': 'rollback', 'status': 'OK'}


def test_fleet_sync_advance_success_prune(fleet):
    root = fleet[1].parent
    spawn = root / 'spawn'
    calls = root / 'spawn-calls'
    spawn.write_text('#!/usr/bin/python3\nimport pathlib,sys\n'
                     'assert "--dry-run" in sys.argv\n'
                     f'with pathlib.Path({str(calls)!r}).open("a") as f: f.write("spawn\\n")\n')
    spawn.chmod(0o755)
    for host in fleet[0]['hosts']:
        host['repos']['plangraph']['health_checks']['spawn'] = [str(spawn), '--dry-run']
    for number in range(1, 6):
        (root / 'host-a/releases' / (str(number) * 40)).mkdir()
    assert invoke(fleet) == 0
    assert calls.read_text().splitlines() == ['spawn', 'spawn']
    for name in ('host-a', 'host-b'):
        assert (root / name / 'current').resolve().name == 'a' * 40
    assert (root / 'host-a/releases' / ('b' * 40)).exists()
    assert len(list((root / 'host-a/releases').iterdir())) == 3


def test_fleet_sync_advance_dry_run(fleet):
    root = fleet[1].parent
    before = sorted(root.rglob('*'))
    assert invoke(fleet, dry_run=True) == 0
    assert sorted(root.rglob('*')) == before


def test_fleet_sync_windows_pointer_fake(fleet):
    repo = fleet[0]['hosts'][0]['repos']['plangraph']
    root = Path(repo['live_dir']).parent
    pointer = root / 'current.pointer'
    pointer.write_text(str(root / 'releases' / ('b' * 40)))
    check = root / 'health'
    seen = root / 'switched'
    check.write_text('#!/usr/bin/python3\nimport pathlib,sys\n'
                     f'p=pathlib.Path({str(pointer)!r})\n'
                     'if pathlib.Path(p.read_text()).name == "a"*40:\n'
                     f' pathlib.Path({str(seen)!r}).touch()\n sys.exit(1)\n')
    check.chmod(0o755)
    repo['health_checks']['doctor'] = [str(check)]
    result = worker({'repo': repo, 'sha': 'a' * 40, 'windows': True})
    assert not result['ok']
    assert seen.exists()
    assert {'step': 'switch', 'status': 'OK'} in result['events']
    assert {'step': 'post-health', 'status': 'FAILED'} in result['events']
    assert result['events'][-1] == {'step': 'rollback', 'status': 'OK'}
    assert Path(pointer.read_text()).name == 'b' * 40


@pytest.mark.parametrize('problem', ['health_checks', 'plangraph', 'unknown', 'spawn'])
def test_fleet_sync_advance_cli_refusal(fleet, monkeypatch, capsys, problem):
    from plangraph.fleet_sync import advance as module
    from plangraph.fleet_sync.__main__ import main
    registry, record = fleet
    repo = registry['hosts'][1]['repos']['plangraph']
    if problem == 'health_checks':
        del repo['health_checks']
    elif problem == 'plangraph':
        del registry['hosts'][1]['repos']['plangraph']
    elif problem == 'spawn':
        repo['health_checks']['spawn'] = ['/bin/true']
    path = record.parent / 'hosts.json'
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(module, 'release_path', lambda: record)
    monkeypatch.setattr(module.subprocess, 'check_output',
                        lambda *a, **k: pytest.fail('host contacted'))
    assert main(['advance', '--sha', 'a' * 40, '--hosts', 'host-a',
                 'absent' if problem == 'unknown' else 'host-b', '--go', 'D-1234',
                 '--registry', str(path)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert len(output.err.splitlines()) == 1
    assert not (record.parent / 'host-a/releases' / ('a' * 40)).exists()


@pytest.mark.parametrize('kind', ['relative', 'symlink'])
def test_fleet_sync_advance_cleanup_refusal(fleet, kind):
    repo = fleet[0]['hosts'][0]['repos']['plangraph']
    if kind == 'relative':
        repo['worktrees_root'] = 'trees'
    else:
        fleet[0]['hosts'][0]['transport'] = 'local'
        alias = fleet[1].parent / 'alias'
        alias.symlink_to(Path(repo['live_dir']).parent, target_is_directory=True)
        repo['worktrees_root'] = str(alias)
    with pytest.raises(ValueError, match='cleanup'):
        invoke(fleet, dry_run=True)


def test_fleet_sync_advance_lock(fleet):
    from plangraph.fleet_sync.advance import rollout_lock
    lock = fleet[1].parent / '.local/state/plangraph/fleet-sync.lock'
    with rollout_lock(lock), pytest.raises(ValueError, match='already running'):
        invoke(fleet)
    assert not lock.exists()


def test_fleet_sync_worker_lock(fleet):
    repo = fleet[0]['hosts'][0]['repos']['plangraph']
    lock = Path(repo['live_dir']).parent / '.advance.lock'
    lock.touch()
    with pytest.raises(FileExistsError):
        worker({'repo': repo, 'sha': 'a' * 40, 'windows': False})
    assert lock.exists()


def test_fleet_sync_advance_cli_requires_go():
    from plangraph.fleet_sync.__main__ import main
    with pytest.raises(SystemExit) as error:
        main(['advance', '--sha', 'a' * 40, '--hosts', 'host-a'])
    assert error.value.code == 2


@pytest.mark.parametrize('missing', ['plangraph', 'live_dir'])
def test_fleet_sync_ps_only_preflight(fleet, missing):
    host = fleet[0]['hosts'][1]
    if missing == 'plangraph':
        del host['repos']['plangraph']
    else:
        del host['repos']['plangraph']['live_dir']
    with pytest.raises(ValueError, match='^ps-only host host-b: no live dir$'):
        invoke(fleet, run=lambda *a: pytest.fail('host contacted'))


def test_fleet_sync_preflight_aggregates(fleet):
    fleet[0]['hosts'][0]['transport'] = 'invalid'
    del fleet[0]['hosts'][1]['repos']['plangraph']['live_dir']
    with pytest.raises(ValueError) as error:
        invoke(fleet, run=lambda *a: pytest.fail('host contacted'))
    assert str(error.value).splitlines() == [
        "host-a: invalid transport 'invalid'", 'ps-only host host-b: no live dir']


def test_fleet_sync_ssh_paths_are_remote(fleet, monkeypatch):
    from plangraph.fleet_sync import advance as module
    repo = fleet[0]['hosts'][0]['repos']['plangraph']
    alias = fleet[1].parent / 'controller-alias'
    alias.symlink_to(Path(repo['live_dir']).parent, target_is_directory=True)
    repo['worktrees_root'] = str(alias)
    original = module.Path.resolve

    def resolve(path, *args, **kwargs):
        assert str(path) not in (repo['worktrees_root'], str(Path(repo['live_dir']).parent))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, 'resolve', resolve)
    seen = []
    assert invoke(fleet, run=lambda host, config: seen.append(config) or
                  {'ok': True, 'events': []}) == 0
    assert seen[0]['repo'] == repo


def test_fleet_sync_windows_transport_exact_argv(fleet, monkeypatch):
    import base64
    import inspect
    import subprocess

    from plangraph.fleet_sync import advance as module
    host = fleet[0]['hosts'][0]
    host['os'] = 'windows'
    config = {'repo': host['repos']['plangraph'], 'sha': 'a' * 40, 'windows': True}
    code = inspect.getsource(worker) + '\nimport json; print(json.dumps(worker(json.loads(' + repr(json.dumps(config)) + '))))'
    encoded = base64.b64encode(code.encode()).decode('ascii')
    remote = subprocess.list2cmdline([
        'python3', '-c', f"import base64;exec(base64.b64decode('{encoded}'))"])

    def check_output(argv, **kwargs):
        assert argv == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                        '--', 'host-a', remote]
        assert '\n' not in argv[-1] and '\r' not in argv[-1]
        return '{"ok": true, "events": []}'

    monkeypatch.setattr(module.subprocess, 'check_output', check_output)
    assert module.transport(host, config)['ok']
