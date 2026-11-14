"""Unwired payment ordering uses fixed local legs, never the live registrations."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from plangraph.planner import review_order
from plangraph.planner.legs import TIERS, Leg

AMBER_ID = 'opencode-console-muse-spark'
AMBER_MODEL = 'console-meta/muse-spark-1.3-contributor'


@pytest.fixture
def muse_legs():
    specs = (
        ('cx-gpt-6.1-sol-xhigh', 'subscription', 'omniroute/cx-gpt-6.1-sol-xhigh', 'gpt'),
        ('agy-gemini-4-hydrogen-medium', 'free', 'gemini-4-hydrogen-medium', 'gemini'),
        ('agy-gemini-4-argon', 'subscription', 'gemini-4-argon-high', 'gemini'),
        ('mimo-v2.6-pro', 'subscription', 'mimo/mimo-v2.6-pro', 'mimo'),
        ('omniroute-muse-spark', 'subscription', 'omniroute/muse-spark-1.3-contributor', 'muse'),
        (AMBER_ID, 'subscription', AMBER_MODEL, 'muse'),
        ('deepseek', 'paid', 'omniroute/deepseek-direct-flash', 'deepseek'),
    )
    return tuple(Leg(id, tier, 'agy' if id.startswith('agy-') else 'opencode', model,
                     family, ('128k',), 100000, False, roles=('implement', 'review'))
                 for id, tier, model, family in specs)


def ids(legs):
    return tuple(leg.id for leg in legs)


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('entry', ('raw', 'catalog-paid', 'catalog-free'))
def test_subscription_boundary_survives_ranking(muse_legs, role, entry):
    """Without designated writers, preserve ranked ties within the payment classes."""
    sub, mimo, pool, amber, deepseek = muse_legs[2:]
    legs = (deepseek, pool, amber, mimo, sub)
    if entry != 'raw':
        legs = review_order.catalog_tiers(legs, ({'id': AMBER_MODEL,
                                                'tier': entry.removeprefix('catalog-')},))
    before = tuple(vars(leg).copy() for leg in legs)
    result = review_order.payment_order(iter(legs), role)
    assert ids(result) == (pool.id, mimo.id, sub.id, AMBER_ID, deepseek.id)
    assert review_order.payment_order(result, role) == result
    assert tuple(vars(leg) for leg in legs) == before


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('entry', ('raw', 'catalog-paid', 'catalog-free'))
def test_subscription_boundary_promotes_free_writer_prefix_only_for_implement(muse_legs, role, entry):
    """Implement promotes the free writer-prefix Gemini; review keeps its slot."""
    legs = muse_legs
    if entry != 'raw':
        legs = review_order.catalog_tiers(legs, ({'id': AMBER_MODEL,
                                                'tier': entry.removeprefix('catalog-')},))
    legs = tuple(reversed(legs))
    before = tuple(vars(leg).copy() for leg in legs)
    result = review_order.payment_order(legs, role)
    assert ids(result) == (('cx-gpt-6.1-sol-xhigh', 'agy-gemini-4-hydrogen-medium',
                           'omniroute-muse-spark', 'mimo-v2.6-pro', 'agy-gemini-4-argon',
                           AMBER_ID, 'deepseek') if role == 'implement' else
                          ('omniroute-muse-spark', 'mimo-v2.6-pro', 'agy-gemini-4-argon',
                           'cx-gpt-6.1-sol-xhigh', AMBER_ID, 'agy-gemini-4-hydrogen-medium', 'deepseek'))
    assert review_order.payment_order(result, role) == result
    assert tuple(vars(leg) for leg in legs) == before


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('tier', ('trial', 'free'))
def test_payment_order_preserves_nonprefix_free_slots(muse_legs, role, tier):
    """Keep ordinary trial/free slots at both ends and between movable legs."""
    sol, gemini, sub, mimo, pool, amber, deepseek = muse_legs
    front = replace(gemini, id='ordinary-front', tier=tier)
    middle = replace(front, id='ordinary-middle')
    back = replace(front, id='ordinary-back')
    legs = (front, deepseek, middle, amber, sub, back)
    result = review_order.payment_order(legs, role)
    assert result == (front, sub, middle, amber, deepseek, back)
    assert all(result[index] is legs[index] for index in (0, 2, 5))


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('tier', ('trial', 'free'))
def test_trial_or_free_slot_keeps_position_unless_writer_prefix_leg(muse_legs, role, tier):
    """Trial or free slots stay fixed unless the leg is in the writer prefix."""
    sol, gemini, sub, mimo, pool, amber, deepseek = muse_legs
    front = replace(gemini, id='ordinary-front', tier=tier)
    middle = replace(gemini, id='ordinary-middle', tier='free' if tier == 'trial' else 'trial')
    legs = (front, deepseek, middle, sub, amber, sol, gemini, mimo, pool)
    result = review_order.payment_order(legs, role)
    assert result[2] is middle
    assert result[0] is front
    assert ids(result) == (('ordinary-front', sol.id, 'ordinary-middle', gemini.id, sub.id,
                           mimo.id, pool.id, AMBER_ID, deepseek.id) if role == 'implement'
                          else ('ordinary-front', sub.id, 'ordinary-middle', sol.id, mimo.id,
                                pool.id, gemini.id, AMBER_ID, deepseek.id))


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('repriced', (False, True))
def test_implement_prefix_cannot_cross_subscription_boundary(muse_legs, role, repriced):
    """Historical name retained: only the tail after the prefix has a barrier."""
    sol, gemini, sub, mimo, pool, amber, deepseek = muse_legs
    if repriced:
        sol, gemini = (replace(leg, tier='paid') for leg in (sol, gemini))
    legs = (deepseek, amber, sub, sol, gemini)
    result = review_order.payment_order(legs, role)
    prefix = (sol, gemini) if role == 'implement' else ()
    expected = (prefix + (sub, amber, deepseek) if role == 'implement' else
                (sub, amber, deepseek, sol, gemini) if repriced else
                (sub, sol, amber, deepseek, gemini))
    assert result == expected
    tail = result[len(prefix):]
    costs = [review_order.payment_tier(leg, role) for leg in tail
             if review_order.payment_tier(leg, role) in ('subscription', 'paid')]
    assert costs == sorted(costs, key=('subscription', 'paid').index)
    assert ids(result).index(AMBER_ID) < ids(result).index(deepseek.id)


@pytest.mark.parametrize('sol_tier', ('trial', 'free', 'subscription', 'paid'))
@pytest.mark.parametrize('gemini_tier', ('trial', 'free', 'subscription', 'paid'))
def test_trial_free_or_repriced_writer_prefix_moves_to_front(muse_legs, sol_tier, gemini_tier):
    sol, gemini, sub, mimo, pool, amber, deepseek = muse_legs
    legs = review_order.catalog_tiers((deepseek, amber, sub, gemini, sol), (
        {'id': deepseek.model, 'tier': 'paid'},
        {'id': amber.model, 'tier': 'paid'},
        {'provider_id': sub.harness, 'upstream_id': sub.model, 'tier': 'subscription'},
        {'id': sol.model, 'tier': sol_tier},
        {'provider_id': gemini.harness, 'upstream_id': gemini.model, 'tier': gemini_tier}))
    assert tuple(leg.tier for leg in legs) == ('paid', 'paid', 'subscription', gemini_tier, sol_tier)
    assert (legs[-1].tier, legs[-2].tier) == (sol_tier, gemini_tier)
    result = review_order.payment_order(legs, 'implement')
    assert ids(result) == (sol.id, gemini.id, sub.id, AMBER_ID, deepseek.id)
    assert review_order.payment_order(result, 'implement') == result


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_nondesignated_paid_leg_never_precedes_subscription_class_leg(muse_legs, role):
    sol, gemini, sub, mimo, pool, amber, deepseek = muse_legs
    other = replace(pool, tier='paid')
    sol, gemini = (replace(leg, tier='paid') for leg in (sol, gemini))
    result = review_order.payment_order((other, gemini, deepseek, sub, amber, sol, mimo), role)
    prefix = (sol, gemini) if role == 'implement' else ()
    assert result == (prefix + (sub, mimo, other, amber, deepseek) if role == 'implement'
                      else (sub, mimo, other, gemini, amber, deepseek, sol))
    tail = result[len(prefix):]
    assert tail[:2] == (sub, mimo)
    assert tail.index(other) > tail.index(sub)
    assert tail.index(other) > tail.index(mimo)


@pytest.mark.parametrize('tier', TIERS)
def test_every_recognized_writer_tier_has_a_movable_prefix_slot(muse_legs, tier):
    """Extending TIERS must not admit a prefix leg that movable slots omit."""
    sol, gemini, sub, mimo, pool, amber, deepseek = muse_legs
    sol = replace(sol, tier=tier)
    legs = (deepseek, sub, amber, sol)
    result = review_order.payment_order(legs, 'implement')
    assert result == (sol, sub, amber, deepseek)
    assert sorted(ids(result)) == sorted(ids(legs))
    assert review_order.payment_order(result, 'implement') == result


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('tier', ('missing', None, 'unknown'))
@pytest.mark.parametrize('id', ('legacy', 'cx-gpt-6.1-sol-xhigh', None, 'missing'))
def test_tierless_and_unknown_legs_stay_in_place(muse_legs, role, tier, id):
    legacy = SimpleNamespace(**({} if id == 'missing' else {'id': id}),
                             **({} if tier == 'missing' else {'tier': tier}))
    sub, amber, deepseek = muse_legs[3], *muse_legs[-2:]
    assert review_order.payment_tier(legacy, role) is None
    result = review_order.payment_order((deepseek, legacy, sub, amber), role)
    assert result == (sub, legacy, amber, deepseek)
    assert result[1] is legacy


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('tier', ('trial', 'free', 'subscription', 'paid'))
def test_idless_legs_keep_trial_free_slots_and_payment_boundary(muse_legs, role, tier):
    legacy = SimpleNamespace(tier=tier)
    sub, amber, deepseek = muse_legs[3], *muse_legs[-2:]
    expected = ((legacy, sub, amber, deepseek) if tier == 'subscription' else
                (sub, amber, deepseek, legacy) if tier == 'paid' else
                (sub, legacy, amber, deepseek))
    assert review_order.payment_order((deepseek, legacy, sub, amber), role) == expected


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('tier', ('missing', None, 'unknown'))
def test_tierless_amber_has_a_defined_payment_class(muse_legs, role, tier):
    amber = SimpleNamespace(id=AMBER_ID, **({} if tier == 'missing' else {'tier': tier}))
    sub, deepseek = muse_legs[3], muse_legs[-1]
    assert review_order.payment_tier(amber, role) == 'paid'
    assert review_order.payment_order((deepseek, amber, sub), role) == (sub, amber, deepseek)


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_paid_precedence_uses_exact_amber_identity(muse_legs, role):
    amber, deepseek = muse_legs[-2:]
    renamed = replace(amber, id='renamed', tier='paid')
    exact = replace(amber, harness='agy', model=muse_legs[1].model, tier='free')
    assert review_order.payment_tier(exact, role) == 'paid'
    assert review_order.payment_tier(replace(amber, id='renamed'), role) == 'subscription'
    assert review_order.payment_order((deepseek, renamed), role) == (deepseek, renamed)
    assert review_order.payment_order((deepseek, exact), role) == (exact, deepseek)


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_subscription_walk_without_amber(muse_legs, role):
    deepseek, sub, mimo = muse_legs[-1], muse_legs[2], muse_legs[3]
    other = replace(deepseek, id='other-paid', model=muse_legs[0].model, family='gpt')
    assert review_order.payment_order((deepseek, sub, other, mimo), role) == (
        sub, mimo, deepseek, other)


@pytest.mark.parametrize('role', ('implement', 'review'))
def test_paid_amber_precedes_deepseek(muse_legs, role):
    sub, amber, deepseek = muse_legs[3], *muse_legs[-2:]
    other = replace(deepseek, id='other-paid', model=muse_legs[0].model, family='gpt')
    assert review_order.payment_order((deepseek, other, amber, sub), role) == (
        sub, amber, deepseek, other)


@pytest.mark.parametrize('role', (None, 'orchestrate', 'research'))
def test_other_roles_unchanged(muse_legs, role):
    legs = (muse_legs[-1], muse_legs[-2], muse_legs[3])
    assert review_order.payment_order(iter(legs), role) == legs


@pytest.mark.parametrize('role', ('implement', 'review'))
@pytest.mark.parametrize('tier', ('trial', 'free', 'subscription', 'paid'))
def test_payment_tier_uses_supplied_leg_data(muse_legs, role, tier):
    leg = replace(muse_legs[3], tier=tier)
    assert review_order.payment_tier(leg, role) == tier
