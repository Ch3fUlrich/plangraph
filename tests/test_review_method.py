import asyncio
import json
import os
import subprocess
import sys

import pytest

from plangraph.settings import ReviewMethod, review_method
from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.service import SpawnService


def test_defaults(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    assert review_method() == ReviewMethod()
    assert review_method().seats == {'standard': 1, 'high': 2}


def test_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'review_method': {'max_rounds': 4},
        'projects': {'demo': {'review_method': {'mode': 'per-item'}}},
        'phases': {'demo:P': {'review_method': {'max_rounds': 2}}}}))
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    value = review_method('demo', 'demo:P')
    assert value.mode == 'per-item' and value.max_rounds == 2
    assert review_method().max_rounds == 4


@pytest.mark.parametrize('field,value', [('mode', 'bad'), ('seats', {'high': 0}),
    ('distinct_families', 1), ('family_source', ''), ('max_rounds', 0),
    ('fixer_split_threshold', True)])
def test_invalid(field, value):
    with pytest.raises(ValueError, match=field):
        ReviewMethod(**{field: value})


def test_spawn_scope(tmp_path, monkeypatch):
    service = SpawnService(runner=object(), state=tmp_path)
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    with pytest.raises(SpawnRefused) as caught:
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review', review_scope='per-fixer'))
    assert caught.value.reason == 'review_method_batch_per_fixer'
    path = tmp_path / 'settings.json'
    path.write_text('{"review_method": {"mode": "per-item"}}')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    # A sentinel route proves admission reached routing rather than refusing.
    def route(*args, **kwargs):
        raise RuntimeError('admitted')
    service._route = route
    with pytest.raises(RuntimeError, match='admitted'):
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review', review_scope='per-fixer'))


def test_cli_show(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    result = subprocess.run([sys.executable, '-m', 'plangraph.settings', 'show',
        '--project', 'demo', '--phase', 'demo:P'], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": "src"})
    value = json.loads(result.stdout)
    assert value['mode'] == 'batch' and value['source'] == 'default'


def test_toml_partial_seats(tmp_path, monkeypatch):
    path = tmp_path / 'settings.toml'
    path.write_text('[review_method.seats]\nhigh = 3\n')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(path))
    assert review_method().seats == {'standard': 1, 'high': 3}


def test_dispatch_scope(tmp_path, monkeypatch):
    from plangraph.spawner.runner import spawn_service
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    service = spawn_service(runner=object(), state=tmp_path)
    with pytest.raises(SpawnRefused, match='review_method_batch_per_fixer'):
        asyncio.run(service._spawn(card={'key': 'demo:C'}, role='review',
                                  review_scope='per-fixer'))


def test_plan_phase_carries_method(tmp_path, monkeypatch):
    from keeper_util import World, defn
    from plangraph.planner.phase import plan_phase
    monkeypatch.delenv('PLANGRAPH_SETTINGS', raising=False)
    world = World(tmp_path / 'keeper')
    try:
        world.store.create_items(world.p('router'), [defn('P', kind='gate'),
            defn('GOAL', ['P'], kind='goal')])
        plan = plan_phase(world.store, world.p('router'), 'demo:P', repo_root=tmp_path)
        assert plan['review_method']['mode'] == 'batch'
        assert plan['review_method']['max_rounds'] == 3
    finally:
        world.close()
