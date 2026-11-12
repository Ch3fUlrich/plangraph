"""Slice-1 routing must not change unrelated leg or catalog contracts."""
import asyncio
import time
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, MUSE_AMBER
from plangraph.planner.review_order import catalog_tiers
from plangraph.planner.routing import next_after_failure, plan_spawn, route_card
from plangraph.routing_support.resolver import DEFAULT_FALLBACK_MODELS
from plangraph.spawner.service import SpawnService


BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}


def test_gemini_remains_free_review_only_and_sol_keeps_base_model():
    gemini = BY_ID['agy-gemini-3.8-flash']
    assert (gemini.tier, gemini.roles) == ('free', ('review',))
    assert gemini.note == 'gemini after its Friday-evening reset'
    assert BY_ID['cx-gpt-6.1-sol'].model == 'omniroute/cx-gpt-6.1-sol'
    review = plan_spawn({'estimate_tokens': 1000}, role='review', writer_families=('gpt',))
    assert review.first_leg.id == gemini.id
    writers = plan_spawn({'estimate_tokens': 1000}, role='implement')
    assert [leg.id for leg in writers.legs] == ['cx-gpt-6.1-sol', MUSE_AMBER]


@pytest.mark.parametrize('role', [None, 'implement', 'review', 'orchestrate'])
@pytest.mark.parametrize('exit_class', ['check_fail', 'stall', 'timeout', 'rebase_conflict', 'worker_crash'])
@pytest.mark.parametrize('implicit_first', [False, True])
def test_non_muse_failure_promotion_does_not_rotate_subscriptions(role, exit_class, implicit_first):
    """Historical name: generic subscription rotation also applies without Muse."""
    ids = ('cx-gpt-6.1-sol', 'cx-gpt-6-luna', 'deepseek')
    legs = tuple(replace(BY_ID[mid], disabled=None) for mid in ids)
    route = route_card({'estimate_tokens': 1000}, legs=legs, role=role)
    assert MUSE_AMBER not in {leg.id for leg in route.legs}
    tried = [] if implicit_first else [ids[0]] * route.attempt_budget
    action, leg = next_after_failure(route, tried, exit_class, route.attempt_budget)
    assert (action, leg.id) == ('rotate', ids[1])
    action, leg = next_after_failure(route, ids[:2], exit_class, route.attempt_budget)
    assert (action, leg.id) == ('promote', 'deepseek')
    assert next_after_failure(route, ids, exit_class, route.attempt_budget) == ('park', None)


def test_non_muse_runtime_rows_keep_their_privacy_metadata():
    rows = {row['id']: row for row in DEFAULT_FALLBACK_MODELS}
    for leg in DEFAULT_LEGS:
        assert rows[leg.model].get('trains_on_prompts') == int(leg.trains_on_data), leg.id
    assert rows[BY_ID['openrouter-free'].model]['trains_on_prompts'] == 1
    assert rows[BY_ID[MUSE_AMBER].model]['trains_on_prompts'] == 1


@pytest.mark.parametrize('role', [None, 'implement', 'review', 'orchestrate'])
@pytest.mark.parametrize('tier', ['trial', 'free', 'subscription', 'paid'])
def test_qualified_catalog_tiers_apply_symmetrically_to_registered_legs(role, tier):
    rows = [{'id': leg.model if '/' in leg.model else f'{leg.harness}/{leg.model}',
             'tier': tier} for leg in DEFAULT_LEGS]
    projected = catalog_tiers(DEFAULT_LEGS, rows, role)
    protected = {MUSE_AMBER, 'cx-gpt-6.1-sol'} if role == 'implement' else {MUSE_AMBER}
    assert [leg.tier for leg in projected] == [
        leg.tier if leg.id in protected else tier for leg in DEFAULT_LEGS]
    assert [leg.roles for leg in projected] == [leg.roles for leg in DEFAULT_LEGS]


@pytest.mark.parametrize('pin', ['leg', 'model'])
@pytest.mark.parametrize('cooled', [False, True])
@pytest.mark.parametrize('reason', [None, 'Audited public writer pin'])
def test_paid_writer_pin_observes_host_subscription_cooldown(tmp_path, pin, cooled, reason):
    from plangraph.planner.routing import SpawnRefused
    sol = BY_ID['cx-gpt-6.1-sol']
    amber = BY_ID[MUSE_AMBER]
    service = SpawnService(state=tmp_path, leg_reader=lambda *a, **kw:
                           {'state': 'available', 'evidence': None})
    if cooled:
        service._save(dict(run_id='run-cooledwriter', status='failed', ended=time.time(),
            attempts=[dict(leg={'id': sol.id}, status='failed', down_reason='usage_limit')]))
        from plangraph.planner.measured_down import measured_down
        assert sol.id in measured_down(tmp_path)
    assert sol.id in {leg.id for leg in service.route({'estimate_tokens': 1000}).legs}
    options = dict(card={'estimate_tokens': 1000}, role='implement', dry_run=True,
                   override_reason=reason,
                   **{pin: amber.id if pin == 'leg' else amber.model})
    with pytest.raises(SpawnRefused, match='subscription writers available: ' + sol.id):
        asyncio.run(service.spawn(**options))
    assert not service.runner.jobs
    assert not (tmp_path / 'spawn-overrides.jsonl').exists()
