import importlib.util
import io
import gzip
import json
import os
from pathlib import Path
import tarfile
import pytest

spec = importlib.util.spec_from_file_location('mirror', Path(__file__).parents[1] / 'tools/mirror_spawner_records.py')
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


def record(**kw):
    return dict(run_id='run-a', host='host-a', status='completed', item_key='p:item',
                exit_code=0, chosen_leg={'id': 'leg', 'path': 'TAIL-MARKER'},
                stdout_tail='TAIL-MARKER', cwd='TAIL-MARKER', **kw)


def setup(tmp_path):
    source, dest = tmp_path / 'runs', tmp_path / 'dest'
    path = source / 'run-a' / 'record.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record()))
    return path, dest, ['--host', 'host-a', '--dest', str(dest), '--source', str(source)]


def test_mirror(tmp_path, capsys):
    path, dest, args = setup(tmp_path)
    assert mirror.main(args) == 0
    out = dest / 'host-a/run-a/record.json'
    old = out.read_bytes()
    assert b'TAIL-MARKER' not in old
    assert set(json.loads(old)) == {'run_id', 'host', 'status', 'exit_code', 'item_key', 'writer_family', 'served_family', 'chosen_leg'}
    assert out.stat().st_mode & 0o777 == 0o600
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in [dest, out.parent, out.parent.parent])
    stamp = out.stat().st_mtime_ns
    assert mirror.main(args) == 0
    assert 'unchanged=1' in capsys.readouterr().out
    assert out.stat().st_mtime_ns == stamp
    value = record(); value['exit_code'] = 7
    path.write_text(json.dumps(value))
    assert mirror.main(args) == 1
    assert out.read_bytes() == old


@pytest.mark.parametrize('change,reason', [({'status': s}, 'status') for s in ('queued', 'running', 'bad;status', '', 'x' * 41, None)] + [
    ({'host': 'host-b'}, 'host'), ({'run_id': 'run-b'}, 'run_id'),
    ({'item_key': None}, 'item_key'), ('{', 'json'), ('x' * (mirror.LIMIT + 1), 'oversize'), ([], 'object')] + [
    ({field: value}, field) for field, values in [
        ('exit_code', ['TAIL-MARKER', True, 1.5, {}]),
        ('item_key', ['', 'tail/prompt', 'x' * 201, True]),
        ('writer_family', ['tail/prompt', 'x' * 201, True, {}]),
        ('served_family', ['tail/prompt', 'x' * 201, 3, []]),
        ('chosen_leg', [{'id': 'tail/prompt'}, {'id': 'x' * 201}, {'id': True}, 'tail'])
    ] for value in values])
def test_skips(tmp_path, capsys, change, reason):
    path, dest, args = setup(tmp_path)
    value = record()
    if isinstance(change, dict):
        value.update(change)
    else:
        value = change
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    assert mirror.main(args) == 0
    assert f'"{reason}": 1' in capsys.readouterr().out
    assert not dest.exists()


def test_dry_symlinks(tmp_path, capsys):
    _, dest, args = setup(tmp_path)
    assert mirror.main(args + ['--dry-run']) == 0
    assert not dest.exists()
    dest.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    for relative in ['host-a', 'host-a/run-a', 'host-a/run-a/record.json']:
        link = dest / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        capsys.readouterr()
        assert mirror.main(args) == 1
        summary = capsys.readouterr().out
        assert 'conflicts=1' in summary and 'io_errors=0' in summary
        assert not list(outside.iterdir())
        link.unlink()


def test_all_terminal_and_missing_item(tmp_path):
    path, dest, args = setup(tmp_path)
    value = record(); del value['item_key']
    path.write_text(json.dumps(value))
    assert mirror.main(args) == 0 and not dest.exists()
    for status in ('completed', 'future_terminal'):
        value = record(); value['status'] = status
        path.write_text(json.dumps(value))
        target = dest / status
        assert mirror.main(args + ['--dest', str(target)]) == 0
        assert json.loads((target / 'host-a/run-a/record.json').read_bytes())['status'] == status


def test_destination_io_error(tmp_path, capsys):
    _, dest, args = setup(tmp_path)
    dest.mkdir(mode=0o500)
    try:
        assert mirror.main(args) == 1
        summary = capsys.readouterr().out
        assert 'io_errors=1' in summary and 'conflicts=0' in summary
        assert not list(dest.iterdir())
    finally:
        dest.chmod(0o700)


@pytest.mark.parametrize('same', [False, True])
def test_publish_race(tmp_path, monkeypatch, capsys, same):
    _, dest, args = setup(tmp_path)
    original = mirror.os.fsync
    raced = []
    out = dest / 'host-a/run-a/record.json'

    def race(fd):
        original(fd)
        if not raced:
            data = next(out.parent.glob('*.tmp')).read_bytes() if same else b'other proof\n'
            out.write_bytes(data)
            raced.append(data)

    monkeypatch.setattr(mirror.os, 'fsync', race)
    assert mirror.main(args) == (0 if same else 1)
    assert out.read_bytes() == raced[0]
    assert ('unchanged=1' if same else 'conflicts=1') in capsys.readouterr().out
    assert not list(out.parent.glob('*.tmp'))


def fake_ssh(tmp_path, monkeypatch, body):
    ssh = tmp_path / 'ssh'
    ssh.write_text('#!/bin/sh\n' + body + '\n')
    ssh.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.chdir(tmp_path)
    return ['--host', 'host-a', '--dest', str(tmp_path / 'dest'),
            '--ssh', 'host-a', '--remote-runs', 'runs ; touch marker']


def test_ssh_tar(tmp_path, monkeypatch):
    runs = tmp_path / 'runs ; touch marker'
    for name in ['run-a/record.json', 'a/b/record.json', 'run-b/other.txt']:
        path = runs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record()))
    args = fake_ssh(tmp_path, monkeypatch, 'for command do :; done\nexec sh -c "$command"')
    assert mirror.main(args) == 0
    assert [p.relative_to(tmp_path / 'dest').as_posix()
            for p in (tmp_path / 'dest').rglob('record.json')] == ['host-a/run-a/record.json']
    assert not (tmp_path / 'marker').exists()


def make_archive(tmp_path, entries):
    archive = tmp_path / 'stream.tar'
    with tarfile.open(archive, 'w') as tar:
        for name, raw, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
            else:
                member.linkname = '../outside'
                tar.addfile(member)
    return archive


@pytest.mark.parametrize('code,truncated', [(0, False), (1, False), (2, False), (0, True), (1, True)])
def test_ssh_stream_status(tmp_path, monkeypatch, capsys, code, truncated):
    raw = json.dumps(record()).encode()
    entries = [(name, raw, tarfile.REGTYPE) for name in [
        './run-a/record.json', '../record.json', '/run-b/record.json',
        './a/b/record.json', 'run-b/other.txt']]
    entries += [('run-b/record.json', b'', tarfile.SYMTYPE),
                ('run-c/record.json', b'', tarfile.LNKTYPE)]
    # Truncate inside a later member, after the valid record was yielded.
    if truncated:
        entries.append(('run-z/record.json', b'x' * 4096, tarfile.REGTYPE))
    archive = make_archive(tmp_path, entries)
    if truncated:
        archive.write_bytes(archive.read_bytes()[:7680])
    monkeypatch.setenv('STREAM', str(archive))
    args = fake_ssh(tmp_path, monkeypatch, f'cat "$STREAM"\nexit {code}')
    failed = code == 2 or truncated
    assert mirror.main(args) == int(failed)
    dest = tmp_path / 'dest'
    if failed:
        assert not dest.exists()
        assert '"source": 1' in capsys.readouterr().out
    else:
        assert [p.relative_to(dest).as_posix() for p in dest.rglob('record.json')] == ['host-a/run-a/record.json']


def test_ssh_oversize_neighbours(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mirror, 'LIMIT', 1024)
    entries = []
    for run_id in ['run-a', 'run-big', 'run-b']:
        value = record(); value['run_id'] = run_id
        raw = b'x' * 1025 if run_id == 'run-big' else json.dumps(value).encode()
        entries.append((f'./{run_id}/record.json', raw, tarfile.REGTYPE))
    monkeypatch.setenv('STREAM', str(make_archive(tmp_path, entries)))
    args = fake_ssh(tmp_path, monkeypatch, 'cat "$STREAM"')
    assert mirror.main(args) == 0
    assert '"oversize": 1' in capsys.readouterr().out
    assert sorted(p.parent.name for p in (tmp_path / 'dest').rglob('record.json')) == ['run-a', 'run-b']



def test_ssh_option_alias(tmp_path):
    with pytest.raises(SystemExit) as exc:
        mirror.main(['--host', 'host-a', '--dest', str(tmp_path), '--ssh=-option', '--remote-runs', 'runs'])
    assert exc.value.code == 2


@pytest.mark.parametrize('cut', [1024, 1124])
def test_ssh_truncated_header(tmp_path, monkeypatch, capsys, cut):
    archive = make_archive(tmp_path, [('run-a/record.json', json.dumps(record()).encode(), tarfile.REGTYPE)])
    archive.write_bytes(archive.read_bytes()[:cut])
    monkeypatch.setenv('STREAM', str(archive))
    args = fake_ssh(tmp_path, monkeypatch, 'cat "$STREAM"')
    assert mirror.main(args) == 1
    assert not (tmp_path / 'dest').exists()
    assert '"source": 1' in capsys.readouterr().out


@pytest.mark.parametrize('compressed_tar', [False, True])
def test_ssh_corrupt_gzip(tmp_path, monkeypatch, capsys, compressed_tar):
    archive = tmp_path / 'bad.gz'
    archive.write_bytes(b'\x1f\x8b\x08\x00' + b'\x00' * 6 + b'\xff' * 100)
    if compressed_tar:
        archive.write_bytes(gzip.compress(make_archive(tmp_path, []).read_bytes()))
    monkeypatch.setenv('STREAM', str(archive))
    args = fake_ssh(tmp_path, monkeypatch, 'cat "$STREAM"')
    assert mirror.main(args) == 1
    assert not (tmp_path / 'dest').exists()
    assert '"source": 1' in capsys.readouterr().out


def test_ssh_stdin_closed(tmp_path, monkeypatch):
    archive = make_archive(tmp_path, [('run-a/record.json', json.dumps(record()).encode(), tarfile.REGTYPE)])
    monkeypatch.setenv('STREAM', str(archive))
    args = fake_ssh(tmp_path, monkeypatch, 'if read line; then exit 2; fi\ncat "$STREAM"')
    with (tmp_path / 'input').open('w+b') as stream:
        stream.write(b'caller input\n'); stream.seek(0)
        saved = os.dup(0)
        try:
            os.dup2(stream.fileno(), 0)
            assert mirror.main(args) == 0
        finally:
            os.dup2(saved, 0)
            os.close(saved)
    assert (tmp_path / 'dest/host-a/run-a/record.json').exists()
