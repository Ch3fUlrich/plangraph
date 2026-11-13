"""D-5648 review registry admission, cost order and privacy (fixed inputs)."""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import plan_spawn, route_card

TABLE = {leg.id: leg for leg in DEFAULT_LEGS}
MODELS = {
    'opencode-deepseek-v4.1-flash-free': ('opencode/deepseek-v4.1-flash-free', 'deepseek', 'free', False),
    'opencode-mimo-v2.6-flash-free': ('opencode/mimo-v2.6-flash-free', 'mimo', 'free', True),
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
        'opencode-deepseek-v4.1-flash-free', 'opencode-mimo-v2.6-flash-free', 'opencode-big-pickle'))
    benchmark = {legs[0].id: {'aa_intelligence': 99, 'cost_per_task_usd': 0,
                            'pool_burn_weight': 0}} if measured else None
    result = plan(legs=legs, benchmark=benchmark)
    assert result.error is None
    assert {leg.id for leg in result.legs} == {leg.id for leg in legs}
    tiers = [TIERS.index(leg.tier) for leg in result.legs]
    assert tiers == sorted(tiers)
    assert {leg.tier for leg in result.legs} == {'free', 'subscription', 'paid'}
    assert len(result.review_seats) == 2
    assert all(leg.tier == 'free' for leg in result.review_seats)


def test_no_argon_review_route_and_no_registration_removed():
    # route_card is pure (no policy/store parameters); pin its complete table and clock.
    for result in (plan(), route_card(CARD, legs=DEFAULT_LEGS, role='review', now=100)):
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
    # Historical test name retained; D-2739 admits only the requested free path.
    from plangraph.mimo_policy import MimoPathError, check_mimo_path
    assert check_mimo_path('opencode/mimo-v2.6-flash-free') == TABLE['opencode-mimo-v2.6-flash-free'].model
    with pytest.raises(MimoPathError, match='Mimo requires one of:'):
        check_mimo_path('opencode/mimo-v2.6-pro-free')


def test_free_review_admission_does_not_enable_new_writers(tmp_path):
    from plangraph.planner.legs import load_legs
    for leg_id in ('opencode-deepseek-v4.1-flash-free', 'opencode-mimo-v2.6-flash-free'):
        leg = TABLE[leg_id]
        assert leg.roles == ('review',)
        path = tmp_path / 'legs.json'
        path.write_text(json.dumps({'legs': [asdict(leg)]}))
        assert load_legs(path) == (leg,)
        result = plan_spawn(CARD, role='implement', legs=(leg,), catalog_rows=(),
                            policy=RoutePolicy(), leg_reader=lambda *a, **k: pytest.fail('non-writer probed'))
        assert not result.legs and result.error


def test_omniroute_deepseek_uses_served_paid_id():
    leg = TABLE['omniroute-deepseek-flash']
    assert (leg.model, leg.tier) == ('omniroute/deepseek-direct-flash', 'paid')
    assert leg.roles == ('implement', 'review', 'orchestrate')


def test_free_mimo_does_not_inherit_direct_gateway_render_or_probe_bypass():
    from plangraph.mimo_policy import DIRECT_MIMO_IDS
    from plangraph.routing_support.opencode_render import _mimo_template, render_gateway_providers
    from plangraph.routing_support.resolver import admitted_by_render
    from plangraph.spawner.opencode_answer import cap_reasoning
    free = TABLE['opencode-mimo-v2.6-flash-free'].model
    assert not admitted_by_render(free)
    assert all(admitted_by_render(model) for model in DIRECT_MIMO_IDS)
    assert set(_mimo_template()['models']) == {'mimo-v2.6-flash', 'mimo-v2.6-pro'}
    child = {'OPENCODE_CONFIG_CONTENT': '{"providers":{}}'}
    assert render_gateway_providers(child, free) is None
    assert json.loads(child['OPENCODE_CONFIG_CONTENT']) == {'providers': {}}
    native = {'OPENCODE_CONFIG_CONTENT': json.dumps({'providers': {'opencode': {
        'package': '@opencode/ai/providers/openai-compatible',
        'models': {'mimo-v2.6-flash-free': {}}}}})}
    before = native.copy()
    assert not cap_reasoning(native, free) and native == before


def test_identity_only_benchmark_rows_never_enter_numeric_catalog():
    from plangraph.routing_support.catalog_db import CatalogDB
    from plangraph.routing_support.catalog_seed import REVIEW_BENCHMARKS
    with CatalogDB() as db:
        db.init_and_seed()
        rows = db.conn.execute('SELECT model_id, aa_intelligence FROM catalog_benchmarks').fetchall()
    assert len(rows) == 25 and all(row[1] is not None for row in rows)
    assert {row[0] for row in rows}.isdisjoint(row['model_id'] for row in REVIEW_BENCHMARKS)


def test_identity_only_rows_do_not_claim_strict_runtime_benchmark_coverage():
    from plangraph.routing_support.catalog_db import CatalogDB
    from plangraph.planner.routing import BenchRequired
    with CatalogDB() as db:
        db.init_and_seed()
        # Identity-only data cannot satisfy the runtime numeric benchmark gate.
        data = {row['model_id']: dict(row) for row in db.conn.execute('SELECT * FROM catalog_benchmarks')}
    legs = tuple(TABLE[id] for id in ('opencode-deepseek-v4.1-flash-free',
                                      'opencode-mimo-v2.6-flash-free', 'console-openrouter-free'))
    with pytest.raises(BenchRequired) as exc:
        plan(legs=legs, strict=True, benchmark=data)
    assert {gap['leg'] for gap in exc.value.gaps} == {leg.id for leg in legs}


def test_direct_deepseek_routes_cannot_fake_distinct_review_families():
    from plangraph.planner.seats import seat_family
    legs = tuple(TABLE[id] for id in ('opencode-deepseek-v4.1-flash-free',
                                      'console-deepseek-flash', 'omniroute-deepseek-flash'))
    assert {seat_family(leg.family) for leg in legs} == {'deepseek'}
    result = plan(legs=legs)
    assert result.review_seats == (legs[0],)
    assert result.error == 'two distinct review families unavailable'


@pytest.mark.parametrize('leg_id', list(MODELS))
def test_failed_probe_skips_each_review_route_with_evidence(tmp_path, leg_id):
    from plangraph.routing_support.leg_state import LegStateStore
    store = LegStateStore(tmp_path / 'legs.db')
    leg = TABLE[leg_id]
    store.record_probe_fail(leg.model, 'public verdict probe failed', 'D-2739', now=100)
    store.record_probe_fail(leg.model, 'public verdict probe failed again', 'D-2739', now=101)
    result = plan_spawn(CARD, role='review', writer_families=('gpt',), legs=(leg,),
                        catalog_rows=(), policy=RoutePolicy(), leg_reader=store, now=102)
    assert not result.legs and not result.review_seats and result.error
    skip, = result.skipped
    assert (skip['leg'], skip['reason']) == (leg.id, 'down')
    assert skip['evidence']['caller'] == 'D-2739' and skip['evidence']['status'] == 503


@pytest.mark.parametrize('leg_id', list(MODELS))
def test_prerun_host_probe_skips_unserved_review_registration(monkeypatch, leg_id):
    from plangraph.routing_support import opencode_overlay, resolver
    monkeypatch.setattr(opencode_overlay, '_load_root_providers', lambda: ({}, None))
    leg = TABLE[leg_id]
    result = resolver.plan_for_launch(plan(legs=(leg,)), probe=lambda config: frozenset())
    assert not result.legs
    assert result.skipped[-1]['skip_reason'] == 'not_served_on_host: ' + leg.model


def test_witnessed_free_verdicts_name_exact_routes_and_completed_assistant_text():
    root = Path(__file__).resolve().parents[1]
    proof = json.loads((root / 'tests/fixtures/review_free_verdicts.json').read_text())
    runs = proof['runs']
    assert {run['model'] for run in runs} == {
        'opencode/deepseek-v4.1-flash-free', 'opencode/mimo-v2.6-flash-free',
        'console-openrouter/openrouter/free'}
    assert len({''.join(run['session_id_parts']) for run in runs}) == 3
    docs = (root / 'docs/routing.md').read_text()
    for run in runs:
        assert run['model'] in docs and ' '.join(run['session_id_parts']) in docs
        assert len(run['session_id_parts']) == 2 and ''.join(run['session_id_parts']).startswith('ses_')
        assert run['events'][0]['type'] == 'text'
        assert run['events'][0]['part']['text'] == 'VERDICT: PASS'
        assert run['events'][-1]['type'] == 'step_finish'
        assert run['events'][-1]['part']['reason'] == 'stop'
