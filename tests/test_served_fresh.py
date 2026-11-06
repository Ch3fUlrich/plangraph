"""Hermetic negative-listing confirmation at both admission boundaries."""
import asyncio
import time

import pytest

from plangraph.routing_support import resolver as r
from plangraph.routing_support import opencode_render


@pytest.fixture
def listing(monkeypatch, real_served_models):
    env = {'OPENCODE_CONFIG_CONTENT': '{}'}
    monkeypatch.setattr(r, 'host_probe_env', lambda extra=None: dict(env))
    monkeypatch.setattr(opencode_render, 'render_gateway_providers', lambda env, model: None)
    monkeypatch.setattr(r, 'served_models', real_served_models)
    monkeypatch.setattr(r, '_served_models_real', real_served_models)
    monkeypatch.setattr(r, '_served_cache', {})
    monkeypatch.setattr(r, '_served_inflight', {})
    calls = []
    result = [frozenset({'fake/other'})]

    def probe(config, *, env=None):
        calls.append((config, env or r._probe_child_env.get()))
        return result[0]

    monkeypatch.setattr(r, '_probe_served_models', probe)
    real_served_models('{}', env=env)
    calls.clear()
    return result, calls, env


@pytest.fixture
def stale_listing(listing):
    result, _, _ = listing
    key = next(iter(r._served_cache))
    r._served_cache[key] = (time.monotonic() + 290, result[0])
    return listing


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('case', ['stale', 'absent', 'present', 'unknown', 'variant'])
def test_listing_confirmation(stale_listing, asynchronous, case):
    result, calls, env = stale_listing
    model = 'fake/model#fast' if case == 'variant' else 'fake/model'
    row = dict(harness='opencode', model=model)
    if case == 'present':
        key = next(iter(r._served_cache))
        r._served_cache[key] = (time.monotonic() + 300, frozenset({model}))
    result[0] = (None if case == 'unknown' else
                 frozenset({'fake/model#slow' if case == 'variant' else
                            'fake/other' if case == 'absent' else model}))

    def admit():
        return asyncio.run(r.host_skip_reason_async(row)) if asynchronous else r.host_skip_reason(row)

    expected = 'not_served_on_host: ' + model if case in ('absent', 'variant', 'unknown') else None
    assert admit() == expected
    assert len(calls) == (0 if case == 'present' else 1)
    if calls:
        assert calls[0] == ('{}', env)
    if case == 'stale':
        key = next(iter(r._served_cache))
        assert r._served_cache[key][1] == frozenset({'fake/model'})
        assert r.served_models('{}', env=env) == result[0]
        assert admit() is None
        assert len(calls) == 1


@pytest.mark.parametrize('models', [None, frozenset({'fake/model'}), frozenset({'fake/other'})])
def test_injected_probe_never_reprobes(listing, models):
    _, calls, _ = listing
    reason = r.host_skip_reason(dict(harness='opencode', model='fake/model'), probe=lambda env: models)
    assert reason == ('not_served_on_host: fake/model' if models == frozenset({'fake/other'}) else None)
    assert calls == []


@pytest.mark.parametrize('case', ['missing', 'different', 'new', 'empty_old', 'empty_new'])
def test_cache_origin_and_age(listing, monkeypatch, case):
    from plangraph.routing_support.served_fresh import served_fresh

    result, calls, env = listing
    models = frozenset() if case.startswith('empty') else result[0]
    key = next(iter(r._served_cache))
    monkeypatch.setattr(time, 'monotonic', lambda: 1000)
    if case == 'missing':
        r._served_cache.clear()
    else:
        cached = frozenset({'fake/different'}) if case == 'different' else models
        ttl = 30 if not cached else 300
        r._served_cache[key] = (1000 + ttl - (10 if case in ('different', 'empty_old') else 5), cached)
    result[0] = frozenset({'fake/model'})
    assert served_fresh('fake/model', models, '{}', env) == (case in ('different', 'empty_old'))
    assert len(calls) == (1 if case in ('different', 'empty_old') else 0)


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('models', [None, frozenset(), frozenset({'fake/other'})])
def test_fresh_admission(listing, asynchronous, models):
    result, calls, _ = listing
    key = next(iter(r._served_cache))
    r._served_cache[key] = (time.monotonic() + 300, models)
    result[0] = frozenset({'fake/model'})
    row = dict(harness='opencode', model='fake/model')
    reason = asyncio.run(r.host_skip_reason_async(row)) if asynchronous else r.host_skip_reason(row)
    assert reason == (None if models is None else 'not_served_on_host: fake/model')
    assert calls == []


def test_listing_fixture_is_fresh(listing):
    assert next(iter(r._served_cache.values()))[0] > time.monotonic() + 295


def test_empty_listing_cache_ttl(listing, real_served_models):
    result, calls, env = listing
    r._served_cache.clear()
    result[0] = frozenset()
    assert real_served_models('{}', env=env, clock=lambda: 1000) == frozenset()
    assert next(iter(r._served_cache.values())) == (1030, frozenset())
    assert len(calls) == 1
