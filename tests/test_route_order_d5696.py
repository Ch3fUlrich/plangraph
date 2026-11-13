"""Hermetic D-5696/D-5706 ordering core; live route wiring is a later slice."""
from dataclasses import replace

import pytest

from plangraph.planner.legs import Leg
from plangraph.planner.review_order import payment_order


@pytest.fixture
def catalog():
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_CATALOG
    return tuple(Leg(**row) for row in OPERATOR_ROUTE_CATALOG)


def ordered(catalog, role, *, order_class='standard', **kwargs):
    return payment_order(reversed(catalog), role, order_class=order_class, **kwargs)


def signature(legs):
    return [(leg.id, leg.effort) for leg in legs]


def test_implement_order_efforts_and_limited_fallthrough(catalog):
    expected = [('cx-gpt-6.1-sol-xhigh', 'xhigh'),
                ('agy-gemini-4-hydrogen-high', 'high'),
                ('agy-claude-sonnet-5-5-high', 'high')]
    assert signature(ordered(catalog, 'implement')) == expected
    # Admission remains upstream: the ordering core cannot resurrect a limited leg.
    assert signature(ordered(catalog[1:], 'implement')) == expected[1:]
    from plangraph.planner.routing import plan_spawn
    from plangraph.planner.route_policy import RoutePolicy
    # Use the real admission boundary rather than pretending the ordering core
    # reads provider state. Live default-table activation remains out of slice.
    admitted = plan_spawn({'estimate_tokens': 1000}, role='implement',
        legs=catalog[:3], catalog_rows=tuple({'id': leg.model, 'active': True, 'available': 1}
                                           for leg in catalog[:3]), policy=RoutePolicy(),
        leg_reader=lambda model, **kw: {'state': 'limited' if model == catalog[0].model else 'ok',
                                       'evidence': None})
    assert signature(ordered(admitted.legs, 'implement')) == expected[1:2]
    assert any(row['leg'] == expected[0][0] and row['reason'] == 'limited'
               for row in admitted.skipped)


def test_standard_review_order_efforts_and_writer_fence(catalog):
    expected = [('mimo-v2.6-pro', 'medium'),
                ('opencode-go-deepseek-v4-flash', 'medium'),
                ('mimo-v2.6-flash', 'medium'),
                ('claude-haiku-5.5', 'xhigh'),
                ('omniroute-deepseek-flash', 'medium')]
    result = ordered(catalog, 'review', writer_families=('openai',), diff_tokens=99999)
    assert signature(result) == expected
    assert signature(ordered(catalog, 'review', writer_families=('mimo', 'anthropic'),
                             diff_tokens=99999)) == [expected[1], expected[-1]]


@pytest.mark.parametrize('order_class', ['high', 'complex'])
def test_complex_review_sol_terra_hydrogen_and_efforts(catalog, order_class):
    result = ordered(catalog, 'review', order_class=order_class,
                     writer_families=('claude',), diff_tokens=1000)
    sol_effort, hydrogen_effort = ('xhigh', 'high') if order_class == 'complex' else ('medium', 'medium')
    assert signature(result[:3]) == [(f'cx-gpt-6.1-sol-{sol_effort}', sol_effort),
        (f'cx-gpt-6.1-terra-{sol_effort}', sol_effort),
        (f'agy-gemini-4-hydrogen-{hydrogen_effort}', hydrogen_effort)]
    # Terra is Sol's same-family fallback, not the second independent seat.
    assert [leg.family for leg in (result[0], result[2])] == ['gpt', 'gemini']
    assert signature(result[3:]) == [('mimo-v2.6-pro', 'medium'),
        ('opencode-go-deepseek-v4-flash', 'medium'), ('mimo-v2.6-flash', 'medium'),
        ('omniroute-deepseek-flash', 'medium')]
    fenced = ordered(catalog, 'review', order_class=order_class,
                     writer_families=('gpt',), diff_tokens=1000)
    assert fenced[0].id == f'agy-gemini-4-hydrogen-{hydrogen_effort}'
    assert all(leg.family != 'gpt' for leg in fenced)


def test_planning_fable_high(catalog):
    assert signature(ordered(catalog, 'planning')) == [('claude-fable-5.2-high', 'high')]


def test_catalog_uses_valid_existing_leg_schema(tmp_path):
    import json
    from plangraph.planner.legs import load_legs
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_CATALOG
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': OPERATOR_ROUTE_CATALOG}))
    assert len(load_legs(path)) == len(OPERATOR_ROUTE_CATALOG)


@pytest.mark.parametrize('diff_tokens,haiku', [(None, False), (99999, True),
                                            (100000, False), (100001, False)])
def test_haiku_requires_diff_strictly_under_100k(catalog, diff_tokens, haiku):
    result = ordered(catalog, 'review', writer_families=('gpt',), diff_tokens=diff_tokens)
    assert ('claude-haiku-5.5' in {leg.id for leg in result}) is haiku


@pytest.mark.parametrize('verdicts,ember', [({}, False),
    ({'mimo-v2.6-pro': 'PASS'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'mimo-v2.6-flash': 'PASS'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'omniroute-deepseek-flash': 'FIX'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'opencode-go-deepseek-v4-flash': 'unknown'}, False),
    ({'mimo-v2.6-pro': 'PASS', 'opencode-go-deepseek-v4-flash': 'FIX'}, True)])
def test_ember_only_after_two_cheaper_disagreeing_verdicts(catalog, verdicts, ember):
    result = ordered(catalog, 'review', writer_families=('gpt',),
                     diff_tokens=1000, verdicts=verdicts)
    assert ('muse-ember-max' in {leg.id for leg in result}) is ember


def test_expensive_or_excluded_verdicts_cannot_unlock_ember(catalog):
    for votes in ({'cx-gpt-6.1-sol-xhigh': 'PASS', 'agy-gemini-4-hydrogen-high': 'FIX'},
                  {'mimo-v2.6-pro': 'PASS', 'opencode-go-deepseek-v4-flash': 'FIX'}):
        result = ordered(catalog, 'review', order_class='complex',
                         writer_families=('deepseek',), diff_tokens=1000, verdicts=votes)
        assert 'muse-ember-max' not in {leg.id for leg in result}


@pytest.mark.parametrize('model,replacement', [('gemini-3.8-flash', 'gemini-4-hydrogen'),
    ('omniroute/gemini-3.8-flash-high', 'gemini-4-hydrogen'),
    ('cxa/gpt-6-luna-high', 'gpt-6.1-terra')])
def test_fixed_order_refuses_legacy_with_replacement(catalog, model, replacement):
    with pytest.raises(ValueError, match=replacement):
        ordered((*catalog, replace(catalog[0], id='legacy', model=model)), 'implement')


def test_fixed_order_is_pure_and_rejects_unknown_class(catalog):
    before = tuple(vars(leg).copy() for leg in catalog)
    result = ordered(catalog, 'implement')
    assert payment_order(result, 'implement', order_class='standard') == result
    assert tuple(vars(leg) for leg in catalog) == before
    with pytest.raises(ValueError, match='order class'):
        ordered(catalog, 'review', order_class='typo', writer_families=('gpt',))


@pytest.mark.parametrize('diff_tokens', [True, -1, 1.5, '1000'])
def test_invalid_diff_size_refused(catalog, diff_tokens):
    with pytest.raises(ValueError, match='diff_tokens'):
        ordered(catalog, 'review', writer_families=('gpt',), diff_tokens=diff_tokens)


def test_unknown_role_and_duplicate_ids_fail_closed(catalog):
    with pytest.raises(ValueError, match='ordered role'):
        ordered(catalog, 'unknown')
    with pytest.raises(ValueError, match='duplicate'):
        ordered((*catalog, catalog[0]), 'implement')
