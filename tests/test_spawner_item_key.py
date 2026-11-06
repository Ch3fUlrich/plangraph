import asyncio
import pytest
from registry_route_util import _Runner, _service
from plangraph.spawner.runner import spawn_service


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
