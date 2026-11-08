import asyncio
import pytest
from plangraph.planner.routing import SpawnRefused
from registry_route_util import _Runner, _service
from plangraph.spawner.runner import spawn_service


def test_seat_card_key_and_missing_warning(monkeypatch):
    from plangraph.spawner.item_key import annotate_item
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    record = {'role': 'review'}
    annotate_item(record, {'item_key': 'plangraph:D-5047'})
    assert record['item_key'] == 'plangraph:D-5047'
    missing = {'role': 'judge'}
    annotate_item(missing, {})
    assert missing['events'][0]['event'] == 'seat_item_missing'


def test_seat_prompt_identity(monkeypatch):
    from plangraph.spawner.item_key import annotate_item
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
    record = {'role': 'review', 'prompt': 'Review plangraph:D-5047 now'}
    annotate_item(record, {})
    assert record['item_key'] == 'plangraph:D-5047'


@pytest.mark.parametrize('enabled,code', [(False, 0), (True, 0), (True, 2)])
def test_seat_autopost(tmp_path, monkeypatch, enabled, code):
    import json
    from plangraph.spawner import item_key
    monkeypatch.setenv('PLANGRAPH_SEAT_AUTOPOST', '1' if enabled else '0')
    monkeypatch.setenv('PG_URL', 'http://keeper.invalid')
    record = dict(run_id='run-test', role='review', status='completed',
                  item_key='plangraph:D-5047', served_model='gpt-5',
                  completion={'head_sha': 'a' * 40})
    path = tmp_path / 'runs/run-test/record.json'
    path.parent.mkdir(parents=True)
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps({'items': [{'key': record['item_key'], 'writer_model': 'gemini'}]}))
    monkeypatch.setenv('PLANGRAPH_SEAT_SNAPSHOT', str(snapshot))
    calls = []
    monkeypatch.setattr(item_key, '_post_verdict', lambda args: calls.append(args) or code)
    item_key.autopost(record, path)
    item_key.autopost(record, path)
    assert len(calls) == int(enabled)
    if enabled:
        args = calls[0]
        assert args == ['--seat-dir', str(path.parent), '--head', 'a' * 40,
                        '--key', record['item_key'], '--writer-family', 'google',
                        '--token-dir', str(item_key.Path.home() / '.config/plangraph/tokens'),
                        '--url', 'http://keeper.invalid']
        assert record['events'][-1]['exit_code'] == code
        assert record['status'] == 'completed'


@pytest.mark.parametrize('positional', [False, True])
def test_refusal_item_key(tmp_path, monkeypatch, positional):
    service = _service(tmp_path, monkeypatch)
    def refuse(*args, **kwargs):
        raise SpawnRefused('test refusal')
    monkeypatch.setattr(service, '_route', refuse)
    saved = []
    monkeypatch.setattr(service, '_save', lambda record: saved.append(dict(record)))
    card = {'estimate_tokens': 100, 'item': 'p:x'}
    with pytest.raises(SpawnRefused, match='test refusal'):
        asyncio.run(service.spawn(card) if positional else service.spawn(card=card))
    assert len(saved) == 1 and saved[0]['status'] == 'refused'
    assert saved[0]['item_key'] == 'p:x'


@pytest.mark.parametrize('dispatch', [False, True])
def test_item_card_intake(tmp_path, monkeypatch, dispatch):
    service = _service(tmp_path, monkeypatch)
    if dispatch:
        service = spawn_service(_Runner(), state=tmp_path)
    card = {'estimate_tokens': 100, 'item': 'p:card'}
    # route is the public card-check entry point shared by both Python paths.
    assert service.route(card).legs
    result = asyncio.run(service.spawn(card=card, prompt='', dry_run=True))
    assert result['item_key'] == 'p:card'


@pytest.mark.parametrize('dispatch', [False, True])
@pytest.mark.parametrize('item,env,expected', [
    ('p:card', None, 'p:card'), (None, 'p:env', 'p:env'),
    ('p:card', 'p:env', 'p:card'), (None, None, None),
    ('bad/item', 'p:env', None), ('', 'p:env', 'p:env'), (12, 'bad/item', None)])
def test_spawn_item_key(tmp_path, monkeypatch, dispatch, item, env, expected):
    monkeypatch.delenv('PLANGRAPH_SPAWN_ITEM', raising=False)
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
    result = asyncio.run(service._spawn(card=card, prompt='', dry_run=True))
    assert saved and all(r['item_key'] == expected for r in saved)
    assert result['item_key'] == expected
    if item == 'bad/item' or env == 'bad/item':
        assert any('invalid item_key' in w for w in result['warnings'])


@pytest.mark.parametrize('item', ['p:parent', None, 'bad/item'])
def test_continuation_identity(tmp_path, monkeypatch, item):
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', 'p:other')
    parent = dict(run_id='run-parent', role='implement', exit_code=0,
                  cwd=str(tmp_path), chosen_leg={}, item_key=item)
    asyncio.run(service._continue_writer(parent, 'Remaining gaps: fix alpha'))
    child = service.result(parent['continuation_run_id'])
    assert child['item_key'] == ('p:parent' if item == 'p:parent' else None)


def test_seat_refusal_identity(tmp_path, monkeypatch):
    from plangraph.planner.legs import DEFAULT_LEGS
    service = _service(tmp_path, monkeypatch)
    seat = DEFAULT_LEGS[0]
    async def refuse(**kwargs):
        raise SpawnRefused('seat refused')
    monkeypatch.setattr(service, '_spawn', refuse)
    # replace() needs a real plan; use the existing routing plan with one seat.
    from dataclasses import replace
    from plangraph.planner.routing import SpawnPlan
    plan = SpawnPlan((seat,), (), 'test', (), ())
    monkeypatch.setattr(service, '_route', lambda *a, **kw: replace(plan, review_seats=(seat,)))
    out = asyncio.run(service._spawn_seats(card={'item': 'p:seat'}, prompt='', role='review',
        writer_families=(), review_of=None, model=None, leg=None, benchmark=None, require_bench=False))
    assert out['seats'][0]['item_key'] == 'p:seat'


@pytest.mark.parametrize('item,expected', [('p:good', 'p:good'), ('bad/item', None)])
def test_listing_identity(tmp_path, item, expected):
    import json
    from plangraph.spawner.stall_watchdog import read_runs
    path = tmp_path / 'runs/run-list/record.json'
    path.parent.mkdir(parents=True)
    text = json.dumps(dict(run_id='run-list', item_key=None, completion={'item': item}))
    path.write_text(text)
    assert read_runs(tmp_path)[0]['item_key'] == expected
    assert path.read_text() == text
