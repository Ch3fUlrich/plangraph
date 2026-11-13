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
    small = next(l for l in DEFAULT_LEGS if l.id == 'opencode-go-deepseek-v4-flash')
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
    assert 'glm' not in policy.review_allowed_families
    # The low-level route_card orders eligible legs; plan_spawn applies policy.
    route = plan_spawn({'estimate_tokens': 1000}, role='review',
                       writer_families=('gpt',), policy=policy, catalog_rows=())
    assert 'glm' not in {leg.family for leg in route.legs}
    assert {s['leg']: s['reason'] for s in route.skipped}['opencode-big-pickle'] == 'off_family'
