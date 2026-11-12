"""Validated spawn identity is persisted without inferring keys from prompts."""
import asyncio
import json
from pathlib import Path

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.item_key import annotate_item, valid_item
from plangraph.spawner.runner import spawn_service
from registry_route_util import _Runner, _service


KEY = 'plangraph:D-5047'
OTHER_KEY = 'plangraph:D-5048'
MISSING_EVENT = dict(event='seat_item_missing', level='warning')


@pytest.fixture(autouse=True)
def isolated_identity(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize('key', [
    KEY, 'plan.graph-1_test:D-5047.part_2:review',
    'p:' + 'x' * 180, 'p' * 19 + ':' + 'x' * 180,
])
def test_valid_item_shape(key):
    assert valid_item(key)


@pytest.mark.parametrize('key', [
    'garbage', ':x', 'plangraph:', '', 'bad/item', 'plangraph:D-5047\n',
    'plangraph:D 5047', 'plängraph:D-5047', 'plangraph:D-５０４７',
    'p:' + 'x' * 181, 'p' * 20 + ':' + 'x' * 180, None, 12, {}, [],
])
def test_invalid_item_shape(key):
    assert not valid_item(key)


@pytest.mark.parametrize('role', ['review', 'judge'])
def test_seat_card_key_and_missing_warning(role):
    record = {'role': role}
    annotate_item(record, {'item_key': KEY})
    assert record['item_key'] == KEY
    assert 'events' not in record
    missing = {'role': role, 'events': [dict(event='existing')]}
    annotate_item(missing, {})
    annotate_item(missing, {})
    assert missing['item_key'] is None
    assert missing['events'] == [dict(event='existing'), MISSING_EVENT]


@pytest.mark.parametrize('role', ['review', 'judge', 'implement'])
def test_prompt_cannot_supply_item_identity(role):
    record = {'role': role, 'prompt': f'Review {KEY} now'}
    annotate_item(record, {})
    assert record['item_key'] is None
    assert record.get('events', []) == ([MISSING_EVENT] if role != 'implement' else [])


@pytest.mark.parametrize('card,env,expected', [
    ({'item_key': KEY}, OTHER_KEY, KEY),
    ({'item_key': KEY, 'item': OTHER_KEY}, None, KEY),
    ({'item_key': '', 'item': KEY}, OTHER_KEY, KEY),
    ({'item_key': 'garbage', 'item': KEY}, OTHER_KEY, None),
    ({'item_key': 12, 'item': KEY}, OTHER_KEY, None),
    ({'item_key': None, 'item': KEY}, OTHER_KEY, KEY),
])
def test_item_alias_precedence(monkeypatch, card, env, expected):
    if env is not None:
        monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', env)
    record = {'role': 'review', 'prompt': f'Review {OTHER_KEY} now'}
    annotate_item(record, card)
    assert record['item_key'] == expected
    if expected is None:
        assert record['warnings'] == ['invalid item_key; stored as null']


@pytest.mark.parametrize('fault,expected', [
    ('wrong_family', 2), ('head', 2), ('verdict', 3), ('forged', 2),
])
def test_poster_refusals_are_hermetic(tmp_path, monkeypatch, fault, expected):
    from test_seat_record_fixes import rec_dir, argv_for, sv
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=KEY,
                   tail='nothing decided' if fault == 'verdict' else 'VERDICT: FIX')
    args = argv_for(seat, writers='xiaomi' if fault == 'wrong_family' else 'google',
                    head='b' * 40 if fault == 'head' else 'a' * 40)
    args[args.index('--key') + 1] = KEY
    args.remove('--dry-run')
    args.extend(['--url', 'http://keeper.invalid'])
    if fault == 'forged':
        record = json.loads((seat / 'record.json').read_text())
        record['run_id'] = 'run-' + '2' * 32
        (seat / 'record.json').write_text(json.dumps(record))

    def forbidden(*args, **kwargs):
        pytest.fail('refused evidence must not reach network')

    monkeypatch.setattr(sv, 'send', forbidden)
    assert sv.main(args) == expected


@pytest.mark.parametrize('stored_key', [KEY, None])
@pytest.mark.parametrize('token_present', [False, True])
def test_poster_rejects_foreign_key(tmp_path, monkeypatch, capsys, stored_key,
                                    token_present):
    from test_seat_record_fixes import rec_dir, argv_for, sv
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=stored_key)
    args = argv_for(seat, writers='google')
    args[args.index('--key') + 1] = OTHER_KEY
    args.remove('--dry-run')
    args.extend(['--url', 'http://keeper.invalid'])
    # The seat is Xiaomi; --writer-family names the reviewed writer, not the token.
    token = tmp_path / 'reviewer-xiaomi.token'
    if token_present:
        token.write_text('dummy\n')
    assert token.exists() is token_present
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path != token, 'foreign key must be refused before token reads'
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded_read)
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('foreign key must not post'))
    assert sv.main(args) == sv.EXIT_REFUSED
    assert 'item_key does not match --key' in capsys.readouterr().err


@pytest.mark.parametrize('identity', [
    {'item_key': KEY}, {'key': KEY}, {'item_key': None}, {'key': None},
    {'item_key': OTHER_KEY, 'key': KEY}, {'item_key': KEY, 'key': OTHER_KEY},
    {'item_key': [], 'key': OTHER_KEY}, {'item_key': OTHER_KEY, 'key': {}},
])
@pytest.mark.parametrize('with_record', [False, True])
def test_poster_rejects_foreign_evidence_identity(tmp_path, monkeypatch, capsys,
                                                identity, with_record):
    from test_seat_record_fixes import rec_dir, sv
    seat = (rec_dir(tmp_path, 'run-' + '1' * 32, item_key=KEY)
            if with_record else tmp_path / 'seat')
    seat.mkdir(exist_ok=True)
    (seat / 'evidence.json').write_text(json.dumps(
        dict(served_model='gemini-3-pro', **identity)))
    (seat / 'review.txt').write_text('VERDICT: FIX\n')
    token = tmp_path / 'reviewer-google.token'
    assert not token.exists()
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path != token, 'foreign identity must be refused before token reads'
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded_read)
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('foreign identity must not post'))
    assert sv.main(['--key', OTHER_KEY, '--seat-dir', str(seat),
                    '--writer-family', 'xiaomi', '--token-dir', str(tmp_path),
                    '--url', 'http://keeper.invalid']) == sv.EXIT_REFUSED
    assert 'does not match --key' in capsys.readouterr().err


@pytest.mark.parametrize('identity', [{}, {'item_key': KEY}, {'key': KEY},
                                     {'item_key': KEY, 'key': KEY}])
@pytest.mark.parametrize('with_record', [False, True])
def test_poster_accepts_matching_or_legacy_evidence(tmp_path, capsys, identity,
                                                  with_record):
    from test_seat_record_fixes import rec_dir, sv
    seat = (rec_dir(tmp_path, 'run-' + '1' * 32, item_key=KEY)
            if with_record else tmp_path)
    (seat / 'evidence.json').write_text(json.dumps(
        dict(served_model='gemini-3-pro', **identity)))
    (seat / 'review.txt').write_text('VERDICT: FIX\n')
    assert sv.main(['--key', KEY, '--seat-dir', str(seat),
                    '--writer-family', 'xiaomi', '--token-dir', str(tmp_path),
                    '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out)['body']['key'] == KEY


@pytest.mark.parametrize('stored_key', [KEY, None])
@pytest.mark.parametrize('identity', [{}, {'item_key': OTHER_KEY, 'key': OTHER_KEY}])
def test_evidence_cannot_override_record_identity(tmp_path, monkeypatch, capsys,
                                                stored_key, identity):
    from test_seat_record_fixes import rec_dir, sv
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=stored_key)
    (seat / 'evidence.json').write_text(json.dumps(
        dict(served_model='gemini-3-pro', **identity)))
    (seat / 'review.txt').write_text('VERDICT: FIX\n')
    monkeypatch.setattr(sv, 'send', lambda *a: pytest.fail('record identity must not be overridden'))
    assert sv.main(['--key', OTHER_KEY, '--seat-dir', str(seat),
                    '--writer-family', 'xiaomi', '--token-dir', str(tmp_path),
                    '--dry-run']) == sv.EXIT_REFUSED
    assert 'record item_key does not match --key' in capsys.readouterr().err


@pytest.mark.parametrize('record', ['{', '[]', 'null', 'directory'])
def test_evidence_with_unreadable_record_identity_is_refused(tmp_path, capsys, record):
    from test_seat_record_fixes import sv
    (tmp_path / 'evidence.json').write_text(json.dumps({'served_model': 'gemini-3-pro'}))
    (tmp_path / 'review.txt').write_text('VERDICT: FIX\n')
    path = tmp_path / 'record.json'
    if record == 'directory':
        path.mkdir()
    else:
        path.write_text(record)
    assert sv.main(['--key', KEY, '--seat-dir', str(tmp_path),
                    '--writer-family', 'xiaomi', '--token-dir', str(tmp_path),
                    '--dry-run']) == sv.EXIT_REFUSED
    assert 'cannot read record identity' in capsys.readouterr().err


def test_poster_accepts_matching_record_key(tmp_path, capsys):
    from test_seat_record_fixes import rec_dir, argv_for, sv
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=KEY)
    args = argv_for(seat, writers='google')
    args[args.index('--key') + 1] = KEY
    assert sv.main(args) == 0
    assert json.loads(capsys.readouterr().out)['body']['key'] == KEY


@pytest.mark.parametrize('key', [KEY, None])
def test_terminal_save_uses_only_verdict_outbox(tmp_path, monkeypatch, key):
    from plangraph.spawner import notify
    root = Path(__file__).resolve().parents[1]
    flag = 'PLANGRAPH_SEAT_AUTOPOST'
    assert all(flag not in path.read_text() for directory in
               (root / 'src', root / 'tools') for path in directory.rglob('*.py'))
    monkeypatch.delenv('PLANGRAPH_SPAWN_NOTIFY_CMD', raising=False)
    monkeypatch.setenv('PLANGRAPH_SEAT_AUTOPOST', '1')
    calls = []
    monkeypatch.setattr(notify, 'deliver_verdict', lambda directory, record, keeper:
                        calls.append((directory, record['item_key'])))
    service = spawn_service(_Runner(), state=tmp_path, keeper_sink=notify.FileInbox(tmp_path))
    record = dict(run_id='run-outcome', role='review', status='completed', ended=1,
                  item_key=key, completion={'event_id': 'run-outcome:completion'})
    service._save(record)
    path = service._path(record['run_id'])
    assert calls == [(path.parent, key)]
    assert json.loads(path.read_text())['item_key'] == key
    assert 'seat_autopost_attempted' not in record
    assert 'events' not in record
    assert not (path.parent / 'seat-autopost.json').exists()


@pytest.mark.parametrize('dispatch', [False, True])
def test_spawn_missing_seat_identity_is_persisted(tmp_path, monkeypatch, dispatch):
    service = _service(tmp_path, monkeypatch)
    if dispatch:
        service = spawn_service(_Runner(), state=tmp_path)
    result = asyncio.run(service.spawn(card={'estimate_tokens': 100},
        prompt=f'Review {KEY} now', role='review', writer_families=('google',),
        dry_run=True, wait_seconds=5))
    assert result['role'] == 'review'
    assert result['item_key'] is None
    assert result['events'].count(MISSING_EVENT) == 1
    stored = json.loads(service._path(result['run_id']).read_text())
    assert stored['role'] == 'review'
    assert stored['item_key'] is None
    assert stored['events'].count(MISSING_EVENT) == 1


@pytest.mark.parametrize('positional', [False, True])
def test_refusal_item_key(tmp_path, monkeypatch, positional):
    service = _service(tmp_path, monkeypatch)

    def refuse(*args, **kwargs):
        raise SpawnRefused('test refusal')

    monkeypatch.setattr(service, '_route', refuse)
    saved = []
    monkeypatch.setattr(service, '_save', lambda record: saved.append(dict(record)))
    card = {'estimate_tokens': 100, 'item': KEY}
    with pytest.raises(SpawnRefused, match='test refusal'):
        asyncio.run(service.spawn(card) if positional else service.spawn(card=card))
    assert len(saved) == 1 and saved[0]['status'] == 'refused'
    assert saved[0]['item_key'] == KEY


@pytest.mark.parametrize('dispatch', [False, True])
@pytest.mark.parametrize('field', ['item', 'item_key'])
def test_item_card_intake(tmp_path, monkeypatch, dispatch, field):
    service = _service(tmp_path, monkeypatch)
    if dispatch:
        service = spawn_service(_Runner(), state=tmp_path)
    card = {'estimate_tokens': 100, field: KEY}
    assert service.route(card).legs
    result = asyncio.run(service.spawn(card=card, prompt=f'Review {OTHER_KEY}', dry_run=True))
    assert result['item_key'] == KEY
    assert json.loads(service._path(result['run_id']).read_text())['item_key'] == KEY


@pytest.mark.parametrize('dispatch', [False, True])
@pytest.mark.parametrize('item,env,expected', [
    (KEY, None, KEY), (None, KEY, KEY), (KEY, OTHER_KEY, KEY),
    (None, None, None), ('bad/item', OTHER_KEY, None), ('', KEY, KEY),
    (12, 'bad/item', None), ('garbage', OTHER_KEY, None), (':x', OTHER_KEY, None),
])
def test_spawn_item_key(tmp_path, monkeypatch, dispatch, item, env, expected):
    monkeypatch.setenv('PLANGRAPH_HOST', 'host-a')
    if env is not None:
        monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', env)
    service = _service(tmp_path, monkeypatch)
    if dispatch:
        service = spawn_service(_Runner(), state=tmp_path)
    saved = []
    original = service._save

    def save(record):
        saved.append(dict(record))
        original(record)

    service._save = save
    card = {'estimate_tokens': 100}
    if item is not None:
        card['item'] = item
    result = asyncio.run(service._spawn(card=card, prompt=f'Review {OTHER_KEY}', dry_run=True))
    assert saved and all(r['item_key'] == expected for r in saved)
    assert result['item_key'] == expected
    assert json.loads(service._path(result['run_id']).read_text())['item_key'] == expected
    if item in ('bad/item', 'garbage', ':x', 12) or env == 'bad/item':
        assert any('invalid item_key' in w for w in result['warnings'])


@pytest.mark.parametrize('item', [KEY, None, 'bad/item'])
def test_continuation_identity(tmp_path, monkeypatch, item):
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', OTHER_KEY)
    parent = dict(run_id='run-parent', role='implement', exit_code=0,
                  cwd=str(tmp_path), chosen_leg={}, item_key=item)
    asyncio.run(service._continue_writer(parent, 'Remaining gaps: fix alpha'))
    child = service.result(parent['continuation_run_id'])
    assert child['item_key'] == (KEY if item == KEY else None)


def test_seat_refusal_identity(tmp_path, monkeypatch):
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    service = _service(tmp_path, monkeypatch)
    seat = DEFAULT_LEGS[0]

    async def refuse(**kwargs):
        raise SpawnRefused('seat refused')

    monkeypatch.setattr(service, '_spawn', refuse)
    plan = SpawnPlan((seat,), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: replace(plan, review_seats=(seat,)))
    out = asyncio.run(service._spawn_seats(card={'item_key': KEY}, prompt='', role='review',
        writer_families=(), review_of=None, model=None, leg=None, benchmark=None, require_bench=False))
    assert out['seats'][0]['item_key'] == KEY


@pytest.mark.parametrize('item,expected', [
    (KEY, KEY), ('bad/item', None), ('garbage', None), (':x', None),
])
def test_listing_identity(tmp_path, item, expected):
    from plangraph.spawner.stall_watchdog import read_runs
    path = tmp_path / 'runs/run-list/record.json'
    path.parent.mkdir(parents=True)
    text = json.dumps(dict(run_id='run-list', item_key=None, completion={'item': item}))
    path.write_text(text)
    assert read_runs(tmp_path)[0]['item_key'] == expected
    assert path.read_text() == text
