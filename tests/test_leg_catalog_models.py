"""Native harness model evidence must agree with catalog IDs before launch."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from plangraph.spawner import leg_probe
from plangraph.routing_support.leg_state import LegStateStore


def test_native_error_list_matches_ids_and_books_unknown_down(tmp_path, monkeypatch):
    fixture = json.loads((Path(__file__).parent / 'fixtures/spawner_seat_fallthrough.json').read_text())
    # Real captured error text, including display names rather than model IDs.
    def find(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from find(item)
        elif isinstance(value, list):
            for item in value:
                yield from find(item)
        elif isinstance(value, str) and 'Available models:' in value:
            yield value
    text = next(find(fixture))
    models = leg_probe.harness_model_ids(text)
    assert 'claude-opus-5-5-high' in models
    assert 'claude-opus-4-6-thinking' not in models
    assert 'gemini-3.8-flash-high' in models
    store = LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda _: '/fake/agy')
    monkeypatch.setattr(leg_probe.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout=text, stderr='', returncode=1))
    bad = SimpleNamespace(harness='agy', model='claude-opus-4-6-thinking')
    good = SimpleNamespace(harness='agy', model='claude-opus-5-5-high')
    result = asyncio.run(leg_probe.catalog_model_check(bad, store))
    assert result['reason'] == 'catalog_model_unknown'
    assert store.leg_status(leg_probe.key(bad))['state'] == 'down'
    assert asyncio.run(leg_probe.catalog_model_check(good, store)) is None
    assert store.leg_status(leg_probe.key(good))['state'] == 'ok'


def test_unreadable_model_list_is_infrastructure(tmp_path, monkeypatch):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda _: '/fake/agy')
    monkeypatch.setattr(leg_probe.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='', stderr='offline', returncode=1))
    leg = SimpleNamespace(harness='agy', model='model')
    result = asyncio.run(leg_probe.catalog_model_check(leg, store))
    assert result['infra']
    assert store.leg_status(leg_probe.key(leg))['state'] == 'ok'


def test_catalog_probe_refuses_unknown_before_spawning(tmp_path, monkeypatch):
    from plangraph.planner import legs
    from plangraph.routing_support.liveness import unavailable
    store = LegStateStore(tmp_path / 'legs.sqlite')
    leg = SimpleNamespace(id='test-unknown', harness='agy', model='old-model', roles=('review',))
    monkeypatch.setattr(legs, 'spawnable_legs', lambda: (leg,))
    monkeypatch.setattr(legs, 'leg_active', lambda _: True)
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda _: '/fake/agy')
    monkeypatch.setattr(leg_probe.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='new-model\n', stderr='', returncode=0))
    class Runner:
        leg_reader = store
        async def spawn(self, **kwargs):
            raise AssertionError('unknown model launched')
    result = asyncio.run(leg_probe.probe_many(Runner(), [leg.id]))
    assert result['probes'][0]['reason'] == 'catalog_model_unknown'
    assert unavailable(store.leg_status(leg_probe.key(leg)))


def test_native_tsv_and_rendered_alias_are_admitted(tmp_path, monkeypatch):
    text = 'gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n'
    assert leg_probe.harness_model_ids(text) == {'gemini-3.8-flash-high'}
    store = LegStateStore(tmp_path / 'legs.sqlite')
    monkeypatch.setattr(leg_probe.shutil, 'which', lambda _: '/fake/agy')
    monkeypatch.setattr(leg_probe.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout=text, stderr='', returncode=0))
    leg = SimpleNamespace(harness='agy', model='gemini-3.8-flash')
    assert asyncio.run(leg_probe.catalog_model_check(leg, store)) is None
