import importlib.util
import io
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


def test_ssh_tar(tmp_path, monkeypatch):
    archive = tmp_path / 'stream.tar'
    with tarfile.open(archive, 'w') as tar:
        for name in ['run-a/record.json', '../record.json', '/run-b/record.json', 'run-b/nested/record.json', 'run-b/record.json']:
            member = tarfile.TarInfo(name)
            raw = json.dumps(record()).encode()
            if name == 'run-b/record.json':
                member.type = tarfile.SYMTYPE; member.linkname = '../outside'
                tar.addfile(member)
            else:
                member.size = len(raw); tar.addfile(member, io.BytesIO(raw))
    ssh = tmp_path / 'ssh'
    ssh.write_text('#!/bin/sh\n[ "$1" = "-o" ] && [ "$2" = "BatchMode=yes" ] && [ "$3" = "--" ] || exit 2\nprintf "%s" "$5" > "$COMMAND"\ncat "$STREAM"\nexit "${FAIL:-0}"\n')
    ssh.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('STREAM', str(archive))
    command = tmp_path / 'command'
    monkeypatch.setenv('COMMAND', str(command))
    args = ['--host', 'host-a', '--dest', str(tmp_path / 'dest'), '--ssh', 'host-a', '--remote-runs', 'runs; touch marker']
    monkeypatch.setenv('FAIL', '1')
    assert mirror.main(args) == 1
    assert not (tmp_path / 'dest').exists()
    monkeypatch.setenv('FAIL', '0')
    assert mirror.main(args) == 0
    assert command.read_text() == "tar -C 'runs; touch marker' -cf - --wildcards '*/record.json'"
    assert len(list((tmp_path / 'dest').rglob('record.json'))) == 1


def test_ssh_option_alias(tmp_path):
    with pytest.raises(SystemExit) as exc:
        mirror.main(['--host', 'host-a', '--dest', str(tmp_path), '--ssh=-option', '--remote-runs', 'runs'])
    assert exc.value.code == 2
