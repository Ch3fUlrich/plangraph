"""D-5648 review registry admission, cost order and privacy (fixed inputs)."""
import json

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import plan_spawn, route_card

TABLE = {leg.id: leg for leg in DEFAULT_LEGS}
MODELS = {
    'opencode-deepseek-v4.1-flash-free': ('opencode/deepseek-v4.1-flash-free', 'deepseek', 'free', False),
    'opencode-go-longcat-2.5-free': ('opencode-go/longcat-2.5-free', 'longcat', 'free', False),
    'console-openrouter-free': ('console-openrouter/openrouter/free', 'openrouter', 'free', True),
    'console-deepseek-flash': ('console-deepseek/deepseek-flash', 'deepseek', 'paid', False),
    'opencode-big-pickle': ('opencode/big-pickle', 'glm', 'free', True),
}
CARD = {'estimate_tokens': 1000}


def plan(card=CARD, **kwargs):
    return plan_spawn(card, role='review', writer_families=('gpt',),
                      catalog_rows=(), policy=RoutePolicy(),
                      leg_reader=lambda *a, **k: {'state': 'ok', 'evidence': None}, **kwargs)


@pytest.mark.parametrize('leg_id,expected', MODELS.items())
def test_review_leg_metadata(leg_id, expected):
    leg = TABLE[leg_id]
    assert (leg.model, leg.family, leg.tier, leg.trains_on_data) == expected
    assert 'review' in leg.roles and leg.disabled is None
    assert (leg.harness, leg.windows, leg.max_request_tokens) == ('opencode', ('128k',), 100000)
    if leg_id == 'opencode-deepseek-v4.1-flash-free':
        assert 'D-4196' in leg.note and 'D-5648' in leg.note and 'superseded' in leg.note


@pytest.mark.parametrize('measured', [False, True])
def test_gpt_review_free_before_subscription_before_paid(measured):
    legs = tuple(TABLE[id] for id in (
        'console-deepseek-flash', 'agy-sonnet-5.5', 'agy-gemini-3.8-flash',
        'opencode-go-longcat-2.5-free', 'console-openrouter-free',
        'opencode-deepseek-v4.1-flash-free', 'opencode-big-pickle'))
    benchmark = {legs[0].id: {'aa_intelligence': 99, 'cost_per_task_usd': 0,
                            'pool_burn_weight': 0}} if measured else None
    result = plan(legs=legs, benchmark=benchmark)
    assert result.error is None
    assert {leg.id for leg in result.legs} == {leg.id for leg in legs}
    tiers = [TIERS.index(leg.tier) for leg in result.legs]
    assert tiers == sorted(tiers) and set(tiers) == {1, 2, 3}
    assert all(leg.tier == 'free' for leg in result.review_seats)


def test_no_argon_review_route_and_no_registration_removed():
    for result in (plan(), route_card(CARD, role='review')):
        assert result.legs
        assert not any('gemini-4-argon' in leg.model for leg in result.legs)
    assert TABLE['agy-gemini-4-argon'].roles == ('implement',)
    assert TABLE['opencode-gemini-4-argon'].roles == ('orchestrate',)
    assert TABLE['opencode-go-gemini-4-argon'].roles == ()
    assert TABLE['opencode-go-gemini-4-argon'].disabled


def test_unchanged_disabled_leg_skipped_with_original_reason():
    leg = TABLE['opencode-glm-5.3-flash-free']
    reason = 'D-4196 opencode zen never worked, use opencode go; never remove (D-3828)'
    assert leg.disabled == reason
    result = plan()
    assert leg not in result.legs
    assert next(row for row in result.skipped if row['leg'] == leg.id) == {
        'leg': leg.id, 'reason': 'disabled: ' + reason,
        'evidence': {'kind': 'leg_disabled', 'reason': reason}}


@pytest.mark.parametrize('leg_id', [id for id, metadata in MODELS.items() if metadata[-1]])
def test_training_review_leg_excluded_for_secrets(leg_id):
    legs = (TABLE[leg_id], TABLE['agy-gemini-3.8-flash'])
    assert TABLE[leg_id] in plan(legs=legs).legs
    result = plan(dict(CARD, risk_tags=['secrets']), legs=legs)
    assert TABLE[leg_id] not in (*result.legs, *result.review_seats)
    assert {row['leg']: row['reason'] for row in result.skipped}[leg_id] == 'trains_on_data'


def test_registry_catalog_render_keeps_new_review_metadata(monkeypatch, capsys):
    from plangraph.routing_support import resolver
    from plangraph.spawner.cli import main
    rows = [dict(id=id, model=model, roles=list(TABLE[id].roles))
            for id, (model, family, tier, trains) in MODELS.items()]
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: rows)
    assert main(['catalog']) == 0
    rows = {row['model']: row for row in json.loads(capsys.readouterr().out)}
    for leg_id, (model, family, tier, trains) in MODELS.items():
        assert rows[model] == dict(id=leg_id, model=model, roles=list(TABLE[leg_id].roles))
        fallback = next(row for row in resolver.DEFAULT_FALLBACK_MODELS if row['id'] == model)
        assert (fallback['family'], fallback['tier']) == (family, tier)


def test_free_mimo_path_remains_fail_closed_until_policy_lane():
    from plangraph.mimo_policy import MimoPathError, check_mimo_path
    with pytest.raises(MimoPathError, match='Mimo requires one of:'):
        check_mimo_path('opencode/mimo-v2.6-flash-free')
