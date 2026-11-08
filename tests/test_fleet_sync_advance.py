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
                          'health_checks': {k: ['/bin/true'] for k in ('doctor', 'whoami', 'spawn')}}}})
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
    for number in range(1, 6):
        (root / 'host-a/releases' / (str(number) * 40)).mkdir()
    assert invoke(fleet) == 0
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
    assert worker({'repo': repo, 'sha': 'a' * 40, 'windows': True})['ok']
    assert Path(pointer.read_text()).name == 'a' * 40
    repo['health_checks']['doctor'] = ['/bin/false']
    assert not worker({'repo': repo, 'sha': 'a' * 40, 'windows': True})['ok']
    assert Path(pointer.read_text()).name == 'a' * 40


def test_fleet_sync_advance_cli_requires_go():
    from plangraph.fleet_sync.__main__ import main
    with pytest.raises(SystemExit) as error:
        main(['advance', '--sha', 'a' * 40, '--hosts', 'host-a'])
    assert error.value.code == 2
