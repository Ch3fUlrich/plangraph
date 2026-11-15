"""Qualified cost inputs and partial review measurements remain independent."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS
from plangraph.planner.review_order import catalog_tiers, review_key
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import plan_spawn

TABLE = {leg.id: leg for leg in DEFAULT_LEGS}
CARD = {'estimate_tokens': 1000}


@pytest.mark.parametrize('model', ['gemini-4-argon-high', 'agy/gemini-4-argon-high'])
@pytest.mark.parametrize('tier', ['free', 'subscription', 'paid'])
def test_unqualified_or_other_provider_row_cannot_override_declared_tier(model, tier):
    leg = replace(TABLE['agy-gemini-4-argon'], model=model, tier=tier)
    rows = [{'id': 'gemini-4-argon-high', 'tier': 'paid' if tier != 'paid' else 'free'},
            {'id': 'other/gemini-4-argon-high', 'tier': 'paid',
             'provider_id': 'other', 'upstream_id': 'gemini-4-argon-high'}]
    assert catalog_tiers((leg,), rows) == (leg,)


def test_injected_catalog_is_the_only_tier_and_admission_snapshot(monkeypatch):
    def ambient():
        pytest.fail('injected planning must not read the ambient catalog')
    monkeypatch.setattr('plangraph.routing_support.resolver.get_catalog_models', ambient)
    leg = replace(TABLE['agy-gemini-4-argon'], model='agy/injected-model')
    rows = [{'id': leg.model, 'tier': 'paid', 'available': 1, 'active': True}]
    plan = plan_spawn(CARD, role='review', legs=(leg,), catalog_rows=rows,
                      writer_families=('gpt',), policy=RoutePolicy(),
                      leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': None})
    assert plan.legs == (replace(leg, tier='paid'),)
    assert plan.candidates[0]['tier'] == 'paid'


@pytest.mark.parametrize('row_key', ['model', 'id'])
def test_partial_benchmarks_keep_each_cost_tier_and_measured_order(row_key):
    # Deliberately reverse table order/cost and include two measured peers.
    specs = [('opencode-go-gemini-4-argon', 'paid'), ('agy-gpt-oss-120b', 'subscription'),
             ('opencode-big-pickle', 'free'), ('agy-gemini-4-argon', 'subscription'),
             ('opencode-go-deepseek-v4-flash', 'paid')]
    legs = tuple(replace(TABLE[id], tier=tier) for id, tier in specs)
    assert len({leg.id for leg in legs}) == len({leg.model for leg in legs}) == len(legs)
    rows = {getattr(leg, row_key): {'aa_intelligence': 47, 'cost_per_task_usd': 0,
                                  'pool_burn_weight': 0,
                                  'verified_tok_per_s': 200 if leg.family == 'deepseek' else 100}
            for leg in legs if leg.tier != 'free'}
    plan = plan_spawn(CARD, role='review', legs=legs, catalog_rows=(), benchmark=rows,
                      writer_families=('mimo',), policy=RoutePolicy(),
                      leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': None})
    assert [leg.id for leg in plan.legs] == ['opencode-big-pickle', 'agy-gpt-oss-120b',
        'agy-gemini-4-argon', 'opencode-go-deepseek-v4-flash', 'opencode-go-gemini-4-argon']
    assert [leg.tier for leg in plan.review_seats] == ['free', 'subscription']
    assert [row['p_source'] for row in plan.candidates] == ['prior', *['measured'] * 4]


def test_review_key_applies_measurement_gate_per_leg():
    free = TABLE['opencode-big-pickle']
    paid = replace(TABLE['agy-gemini-4-argon'], tier='paid')
    assert review_key(free, TIERS.index(free.tier), 1) < review_key(
        paid, TIERS.index(paid.tier), 0, measured=True)
    # Gemini gets no hand-set preference over an earlier same-tier Qwen/GLM leg.
    gemini = TABLE['agy-gemini-4-argon']
    assert review_key(free, TIERS.index('free'), 0) < review_key(gemini, TIERS.index('free'), 1)


def test_partial_benchmarks_do_not_pin_unmeasured_families():
    legs = tuple(replace(TABLE[id], tier='subscription', tool_mode='tools') for id in (
        'agy-gemini-4-argon', 'mimo-v2.6-flash', 'agy-gpt-oss-120b'))
    row = {'aa_intelligence': 47, 'cost_per_task_usd': 0, 'pool_burn_weight': 0}
    plan = plan_spawn(CARD, role='review', legs=legs, catalog_rows=(),
                      benchmark={legs[-1].model: row}, writer_families=('claude',),
                      policy=RoutePolicy(),
                      leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': None})
    assert [leg.id for leg in plan.legs] == ['agy-gpt-oss-120b', 'mimo-v2.6-flash', 'agy-gemini-4-argon']
    assert [detail['p_source'] for detail in plan.candidates] == ['measured', 'prior', 'prior']


def test_big_pickle_uses_the_registered_glm_family_and_policy():
    from plangraph.review.family import writer_key
    leg = TABLE['opencode-big-pickle']
    assert writer_key(leg.family) == 'zhipu'
    assert RoutePolicy(off_families=('zhipu',)).skip_reason(leg, 'review') == 'off_family'
    plan = plan_spawn(CARD, role='review', legs=(leg,), catalog_rows=(),
                      writer_families=('zhipu',), policy=RoutePolicy(),
                      leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': None})
    assert not plan.legs and plan.skipped[0]['reason'] == 'excluded_family'
