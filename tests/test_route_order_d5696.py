"""Opt-in order core; real-registry drops and explicitly synthetic positive paths."""
import json
from dataclasses import asdict, replace

import pytest

from plangraph.planner.legs import load_legs, spawnable_legs
from plangraph.planner.review_order import payment_order


@pytest.fixture
def catalog():
    return {leg.id: leg for leg in spawnable_legs()}


def ordered(legs, role, *, order_class='standard', **kwargs):
    return payment_order(reversed(tuple(legs)), role, order_class=order_class, **kwargs)


def signature(legs):
    return [(leg.id, leg.effort) for leg in legs]


@pytest.mark.parametrize('id,effort', [('claude-haiku-5.5', 'xhigh'),
    ('mimo-v2.6-pro', 'medium'), ('mimo-v2.6-flash', 'medium'),
    ('opencode-go-deepseek-v4-flash', 'medium'), ('omniroute-deepseek-flash', 'medium')])
def test_catalog_efforts_are_explicit(catalog, id, effort):
    """Requested efforts stay unregistered until exact benchmark coverage exists."""
    from plangraph.catalog_data import SEED_MODELS
    from plangraph.routing_support.catalog_seed import SEED_BENCHMARKS
    from test_benchmark_data_contract import _violations
    assert catalog[id].effort is None
    model = catalog[id].model
    model = model if '/' in model else catalog[id].harness + '/' + model
    assert ('missing_row', f'leg:{id}@{model}', effort) in _violations(
        SEED_BENCHMARKS, SEED_MODELS, [replace(catalog[id], effort=effort)])


def test_implement_order_efforts_and_limited_fallthrough(catalog):
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_ORDERS
    assert OPERATOR_ROUTE_ORDERS['implement'] == (
        'cx-gpt-6.1-sol-xhigh', 'agy-gemini-4-hydrogen-high', 'agy-claude-sonnet-5-5-high')
    legs = [catalog[id] for id in OPERATOR_ROUTE_ORDERS['implement']]
    assert signature(ordered(legs, 'implement')) == [
        ('cx-gpt-6.1-sol-xhigh', 'xhigh'), ('agy-claude-sonnet-5-5-high', 'high')]
    assert ordered(legs[1:], 'implement') == (legs[2],)  # Cannot resurrect absent Sol.
    assert not ordered([catalog[id] for id in (
        'mimo-v2.6-pro', 'opencode-go-deepseek-v4-flash', 'mimo-v2.6-flash')], 'implement')
    # Synthetic-only positive Hydrogen path; this does not admit its registration.
    admitted = [replace(leg, roles=('implement',)) for leg in legs]
    assert signature(ordered(admitted, 'implement')) == [(legs[0].id, 'xhigh'),
        (legs[1].id, 'high'), (legs[2].id, 'high')]


def test_standard_review_order_efforts_and_writer_fence(catalog):
    result = ordered(catalog.values(), 'review', writer_families=('gpt',), diff_tokens=99999)
    assert signature(result) == [('mimo-v2.6-pro', None),
        ('opencode-go-deepseek-v4-flash', None), ('mimo-v2.6-flash', None),
        ('claude-haiku-5.5', None), ('omniroute-deepseek-flash', None)]
    assert ordered(result, 'review', writer_families=('mimo', 'anthropic'),
                   diff_tokens=99999) == (result[1], result[-1])


@pytest.mark.parametrize('order_class', ['high', 'complex'])
def test_complex_review_sol_terra_hydrogen_and_efforts(catalog, order_class):
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_ORDERS
    effort, hydrogen = ('xhigh', 'high') if order_class == 'complex' else ('medium', 'medium')
    assert OPERATOR_ROUTE_ORDERS['review'][order_class][:3] == (
        f'cx-gpt-6.1-sol-{effort}', f'cx-gpt-6.1-terra-{effort}', f'agy-gemini-4-hydrogen-{hydrogen}')
    assert all('review' not in catalog[id].roles
               for id in OPERATOR_ROUTE_ORDERS['review'][order_class][:3])
    result = ordered(catalog.values(), 'review', order_class=order_class,
                     writer_families=('claude',), diff_tokens=1000)
    assert signature(result) == [('mimo-v2.6-pro', None),
        ('opencode-go-deepseek-v4-flash', None), ('mimo-v2.6-flash', None),
        ('omniroute-deepseek-flash', None)]


def test_planning_fable_high(catalog):
    fable = catalog['claude-fable-5.2-high']
    assert not fable.roles and fable.disabled and fable.expires is None
    assert fable.effort == 'high' and not ordered(catalog.values(), 'planning')
    # Synthetic-only positive Fable path; real planning admission is empty.
    assert ordered((replace(fable, roles=('orchestrate',)),), 'planning')[0].effort == 'high'


@pytest.mark.parametrize('order_class,effort,hydrogen', [('high', 'medium', 'medium'), ('complex', 'xhigh', 'high')])
def test_explicit_upstream_roles_keep_full_high_review_order(catalog, order_class, effort, hydrogen):
    """Synthetic-only upstream roles; real-registry prefix drops are tested above."""
    ids = (f'cx-gpt-6.1-sol-{effort}', f'cx-gpt-6.1-terra-{effort}', f'agy-gemini-4-hydrogen-{hydrogen}')
    admitted = [replace(catalog[id], roles=('review',)) for id in ids]
    result = ordered(admitted, 'review', order_class=order_class, writer_families=('claude',))
    assert signature(result) == [(ids[0], effort), (ids[1], effort), (ids[2], hydrogen)]
    assert result[0].family == result[1].family != result[2].family
    assert ordered(admitted, 'review', order_class=order_class, writer_families=('openai',)) == (admitted[2],)


def test_payment_order_unmodified_registry_names_every_drop(catalog):
    result = payment_order(spawnable_legs(), 'review', order_class='standard',
                           writer_families=('mimo',), diff_tokens=100000)
    reasons = {
        'role': '''openrouter-free cx-gpt-6.1-sol-xhigh deepseek cx-gpt-6.1-sol
            cx-gpt-6.1-sol-low cx-gpt-6.1-sol-medium cx-gpt-6.1-sol-high cx-gpt-6.1-sol-max
            agy-gemini-4-hydrogen-high agy-gemini-4-hydrogen-medium cx-gpt-6.1-terra-medium
            cx-gpt-6.1-terra-xhigh muse-ember-max claude-fable-5.2-high''',
        'not_in_order': '''qodercli-qwen3.8-flash opencode-deepseek-v4.1-flash-free
            opencode-glm-5.3-flash-free cx-gpt-6-luna agy-gemini-3.8-flash agy-gemini-4-argon
            opencode-big-pickle agy-gpt-oss-120b agy-sonnet-5.5 agy-claude-opus agy-opus-5.5
            omniroute-muse-spark agy-opus opencode-gemini-4-argon opencode-go-gemini-4-argon
            opencode-go-glm-5.2 opencode-go-qwen3.8-max omniroute-grok-4.7 opencode-go-muse-spark
            opencode-console-muse-spark omniroute-gemini-3.8-flash agy-claude-sonnet-5-5-low
            agy-claude-sonnet-5-5-medium agy-claude-sonnet-5-5-high agy-claude-opus-5-5-low
            agy-claude-opus-5-5-medium agy-claude-opus-5-5-high agy-gemini-3.8-flash-low
            agy-gemini-3.8-flash-medium agy-gemini-3.8-flash-high cx-gpt-6-luna-low
            cx-gpt-6-luna-medium cx-gpt-6-luna-high cx-gpt-6-luna-xhigh cx-gpt-6-luna-max''',
        'writer_family': 'mimo-v2.6-pro mimo-v2.6-flash', 'diff_size': 'claude-haiku-5.5'}
    dropped = {id: reason for reason, ids in reasons.items() for id in ids.split()}
    assert signature(result) == [('opencode-go-deepseek-v4-flash', None),
                                 ('omniroute-deepseek-flash', None)]
    assert catalog.keys() - {leg.id for leg in result} == dropped.keys()
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_ORDERS
    for id, reason in dropped.items():
        leg = catalog[id]
        assert {'role': 'review' not in leg.roles,
                'not_in_order': 'review' in leg.roles and id not in OPERATOR_ROUTE_ORDERS['review']['standard'],
                'writer_family': leg.family == 'mimo',
                'diff_size': id == 'claude-haiku-5.5'}[reason], (id, reason)


@pytest.mark.parametrize('diff_tokens,haiku', [(None, False), (99999, True), (100000, False), (100001, False)])
def test_haiku_requires_diff_strictly_under_100k(catalog, diff_tokens, haiku):
    result = ordered(catalog.values(), 'review', writer_families=('gpt',), diff_tokens=diff_tokens)
    assert ('claude-haiku-5.5' in {leg.id for leg in result}) is haiku


@pytest.mark.parametrize('verdicts,ember', [({}, False), ({'mimo-v2.6-pro': 'PASS'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'mimo-v2.6-flash': 'PASS'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'omniroute-deepseek-flash': 'FIX'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'opencode-go-deepseek-v4-flash': 'unknown'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'opencode-go-deepseek-v4-flash': 'FIX'}, True)])
def test_ember_only_after_two_cheaper_disagreeing_verdicts(catalog, verdicts, ember):
    assert not catalog['muse-ember-max'].roles and catalog['muse-ember-max'].disabled
    assert 'muse-ember-max' not in {leg.id for leg in ordered(catalog.values(), 'review',
        writer_families=('gpt',), diff_tokens=1000, verdicts=verdicts)}
    # Hypothetical upstream admission only; the registry declaration stays role-less.
    admitted = (*[leg for leg in catalog.values() if leg.id != 'muse-ember-max'],
                replace(catalog['muse-ember-max'], roles=('review',)))
    result = ordered(admitted, 'review', writer_families=('gpt',), diff_tokens=1000, verdicts=verdicts)
    assert ('muse-ember-max' in {leg.id for leg in result}) is ember
    expected = [('mimo-v2.6-pro', None), ('opencode-go-deepseek-v4-flash', None),
                ('mimo-v2.6-flash', None), ('claude-haiku-5.5', None)]
    assert signature(result) == expected + ([('muse-ember-max', 'max')] if ember else []) + [
        ('omniroute-deepseek-flash', None)]


def test_expensive_or_excluded_verdicts_cannot_unlock_ember(catalog):
    admitted = [replace(catalog['muse-ember-max'], roles=('review',)),
                catalog['mimo-v2.6-pro'], catalog['opencode-go-deepseek-v4-flash']]
    for votes in ({'cx-gpt-6.1-sol-xhigh': 'PASS', 'agy-gemini-4-hydrogen-high': 'FIX'},
                  {'mimo-v2.6-pro': 'PASS', 'opencode-go-deepseek-v4-flash': 'FIX'}):
        assert ordered(admitted, 'review', writer_families=('mimo',), verdicts=votes) == (admitted[2],)


def test_fixed_order_is_pure_and_rejects_unknown_class(catalog):
    before = tuple(vars(leg).copy() for leg in catalog.values())
    result = ordered(catalog.values(), 'implement')
    assert payment_order(result, 'implement', order_class='standard') == result
    assert tuple(vars(leg) for leg in catalog.values()) == before
    with pytest.raises(ValueError, match='order class'):
        ordered(catalog.values(), 'review', order_class='typo', writer_families=('gpt',))


@pytest.mark.parametrize('diff_tokens', [True, -1, 1.5, '1000'])
def test_invalid_diff_size_refused(catalog, diff_tokens):
    with pytest.raises(ValueError, match='diff_tokens'):
        ordered(catalog.values(), 'review', writer_families=('gpt',), diff_tokens=diff_tokens)


def test_unknown_role_and_duplicate_ids_fail_closed(catalog):
    with pytest.raises(ValueError, match='ordered role'):
        ordered(catalog.values(), 'unknown')
    with pytest.raises(ValueError, match='duplicate'):
        ordered([catalog['mimo-v2.6-pro']] * 2, 'implement')
    with pytest.raises(ValueError, match='writer family'):
        ordered(catalog.values(), 'review')


def test_order_never_bypasses_role_admission(catalog):
    sonnet = replace(catalog['agy-claude-sonnet-5-5-high'], harness='claude', roles=('review',))
    assert not ordered((sonnet,), 'implement')
    with pytest.raises(ValueError, match='D-5712: claude_code_subscription_forbidden_for_implement'):
        ordered((replace(sonnet, roles=('implement',)),), 'implement')


@pytest.mark.parametrize('effort', [None, 'low', 'max'])
def test_order_preserves_registered_effort_not_id_suffix(catalog, effort):
    leg = replace(catalog['cx-gpt-6.1-sol-xhigh'], effort=effort)
    assert ordered((leg,), 'implement')[0] is leg


@pytest.mark.parametrize('missing', [('windows',), ('max_request_tokens',), ('trains_on_data',),
                                  ('windows', 'max_request_tokens', 'trains_on_data')])
def test_disabled_declaration_cannot_bypass_capability_validation(tmp_path, catalog, missing):
    raw = asdict(replace(catalog['mimo-v2.6-pro'], disabled='evidence required', roles=()))
    for key in missing:
        raw.pop(key)
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': [raw]}))
    with pytest.raises(ValueError, match='missing field') as exc:
        load_legs(path)
    assert all(key in str(exc.value) for key in missing)


def test_disabled_declaration_refuses_explicit_unknown_capabilities(tmp_path, catalog):
    raw = asdict(replace(catalog['mimo-v2.6-pro'], disabled='evidence required', roles=(),
                         windows=(), max_request_tokens=None, trains_on_data=None))
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': [raw]}))
    with pytest.raises(ValueError, match='windows.*max_request_tokens.*trains_on_data'):
        load_legs(path)


def test_order_rows_share_the_existing_leg_registry(catalog):
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_ORDERS
    tables = OPERATOR_ROUTE_ORDERS
    ids = set((*tables['implement'], *tables['planning'], *tables['review']['high'], *tables['review']['complex']))
    assert ids <= catalog.keys()
    for id in ('agy-gemini-4-hydrogen-high', 'agy-gemini-4-hydrogen-medium',
               'cx-gpt-6.1-terra-medium', 'cx-gpt-6.1-terra-xhigh', 'muse-ember-max', 'claude-fable-5.2-high'):
        leg = catalog[id]
        assert leg.disabled and not leg.roles and (leg.windows, leg.max_request_tokens, leg.trains_on_data) == ((), None, None)


def test_catalog_uses_valid_existing_leg_schema(tmp_path):
    legs = tuple(leg for leg in spawnable_legs() if leg.windows)
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': [asdict(leg) for leg in legs]}))
    assert load_legs(path) == legs


def test_order_capabilities_are_existing_facts_or_unset(catalog):
    haiku = catalog['claude-haiku-5.5']
    assert (haiku.windows, haiku.max_request_tokens) == (('128k', '256k'), 180000)
    before = tuple(asdict(leg) for leg in catalog.values())
    ordered(catalog.values(), 'review', writer_families=('gpt',), diff_tokens=1000)
    assert tuple(asdict(leg) for leg in catalog.values()) == before


def test_d5712_sonnet_source_and_position_2_to_4(catalog):
    sonnet = catalog['agy-claude-sonnet-5-5-high']
    assert sonnet.model == 'claude-sonnet-5-5-high' and sonnet.effort == 'high'
    assert ordered((sonnet,), 'implement') == (sonnet,)
    assert not ordered((catalog['agy-sonnet-5.5'],), 'implement')
    assert ordered((replace(sonnet, harness='opencode'),), 'implement')[0].effort == 'high'


@pytest.mark.parametrize('model,replacement', [('gemini-3.8-flash', 'gemini-4-hydrogen'),
    ('omniroute/gemini-3.8-flash-high', 'gemini-4-hydrogen'), ('cxa/gpt-6-luna-high', 'gpt-6.1-terra')])
def test_fixed_order_refuses_legacy_with_replacement(catalog, model, replacement):
    with pytest.raises(ValueError, match=replacement):
        ordered((replace(catalog['cx-gpt-6.1-sol-xhigh'], model=model),), 'implement')


def test_live_legacy_door_keeps_d5696_open(catalog):
    # Only opt-in fixed ordering is fenced here; live routing belongs to LIVE-FENCE.
    with pytest.raises(ValueError, match='gemini-4-hydrogen'):
        ordered((replace(catalog['cx-gpt-6.1-sol-xhigh'], model='gemini-3.8-flash'),), 'implement')


def test_unknown_capability_declarations_are_not_live_admissions(catalog):
    unknown = [leg for leg in catalog.values() if not leg.windows]
    assert len(unknown) == 6
    assert all(leg.disabled and leg.expires is None and not leg.roles for leg in unknown)


@pytest.mark.parametrize('statement', [
    'Hydrogen high is table-only until its registration is admitted',
    'positive paths are synthetic-only'])
def test_order_handoff_distinguishes_tables_from_registry(statement):
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / 'docs/routing.md').read_text()
    assert statement in doc
