"""Record identity binds posting independently of derived evidence."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_seat_record_fixes import argv_for, rec_dir, sv
from test_seat_verdict_record import record_dir, seat_record


KEY = 'plangraph:D-5047'
OTHER_KEY = 'plangraph:D-5048'


@pytest.mark.parametrize('stored_key', [KEY, None])
@pytest.mark.parametrize('identity', [{}, {'item_key': OTHER_KEY, 'key': OTHER_KEY}])
def test_record_only_identity_is_independent_of_evidence_builder(
        tmp_path, monkeypatch, capsys, stored_key, identity):
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=stored_key)
    assert not (seat / 'evidence.json').exists()
    monkeypatch.setattr(sv, 'evidence_from_record', lambda *a, **kw:
                        (dict(served_model='gemini-3-pro', head_sha='a' * 40,
                              **identity), 'VERDICT: FIX\n'))
    token = tmp_path / 'reviewer-google.token'
    token.write_text('dummy\n', encoding='utf-8')
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path != token, 'record identity must be refused before token reads'
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded_read)
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('foreign key must not post'))
    args = argv_for(seat, writers='xiaomi')
    args[args.index('--key') + 1] = OTHER_KEY
    args.remove('--dry-run')
    args.extend(['--url', 'http://keeper.invalid'])
    assert sv.main(args) == sv.EXIT_REFUSED
    assert 'record item_key does not match --key' in capsys.readouterr().err


@pytest.mark.parametrize('with_evidence', [False, True])
@pytest.mark.parametrize('record', [b'{', b'[]', b'null', b'\xff', 'directory'])
def test_unreadable_identity_cannot_be_laundered_by_evidence_builder(
        tmp_path, monkeypatch, capsys, with_evidence, record):
    path = tmp_path / 'record.json'
    if record == 'directory':
        path.mkdir()
    else:
        path.write_bytes(record)
    if with_evidence:
        (tmp_path / 'evidence.json').write_text(json.dumps(
            {'served_model': 'gemini-3-pro'}), encoding='utf-8')
        (tmp_path / 'review.txt').write_text('VERDICT: FIX\n', encoding='utf-8')

    def forbidden(*args, **kwargs):
        pytest.fail('unreadable record identity must refuse before derivation or posting')

    monkeypatch.setattr(sv, 'evidence_from_record', forbidden)
    monkeypatch.setattr(sv, 'send', forbidden)
    assert sv.main(['--key', OTHER_KEY, '--seat-dir', str(tmp_path),
                    '--writer-family', 'xiaomi', '--token-dir', str(tmp_path),
                    '--url', 'http://keeper.invalid'], record_wait=0) == sv.EXIT_REFUSED
    assert 'cannot read record identity' in capsys.readouterr().err


@pytest.mark.parametrize('source', ['record', 'evidence', 'both'])
@pytest.mark.parametrize('identity', [{}, {'item_key': KEY}, {'item_key': None}])
def test_omitted_key_is_required_before_any_seat_or_token_read(
        tmp_path, monkeypatch, capsys, source, identity):
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, **identity)
    if source != 'record':
        (seat / 'evidence.json').write_text(json.dumps(
            dict(served_model='gemini-3-pro', **identity)), encoding='utf-8')
        (seat / 'review.txt').write_text('VERDICT: FIX\n', encoding='utf-8')
    if source == 'evidence':
        (seat / 'record.json').unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('omitted --key must not read a seat, token, or post')

    monkeypatch.setattr(Path, 'read_text', forbidden)
    monkeypatch.setattr(sv, 'evidence_from_record', forbidden)
    monkeypatch.setattr(sv, 'send', forbidden)
    with pytest.raises(SystemExit) as exc:
        sv.main(['--seat-dir', str(seat), '--writer-family', 'xiaomi',
                 '--token-dir', str(tmp_path), '--url', 'http://keeper.invalid'])
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert 'required: --key' in output.err
    assert output.out == ''

@pytest.fixture
def posting(tmp_path, monkeypatch):
    argv = record_dir(tmp_path, "run-" + "1" * 32)
    argv.remove("--dry-run")
    argv.extend(["--url", "http://keeper.invalid"])
    sent = []

    def send(url, token, payload):
        assert url == "http://keeper.invalid" and token == "dummy"
        assert payload["key"] == "demo:A" and payload["ok"] is True
        sent.append(payload)
        return 200, "ok"

    monkeypatch.setattr(sv, "send", send)
    return argv, tmp_path / ("run-" + "1" * 32), sent


@pytest.mark.parametrize("has_getuid", [False, True])
def test_windows_record_skips_posix_owner_and_mode(posting, monkeypatch, has_getuid):
    argv, seat, sent = posting
    (seat / "record.json").chmod(0o777)

    def forbidden():
        pytest.fail("Windows must not call getuid")

    platform = SimpleNamespace(name="nt", **({"getuid": forbidden} if has_getuid else {}))
    monkeypatch.setattr(seat_record, "os", platform)
    assert sv.main(argv) == 0
    assert len(sent) == 1


def test_drive_mount_still_requires_linux_owner(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    uid = (seat / "record.json").stat().st_uid
    # Compare the same foreign owner on Windows and POSIX, including drive mounts.
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="nt", getuid=lambda: uid + 1))
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid + 1))
    monkeypatch.setattr(Path, "resolve", lambda self: Path("/mnt/c") / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "owned by the current uid" in capsys.readouterr().err
    assert len(sent) == 1


@pytest.mark.parametrize("mode", [0o620, 0o602], ids=["group", "other"])
@pytest.mark.parametrize("root", ["/native", "/mnt/data"])
def test_posix_record_group_or_other_writable_refused(posting, monkeypatch, capsys, mode, root):
    argv, seat, sent = posting
    path = seat / "record.json"
    path.chmod(mode)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="nt"))
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    monkeypatch.setattr(Path, "resolve", lambda self: Path(root) / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "not group/world-writable" in capsys.readouterr().err
    assert len(sent) == 1


@pytest.mark.parametrize("drive", ["c", "Z"])
def test_native_record_link_into_drive_mount_keeps_mode_checks(
        posting, tmp_path, monkeypatch, capsys, drive):
    argv, seat, sent = posting
    path, target = seat / "record.json", tmp_path / "target.json"
    path.rename(target)
    target.chmod(0o666)
    path.symlink_to(target)
    uid = target.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    mounted = Path("/mnt") / drive / seat.name / path.name
    monkeypatch.setattr(Path, "absolute", lambda self: mounted)
    monkeypatch.setattr(Path, "resolve", lambda self: mounted)
    # The real target is on the drive, including a native lexical alias.
    assert sv.main(argv) == 0
    capsys.readouterr()
    monkeypatch.setattr(Path, "absolute", lambda self: Path("/native") / seat.name / self.name)
    assert sv.main(argv) == 0
    assert not capsys.readouterr().err
    assert len(sent) == 2


def test_mnt_prefix_with_dotdot_is_not_a_drive_path(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    path = seat / "record.json"
    path.chmod(0o666)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, "os", SimpleNamespace(name="posix", getuid=lambda: uid))
    monkeypatch.setattr(Path, "resolve", lambda self: Path("/mnt/c/../../home") / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert "not group/world-writable" in capsys.readouterr().err
    assert not sent


def test_drive_alias_to_native_record_enforces_real_mode(posting, monkeypatch, capsys):
    argv, seat, sent = posting
    path = seat / 'record.json'
    path.chmod(0o666)
    uid = path.stat().st_uid
    monkeypatch.setattr(seat_record, 'os', SimpleNamespace(name='posix', getuid=lambda: uid))
    monkeypatch.setattr(Path, 'absolute', lambda self: Path('/mnt/c/link') / seat.name / self.name)
    monkeypatch.setattr(Path, 'resolve', lambda self: Path('/native/records') / seat.name / self.name)
    assert sv.main(argv) == sv.EXIT_REFUSED
    assert 'not group/world-writable' in capsys.readouterr().err
    assert not sent
