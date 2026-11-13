from dataclasses import replace
from pathlib import Path

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.route_policy import load_route_policy
from plangraph.planner.routing import plan_spawn, route_card


def test_qoder_off_for_review_even_with_old_active_leg():
    leg = next(l for l in DEFAULT_LEGS if l.harness == 'qodercli')
    policy = load_route_policy(Path('src/plangraph/planner/route_policy.json'))
    assert policy.skip_reason(replace(leg, disabled=None), 'review') == 'paused_harness'


def test_disabled_leg_never_a_route_option():
    disabled = next(l for l in DEFAULT_LEGS if l.harness == 'qodercli')
    later = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-3.8-flash')
    route = route_card({'estimate_tokens': 1000}, legs=(disabled, later), role='review')
    assert route.legs == (later,)
    assert 'excluded 1 legs: disabled' in route.reasons


def test_large_review_falls_through_small_window_with_reason():
    small = next(l for l in DEFAULT_LEGS if l.id == 'opencode-go-gemini-4-argon')
    large = next(l for l in DEFAULT_LEGS if l.id == 'agy-gemini-3.8-flash')
    route = route_card({'estimate_tokens': 200000, 'window_class': '128k'},
                       legs=(small, large), role='review')
    assert route.legs == (large,)
    assert route.window_class == '1m'
    assert 'excluded 1 legs: window_class' in route.reasons


def test_review_route_never_offers_qwen_max():
    route = route_card({'estimate_tokens': 1000}, role='review')
    assert 'opencode-go-qwen3.8-max' not in [leg.id for leg in route.legs]


def test_glm_never_a_review_option():
    policy = load_route_policy(Path('src/plangraph/planner/route_policy.json'))
    assert 'glm' in policy.review_allowed_families  # Historical name; D-5648 supersedes D-3605.
    # The low-level route_card orders eligible legs; plan_spawn applies policy.
    route = plan_spawn({'estimate_tokens': 1000}, role='review',
                       writer_families=('gpt',), policy=policy, catalog_rows=(),
                       leg_reader=lambda *a, **k: {'state': 'available', 'evidence': None})
    assert [leg.id for leg in route.legs if leg.family == 'glm'] == ['opencode-big-pickle']
    reasons = {s['leg']: s['reason'] for s in route.skipped}
    assert 'opencode-big-pickle' not in reasons
    assert reasons['opencode-go-glm-5.2'].startswith('disabled: D-3605')
    assert reasons['opencode-glm-5.3-flash-free'].startswith('disabled: D-4196')
    ordinary = replace(policy, review_allowed_families=('deepseek',))
    assert ordinary.skip_reason(next(leg for leg in DEFAULT_LEGS
                                    if leg.id == 'opencode-big-pickle'), 'review') == 'off_family'


def test_big_pickle_review_lift_does_not_bypass_other_policy_guards():
    from plangraph.planner.route_policy import RoutePolicy
    pickle = next(leg for leg in DEFAULT_LEGS if leg.id == 'opencode-big-pickle')
    policy = load_route_policy(Path('src/plangraph/planner/route_policy.json'))
    assert policy.skip_reason(pickle, 'review') is None
    for role in ('implement', 'orchestrate'):
        assert policy.skip_reason(pickle, role) == 'off_family'
    assert RoutePolicy(banned_families=('glm',)).skip_reason(pickle, 'review') == 'banned_family'
    assert RoutePolicy(off_families=('glm',)).skip_reason(pickle, 'review') == 'off_family'
    assert replace(policy, paused_harnesses={'opencode': ()}).skip_reason(
        pickle, 'review') == 'paused_harness'
    assert 'D-5648' in Path('src/plangraph/planner/route_policy.json').read_text()
