"""Hermetic artifact transport checks; no harness or network."""
import os
from pathlib import Path
import subprocess
import sys
import runpy

import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    monkeypatch.setenv('PLANGRAPH_ARTIFACT_REMOTE', str(remote))
    def run(*args, code=0):
        p = subprocess.run([sys.executable, '-m', 'plangraph.fleet_artifact', *map(str, args)], capture_output=True)
        assert p.returncode == code, p.stderr
        assert b'document-sentinel' not in p.stdout + p.stderr
        return p.stdout.decode().strip()
    run('init', '--confirm-private')
    return tmp_path, remote, run


@pytest.mark.parametrize('data', [b'document-sentinel\r\nnext\r\n', b'\xff\xfe\n', b'document-sentinel', b'', b'\0\r\n\x80', 'multilingual: é漢字\n'.encode()])
def test_roundtrip(store, data):
    root, remote, run = store
    source = root / 'brief'
    source.write_bytes(data)
    pointer = run('put', source, '--item', 'plan:ONE')
    assert pointer.startswith('ARTIFACT brief sha256:') and '\n' not in pointer
    before = subprocess.check_output(['git', '--git-dir', str(remote), 'show-ref'])
    assert run('put', source, '--item', 'plan:ONE') == pointer
    assert subprocess.check_output(['git', '--git-dir', str(remote), 'show-ref']) == before
    run('get', pointer, '--out', root / 'out')
    assert (root / 'out/brief').read_bytes() == data
    run('get', pointer.split(' ref=')[1].split()[0], '--out', root / 'out')
    (root / 'out/brief').write_bytes(b'other')
    run('get', pointer, '--out', root / 'out', code=5)
    assert (root / 'out/brief').read_bytes() == b'other'
    for bad in (pointer[:30], pointer.replace('bytes=', 'bytes=9'),
                pointer.replace('lines=', 'lines=9'), pointer.replace('ARTIFACT brief', 'ARTIFACT other'),
                pointer.replace('sha256:', 'sha256:f', 1)):
        run('get', bad, '--out', root / 'bad', code=4)
        assert not (root / 'bad').exists()
    source.write_bytes(data + b'new')
    run('put', source)
    after = subprocess.check_output(['git', '--git-dir', str(remote), 'show-ref'])
    assert len(after.splitlines()) == len(before.splitlines()) + 1
    assert all(line in after.splitlines() for line in before.splitlines())


def test_failures(store, monkeypatch):
    root, remote, run = store
    source = root / 'brief'
    source.write_bytes(b'document-sentinel')
    run('init', code=2)
    run('put', source, '--name', '../x', code=2)
    source.write_bytes(b'x' * (5 * 1024 * 1024 + 1))
    run('put', source, code=6)
    source.write_text('safe\n' + 'gh' + 'p_' + 'examplevalue')
    stderr = subprocess.run([sys.executable, '-m', 'plangraph.fleet_artifact', 'put', str(source)], capture_output=True).stderr
    assert b'lines 2' in stderr
    assert b'ghp_examplevalue' not in stderr
    run('put', source, code=7)
    monkeypatch.delenv('PLANGRAPH_ARTIFACT_REMOTE')
    run('put', source, code=2)
    monkeypatch.setenv('PLANGRAPH_ARTIFACT_REMOTE', 'https://user:password@example.invalid/repo')
    run('put', source, code=2)
    monkeypatch.setenv('PLANGRAPH_ARTIFACT_REMOTE', str(root / 'absent'))
    run('put', source, code=3)
    subprocess.run(['git', '--git-dir', str(remote), 'update-ref', '-d', 'refs/heads/artifact-store'], check=True)
    monkeypatch.setenv('PLANGRAPH_ARTIFACT_REMOTE', str(remote))
    run('put', source, code=2)
    assert subprocess.run(['git', '--git-dir', str(remote), 'show-ref'], capture_output=True).stdout == b''


def test_shared_shapes(monkeypatch):
    from plangraph import fleet_artifact
    shared = runpy.run_path('tools/hooks/redact_common.py')['_SHAPES']
    calls = []
    def load(path):
        calls.append(Path(path).resolve())
        return {'_SHAPES': shared}
    monkeypatch.setattr(runpy, 'run_path', load)
    namespace = dict(__file__=fleet_artifact.__file__, __name__='shape_test')
    exec(compile(Path(fleet_artifact.__file__).read_text(), fleet_artifact.__file__, 'exec'),
         namespace)
    assert calls == [Path('tools/hooks/redact_common.py').resolve()]
    assert namespace['_SHAPES'] is shared


@pytest.mark.parametrize('separator', [b'\x0b', b'\x0c', b'\x1c', b'\x1d', b'\x1e', b'\x85'])
def test_secret_line_boundaries(store, separator):
    root, remote, run = store
    source = root / 'brief'
    data = b'a' + separator + b'ghp_' + b'A' * 20
    source.write_bytes(data)
    before = subprocess.check_output(['git', '--git-dir', str(remote), 'show-ref'])
    p = subprocess.run([sys.executable, '-m', 'plangraph.fleet_artifact', 'put', str(source)], capture_output=True)
    assert p.returncode == 7 and p.stdout == b''
    assert p.stderr == b'fleet-artifact: secret shape on lines 1\n'
    assert data.count(b'\n') == 0
    assert subprocess.check_output(['git', '--git-dir', str(remote), 'show-ref']) == before


def test_shims():
    for suffix in ('', '.cmd', '.ps1'):
        assert Path('tools/fleet-artifact' + suffix).read_text() == Path('tools/fleet-msg' + suffix).read_text().replace('plangraph.fleet_bus.client', 'plangraph.fleet_artifact')
    assert subprocess.run(['tools/fleet-artifact', '--help'], capture_output=True).returncode == 0


@pytest.mark.parametrize('kind', ['tamper', 'two', '../x', 'a/b'])
def test_hostile_tree(store, kind):
    root, remote, run = store
    source = root / 'brief'
    source.write_bytes(b'document-sentinel')
    pointer = run('put', source)
    ref = 'refs/heads/' + pointer.split(' ref=')[1]
    def git(*args, data=None):
        return subprocess.check_output(['git', '--git-dir', str(remote), *args], input=data)
    blob = git('hash-object', '-w', '--stdin', data=b'changed').strip()
    name = kind if '/' in kind else 'brief'
    # Raw tree objects permit names that normal porcelain would reject.
    tree_data = b'100644 ' + name.encode() + b'\0' + bytes.fromhex(blob.decode())
    if kind == 'two':
        tree_data += b'100644 extra\0' + bytes.fromhex(blob.decode())
    tree = git('hash-object', '--literally', '-w', '-t', 'tree', '--stdin', data=tree_data).strip()
    env = dict(os.environ, GIT_AUTHOR_NAME='test', GIT_COMMITTER_NAME='test',
               GIT_AUTHOR_EMAIL='test@artifact.invalid', GIT_COMMITTER_EMAIL='test@artifact.invalid')
    commit = subprocess.check_output(['git', '--git-dir', str(remote), 'commit-tree', tree.decode()], input=b'test\n', env=env).strip()
    git('update-ref', ref, commit.decode())
    before = git('show-ref')
    run('put', source, code=4)
    assert git('show-ref') == before
    run('get', pointer, '--out', root / 'out', code=4)
    assert not (root / 'out').exists()
