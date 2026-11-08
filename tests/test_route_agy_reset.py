"""Hermetic routing pins for the AGY reset and operator role/order rules."""
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import plan_spawn, route_card
from plangraph.review.family import model_family
from plangraph.routing_support import leg_state


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    from plangraph.routing_support import resolver

    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    store = leg_state.LegStateStore(tmp_path / 'leg-state.sqlite')
    monkeypatch.setattr(leg_state, '_default_store', lambda: store)
    return store


def plan(store, role='review', **kwargs):
    return plan_spawn(
        dict(estimate_tokens=1000, diff_ref=dict(repo=str(Path.cwd()), base='HEAD', head='HEAD')), role=role,
        writer_families=('xiaomi',), leg_reader=store, now=100,
        policy=RoutePolicy(), **kwargs)


def test_agy_gpt_oss_review_only_and_cross_family(boundary):
    leg = next(l for l in DEFAULT_LEGS if l.id == 'agy-gpt-oss-120b')
    assert (leg.harness, leg.model, leg.tier, leg.roles) == (
        'agy', 'gpt-oss-120b-medium', 'subscription', ('review',))
    assert leg.family == model_family(leg.model) == 'openai'
    assert leg in plan(boundary).legs
    for role in ('implement', 'orchestrate'):
        assert leg not in plan(boundary, role).legs
    # A cx writer is family "gpt" (legs.py); the keeper treats gpt and openai as one family.
    for writer in ('openai', 'gpt'):
        excluded = plan_spawn(
            dict(estimate_tokens=1000, diff_ref=dict(repo=str(Path.cwd()), base='HEAD', head='HEAD')),
            role='review', writer_families=(writer,), leg_reader=boundary, now=100,
            policy=RoutePolicy())
        assert leg not in excluded.legs, writer
        assert any(s['leg'] == leg.id and s['reason'] == 'excluded_family'
                   for s in excluded.skipped), writer
    assert all(l.id != 'ovh-gpt-oss-120b' and
               l.model != 'omniroute/ovh-gpt-oss-120b' for l in DEFAULT_LEGS)


@pytest.mark.parametrize('prefix', ['', 'agy/'])
def test_probe_pass_clears_stored_agy_pause(boundary, prefix):
    key = prefix + 'gemini-3.8-flash'
    # Two failed probes establish a harness pause; it has not expired at route time.
    for now in (10, 20):
        leg_state.record_probe_fail(key, 'probe unavailable', 'test',
                                    now=now, down_s=1000)
    assert boundary.leg_status(key, now=100)['state'] == 'down'
    paused = plan(boundary)
    assert 'agy-gemini-3.8-flash' not in {l.id for l in paused.legs}
    assert any(s['leg'] == 'agy-gemini-3.8-flash' and s['reason'] == 'down'
               for s in paused.skipped)
    leg_state.record_probe_pass(key, 'test', now=30)
    assert boundary.leg_status(key, now=100)['state'] == 'ok'
    assert 'agy-gemini-3.8-flash' in {l.id for l in plan(boundary).legs}


def test_no_claude_implementers_and_haiku_last(boundary):
    assert all('implement' not in l.roles for l in DEFAULT_LEGS
               if l.family == 'claude')
    assert all(l.family != 'claude' for l in plan(boundary, 'implement').legs)
    reviews = plan(boundary).legs
    haiku = next(i for i, l in enumerate(reviews) if l.id == 'claude-haiku-5.5')
    assert all(i < haiku for i, l in enumerate(reviews) if l.family != 'claude')


@pytest.mark.parametrize('role', ['implement', 'review', 'orchestrate'])
def test_free_and_subscription_before_paid_deepseek(boundary, role):
    # route_card orders candidates; plan_spawn separately enforces retained roles.
    # Include the disabled paid fallback to pin ordering even while it is parked.
    from dataclasses import replace

    legs = tuple(replace(l, disabled=None) for l in DEFAULT_LEGS
                 if role in l.roles or l.id == 'deepseek')
    # The paid fallback serves implement only; other roles must not offer it.
    if role != 'implement':
        assert all(l.id != 'deepseek' for l in plan(boundary, role).legs)
        # Haiku-last (D-3763: Haiku review only, last) is pinned by its own test above;
        # this ordering check covers the non-Claude legs, which must not be empty.
        legs = tuple(l for l in legs if l.family != 'claude')
        assert any(l.tier in ('free', 'subscription') for l in legs)
    ordered = route_card(dict(estimate_tokens=1000), legs=legs, role=role).legs
    paid = next(i for i, l in enumerate(ordered) if l.id == 'deepseek')
    assert all(i < paid for i, l in enumerate(ordered)
               if l.tier in ('free', 'subscription'))
