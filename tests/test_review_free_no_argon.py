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


@pytest.mark.parametrize('role', ['implement', 'review', 'orchestrate'])
def test_route_card_checks_explicit_role_before_other_eligibility(role):
    from dataclasses import replace
    from plangraph.planner.routing import RouteError
    original = TABLE['opencode-deepseek-v4.1-flash-free']
    candidate = replace(original, roles=(role,))
    assert route_card(CARD, legs=(candidate,), role=role).legs == (candidate,)
    for other_role in {'implement', 'review', 'orchestrate'} - {role}:
        with pytest.raises(RouteError) as exc:
            route_card(CARD, legs=(candidate,), role=other_role)
        assert exc.value.exclusion_reasons == {'role': 1}


@pytest.mark.parametrize('wrapped', [False, True], ids=['callable', 'leg-status-object'])
def test_plan_spawn_reader_contract_forwards_card_scope(wrapped):
    from types import SimpleNamespace
    leg = TABLE['opencode-deepseek-v4.1-flash-free']
    calls = []

    def reader(model, **scope):
        calls.append((model, scope))
        return {'state': 'ok', 'evidence': None}

    result = plan_spawn(dict(CARD, account='public-account', combo='review-combo'),
                        role='review', writer_families=('gpt',), legs=(leg,), catalog_rows=(),
                        policy=RoutePolicy(), now=100,
                        leg_reader=SimpleNamespace(leg_status=reader) if wrapped else reader)
    assert result.legs == (leg,)
    assert calls == [(leg.model, dict(now=100, account='public-account', combo='review-combo'))]


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
    from plangraph.routing_support.catalog_seed import review_benchmarks
    from plangraph.routing_support.catalog_seed import SEED_BENCHMARKS
    with CatalogDB(':memory:') as db:
        assert db.is_memory and db.conn.execute('PRAGMA database_list').fetchone()[2] == ''
        db.init_and_seed()
        rows = db.conn.execute('SELECT model_id, aa_intelligence FROM catalog_benchmarks').fetchall()
    identities = {row['model_id'] for row in review_benchmarks()}
    assert {row[0] for row in rows} == {row['model_id'] for row in SEED_BENCHMARKS}
    assert rows and all(row[1] is not None for row in rows)
    assert {row[0] for row in rows}.isdisjoint(identities)


def test_numeric_seed_with_null_score_is_not_silently_discarded(monkeypatch):
    import sqlite3
    from plangraph.routing_support import catalog_db
    original = catalog_db.SEED_BENCHMARKS[0]
    monkeypatch.setattr(catalog_db, 'SEED_BENCHMARKS', [dict(original, aa_intelligence=None)])
    with catalog_db.CatalogDB(':memory:') as db:
        with pytest.raises(sqlite3.IntegrityError, match='aa_intelligence'):
            db.init_and_seed()


def test_review_identity_resource_load_is_lazy_and_packaged(monkeypatch):
    import runpy
    import tomllib
    from fnmatch import fnmatch
    from plangraph.routing_support import catalog_seed
    from importlib.resources import files
    original = files('plangraph').joinpath('config/review_benchmarks.json')
    calls = []

    def resource(package):
        calls.append(package)
        return files(package)

    monkeypatch.setattr('importlib.resources.files', resource)
    namespace = runpy.run_path(catalog_seed.__file__)
    assert calls == []
    assert namespace['review_benchmarks']() == json.loads(original.read_text(encoding='utf-8'))
    assert calls == ['plangraph']
    config = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
    patterns = config['tool']['setuptools']['package-data']['plangraph']
    assert any(fnmatch('config/review_benchmarks.json', pattern) for pattern in patterns)


def test_identity_only_rows_do_not_claim_strict_runtime_benchmark_coverage():
    from plangraph.routing_support.catalog_db import CatalogDB
    from plangraph.planner.routing import BenchRequired
    with CatalogDB(':memory:') as db:
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
    """Historical name: docs consistency only, NOT independent live-run proof."""
    root = Path(__file__).resolve().parents[1]
    proof = json.loads((root / 'tests/fixtures/review_free_verdicts.json').read_text())
    assert proof['scope'] == 'docs-consistency only: native availability/verdict-format smokes, not witnessed spawner reviews'
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
