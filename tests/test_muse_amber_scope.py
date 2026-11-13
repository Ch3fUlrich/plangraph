"""Unwired payment-order contracts; every input is a fixed local snapshot."""
from dataclasses import replace
from types import SimpleNamespace
import os
import shutil
import socket
import subprocess
import time

import pytest

from plangraph.planner import legs as registrations, review_order
from plangraph.planner.legs import Leg
from plangraph.planner.route_policy import RoutePolicy

AMBER_ID = 'opencode-console-muse-spark'
AMBER_MODEL = 'console-meta/muse-spark-1.3-contributor'


@pytest.fixture(autouse=True)
def muse_sandbox(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail('ordering must not discover catalog, host state, or launch processes')
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'TMPDIR', 'AUTOOS_ROOT'):
        path = tmp_path / name.lower()
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setenv('PLANGRAPH_SETTINGS', str(tmp_path / 'absent-settings.json'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(time, 'time', lambda: 1000.0)
    monkeypatch.setattr(socket, 'gethostname', lambda: 'fixture-host')
    from plangraph.routing_support import resolver, leg_state
    for owner, names in ((socket, ('socket', 'create_connection', 'getaddrinfo')),
                         (subprocess, ('Popen',)), (shutil, ('which',)),
                         (resolver, ('get_catalog_models', '_get_default_catalog_db')),
                         (leg_state, ('leg_status',))):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def muse_catalog_rows():
    return (
        {'id': 'omniroute/cx-gpt-6.1-sol-xhigh', 'tier': 'subscription', 'active': True, 'available': 1},
        {'id': 'agy/gemini-3.8-flash', 'tier': 'free', 'active': True, 'available': 1},
        {'id': 'agy/gemini-4-argon-high', 'tier': 'subscription', 'active': True, 'available': 1},
        {'id': 'mimo/mimo-v2.6-pro', 'tier': 'subscription', 'active': True, 'available': 1},
        {'id': 'omniroute/muse-spark-1.3-contributor', 'tier': 'subscription', 'active': True, 'available': 1},
        {'id': AMBER_MODEL, 'tier': 'paid', 'active': True, 'available': 1},
        {'id': 'omniroute/deepseek-direct-flash', 'tier': 'paid', 'active': True, 'available': 1},
    )


@pytest.fixture
def muse_legs():
    specs = (
        ('cx-gpt-6.1-sol-xhigh', 'subscription', 'omniroute/cx-gpt-6.1-sol-xhigh', 'gpt'),
        ('agy-gemini-3.8-flash', 'free', 'gemini-3.8-flash', 'gemini'),
        ('agy-gemini-4-argon', 'subscription', 'gemini-4-argon-high', 'gemini'),
        ('mimo-v2.6-pro', 'subscription', 'mimo/mimo-v2.6-pro', 'mimo'),
        ('omniroute-muse-spark', 'subscription', 'omniroute/muse-spark-1.3-contributor', 'muse'),
        (AMBER_ID, 'paid', AMBER_MODEL, 'muse'),
        ('deepseek', 'paid', 'omniroute/deepseek-direct-flash', 'deepseek'),
    )
    return tuple(Leg(id, tier, 'agy' if id.startswith('agy-') else 'opencode', model,
                     family, ('128k',), 100000, id == AMBER_ID,
                     roles=('implement', 'review')) for id, tier, model, family in specs)


@pytest.fixture
def muse_plan_inputs(muse_legs, muse_catalog_rows):
    return dict(legs=muse_legs, catalog_rows=muse_catalog_rows,
                policy=RoutePolicy(banned_families=(), off_families=(), paused_harnesses={}),
                leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': {'kind': 'fixed'}},
                benchmark={}, strict=False, now=1000.0)


def ids(legs):
    return tuple(leg.id for leg in legs)


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('repriced', (False, True))
def test_subscription_boundary_survives_ranking(muse_plan_inputs, role, repriced):
    rows = muse_plan_inputs['catalog_rows']
    if repriced:
        rows = tuple(dict(row, tier='paid') if row['id'] in (
            'omniroute/cx-gpt-6.1-sol-xhigh', 'agy/gemini-3.8-flash') else row for row in rows)
    legs = review_order.catalog_tiers(muse_plan_inputs['legs'], rows, role=role)
    result = review_order.payment_order(tuple(reversed(legs)), role)
    costs = [leg.tier for leg in result if leg.tier in ('subscription', 'paid')]
    assert costs == sorted(costs, key=('subscription', 'paid').index)
    if role == 'implement':
        assert ids(result) == (('omniroute-muse-spark', 'mimo-v2.6-pro', 'agy-gemini-4-argon',
                               'cx-gpt-6.1-sol-xhigh', 'agy-gemini-3.8-flash', AMBER_ID, 'deepseek')
                              if repriced else ('cx-gpt-6.1-sol-xhigh', 'agy-gemini-3.8-flash',
                               'omniroute-muse-spark', 'mimo-v2.6-pro', 'agy-gemini-4-argon', AMBER_ID, 'deepseek'))
    assert review_order.payment_order(result, role) == result


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_paid_amber_precedes_deepseek(muse_legs, role):
    sol, free, sub, mimo, pool, amber, deepseek = muse_legs
    trial = replace(free, id='trial', tier='trial')
    other = replace(amber, id='other-paid', model='fixture/other', family='gpt')
    legs = (deepseek, trial, sub, other, free, amber, mimo, pool)
    result = review_order.payment_order(legs, role)
    assert ids(result) == (('agy-gemini-3.8-flash', 'agy-gemini-4-argon', 'trial',
                           'mimo-v2.6-pro', 'omniroute-muse-spark', AMBER_ID, 'deepseek', 'other-paid')
                          if role == 'implement' else ('agy-gemini-4-argon', 'trial',
                           'mimo-v2.6-pro', 'omniroute-muse-spark', 'agy-gemini-3.8-flash', AMBER_ID, 'deepseek', 'other-paid'))
    assert muse_legs[0] == sol and legs[0] == deepseek
    if role == 'review':
        assert result[1] is trial and result[4] is free


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_subscription_walk_without_amber(muse_legs, role):
    other = replace(muse_legs[-1], id='other-paid', model='fixture/other', family='gpt')
    legs = (muse_legs[-1], muse_legs[3], other, muse_legs[2])
    result = review_order.payment_order(legs, role)
    assert ids(result) == (('mimo-v2.6-pro', 'agy-gemini-4-argon', 'deepseek', 'other-paid')
                           if role == 'implement' else ids(legs))
    assert set(ids(result)) == set(ids(legs))


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('explicit_tried', (False, True))
def test_failure_walk_exhausts_subscriptions(muse_legs, role, explicit_tried):
    legs = review_order.payment_order((muse_legs[-1], muse_legs[3], muse_legs[-2], muse_legs[2]), role)
    last, walked = legs[0], [legs[0].id]
    tried = [last.id] if explicit_tried else []
    while True:
        action, successor = review_order.next_cost_leg(legs, tried, last, role=role)
        if successor is None:
            assert action == 'park'
            break
        assert action == ('rotate' if successor.tier == last.tier else 'promote')
        assert successor.id not in walked
        tried.extend([last.id] if not tried else [])
        tried.append(successor.id)
        walked.append(successor.id)
        last = successor
    assert walked == ['mimo-v2.6-pro', 'agy-gemini-4-argon', AMBER_ID, 'deepseek']
    first_tried = [legs[0].id] if explicit_tried else []
    assert review_order.next_cost_leg(legs, first_tried, legs[0], role=role) == ('rotate', legs[1])


@pytest.mark.parametrize('role', (None, 'orchestrate', 'research', 'review'))
def test_other_roles_and_readiness_unchanged(muse_legs, role):
    legs = (muse_legs[-1], muse_legs[3], muse_legs[0])
    assert review_order.payment_order(legs, role) == legs
    if role != 'review':
        assert review_order.next_cost_leg(legs, [], legs[1], role=role) == ('promote', legs[0])
    amber = next(leg for leg in registrations.READINESS_LEGS if leg.id == AMBER_ID)
    assert (amber.tier, amber.trains_on_data, amber.roles) == ('subscription', False, ('review',))
    legacy = (SimpleNamespace(id=AMBER_ID, model=AMBER_MODEL, harness='opencode'),
              SimpleNamespace(id='old'))
    assert review_order.payment_order(legacy, 'review') == legacy


@pytest.mark.parametrize('role', (None, 'orchestrate', 'implement', 'review'))
@pytest.mark.parametrize('variant', ('direct', 'alias', 'inactive', 'wrong-provider'))
def test_catalog_contract_uses_role_and_exact_identity(muse_legs, role, variant):
    amber = replace(muse_legs[-2], id='renamed', tier='subscription')
    row = {'id': AMBER_MODEL, 'tier': 'free', 'active': variant != 'inactive'}
    if variant in ('alias', 'wrong-provider'):
        row.update(id='fixture/alias', provider_id='console-meta' if variant == 'alias' else 'other',
                   upstream_id='muse-spark-1.3-contributor')
    wrong = replace(amber, harness='agy')
    sol = {'id': muse_legs[0].model, 'tier': 'free'}
    result = review_order.catalog_tiers((amber, wrong, muse_legs[0]), (row, sol), role=role)
    projected = 'subscription' if variant == 'wrong-provider' else 'free'
    assert tuple(leg.tier for leg in result) == (
        'paid' if role in ('implement', 'review') else projected, projected, 'free')
    assert row['tier'] == 'free' and result[0].id == 'renamed'


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_payment_order_preserves_nonprefix_free_slots(muse_legs, role):
    free = replace(muse_legs[1], id='ordinary-free')
    trial = replace(free, id='ordinary-trial', tier='trial')
    legs = (muse_legs[-1], free, muse_legs[3], trial, muse_legs[-2], muse_legs[2])
    result = review_order.payment_order(legs, role)
    assert ids(result) == ('mimo-v2.6-pro', 'ordinary-free', 'agy-gemini-4-argon',
                           'ordinary-trial', AMBER_ID, 'deepseek')
    assert result[1] is free and result[3] is trial


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_paid_precedence_uses_exact_amber_identity(muse_legs, role):
    amber, deepseek = muse_legs[-2:]
    renamed = replace(amber, id='renamed')
    wrong = replace(amber, harness='agy')
    assert review_order.payment_order((deepseek, renamed), role) == (renamed, deepseek)
    assert review_order.payment_order((deepseek, wrong), role) == (deepseek, wrong)


def test_declared_subscription_writer_identities():
    assert registrations.AMBER_LEG_ID == AMBER_ID and registrations.AMBER_MODEL == AMBER_MODEL
    identities = registrations.SUBSCRIPTION_WRITER_IDENTITIES
    for pair in (('opencode', 'omniroute/cx-gpt-6.1-sol-xhigh'),
                 ('opencode', 'omniroute/cx-gpt-6.1-sol'), ('agy', 'claude-sonnet-5-5-high'),
                 ('agy', 'gemini-4-argon-high'), ('opencode', 'mimo/mimo-v2.6-pro'),
                 ('opencode', 'omniroute/muse-spark-1.3-contributor')):
        assert pair in identities
    assert ('agy', 'gemini-3.8-flash') not in identities
    assert 'cx-gpt-6.1-sol' not in ids(registrations.DEFAULT_LEGS)
    assert {('opencode', 'mimo/mimo-v2.6-flash'),
            ('opencode', 'omniroute/deepseek-flash')} <= identities
    assert ('opencode', AMBER_MODEL) not in identities
