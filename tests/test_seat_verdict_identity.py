"""Record identity binds posting independently of derived evidence."""
import json
from pathlib import Path

import pytest

from test_seat_record_fixes import argv_for, rec_dir, sv


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
                    '--url', 'http://keeper.invalid']) == sv.EXIT_REFUSED
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
