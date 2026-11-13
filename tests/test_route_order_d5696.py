"""Hermetic D-5696/D-5706 ordering core; live route wiring is a later slice."""
from dataclasses import replace

import pytest

from plangraph.planner.review_order import payment_order


@pytest.fixture
def catalog():
    from plangraph.planner.legs import spawnable_legs
    from plangraph.routing_support.catalog_seed import OPERATOR_ROUTE_ORDERS
    tables = OPERATOR_ROUTE_ORDERS
    ids = dict.fromkeys((*tables['implement'], *tables['review']['high'],
                         *tables['review']['complex'], *tables['planning']))
    registered = {leg.id: leg for leg in spawnable_legs()}
    # Hermetic admission projects table roles; live policy is deliberately not
    # activated by these registrations (see test_live_legacy_door_keeps_d5696_open).
    return tuple(replace(registered[id], roles=tuple(dict.fromkeys(role for role, table in (
        ('implement', tables['implement']), ('review', tables['review']['high']),
        ('review', tables['review']['complex']), ('orchestrate', tables['planning']))
        if id in table))) for id in ids)


def ordered(catalog, role, *, order_class='standard', **kwargs):
    return payment_order(reversed(catalog), role, order_class=order_class, **kwargs)


def signature(legs):
    return [(leg.id, leg.effort) for leg in legs]


def test_implement_order_efforts_and_limited_fallthrough(catalog):
    expected = [('cx-gpt-6.1-sol-xhigh', 'xhigh'),
                ('agy-gemini-4-hydrogen-high', 'high'),
                ('agy-claude-sonnet-5-5-high', 'high'),
                ('mimo-v2.6-pro', None), ('opencode-go-deepseek-v4-flash', None),
                ('mimo-v2.6-flash', None)]
    assert signature(ordered(catalog, 'implement')) == expected
    review_only = tuple(replace(leg, roles=('implement', 'review')) for leg in catalog
                        if leg.id in ('muse-ember-max', 'omniroute-deepseek-flash'))
    assert not ordered(review_only, 'implement')
    # Admission remains upstream: the ordering core cannot resurrect a limited leg.
    assert signature(ordered(catalog[1:], 'implement')) == expected[1:]
    from plangraph.planner.routing import plan_spawn
    from plangraph.planner.route_policy import RoutePolicy
    # Use the real admission boundary rather than pretending the ordering core
    # reads provider state. Live default-table activation remains out of slice.
    # Unknown Hydrogen capabilities are test evidence only, never live defaults.
    inputs = (catalog[0], replace(catalog[1], disabled=None, windows=('128k',),
                                max_request_tokens=1000, trains_on_data=False), catalog[2])
    admitted = plan_spawn({'estimate_tokens': 1000}, role='implement',
        legs=inputs, catalog_rows=tuple({'id': leg.model, 'active': True, 'available': 1}
                                      for leg in inputs), policy=RoutePolicy(),
        leg_reader=lambda model, **kw: {'state': 'limited' if model == catalog[0].model else 'ok',
                                       'evidence': None})
    assert signature(ordered(admitted.legs, 'implement')) == expected[1:2]
    assert any(row['leg'] == expected[0][0] and row['reason'] == 'limited'
               for row in admitted.skipped)
    # Not a one-leg trim: live D-3828 still fences AgY Sonnet (D-5696 stays open).
    assert any(row['leg'] == expected[2][0] and row['reason'] == 'excluded_family'
               for row in admitted.skipped)


def test_standard_review_order_efforts_and_writer_fence(catalog):
    expected = [('mimo-v2.6-pro', None),
                ('opencode-go-deepseek-v4-flash', None),
                ('mimo-v2.6-flash', None),
                ('claude-haiku-5.5', None),
                ('omniroute-deepseek-flash', None)]
    result = ordered(catalog, 'review', writer_families=('gpt',), diff_tokens=99999)
    assert signature(result) == expected
    assert signature(ordered(catalog, 'review', writer_families=('mimo', 'anthropic'),
                             diff_tokens=99999)) == [expected[1], expected[-1]]
    fenced = ordered(catalog, 'review', order_class='high', writer_families=('openai',))
    assert fenced[0].id == 'agy-gemini-4-hydrogen-medium' and all(l.family != 'gpt' for l in fenced)


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
    assert signature(result[3:]) == [('mimo-v2.6-pro', None),
        ('opencode-go-deepseek-v4-flash', None), ('mimo-v2.6-flash', None),
        ('omniroute-deepseek-flash', None)]
    fenced = ordered(catalog, 'review', order_class=order_class,
                     writer_families=('gpt',), diff_tokens=1000)
    assert fenced[0].id == f'agy-gemini-4-hydrogen-{hydrogen_effort}'
    assert all(leg.family != 'gpt' for leg in fenced)


def test_planning_fable_high(catalog):
    assert signature(ordered(catalog, 'planning')) == [('claude-fable-5.2-high', 'high')]


def test_catalog_uses_valid_existing_leg_schema(tmp_path, catalog):
    import json
    from plangraph.planner.legs import load_legs
    path = tmp_path / 'legs.json'
    from dataclasses import asdict
    from plangraph.planner.legs import spawnable_legs
    registrations = tuple(leg for leg in spawnable_legs() if leg.id in {l.id for l in catalog})
    def row(leg):
        return {key: value for key, value in asdict(leg).items()
                if leg.windows or key not in ('windows', 'max_request_tokens', 'trains_on_data')}
    path.write_text(json.dumps({'legs': [row(leg) for leg in registrations]}))
    assert load_legs(path) == registrations
    unknown = next(row(leg) for leg in registrations if not leg.windows)
    for change in ({'disabled': None}, {'expires': 10}, {'roles': ['implement']},
                   {'windows': []}, {'max_request_tokens': None}, {'trains_on_data': None},
                   {'trains_on_data': 'false'}):
        path.write_text(json.dumps({'legs': [dict(unknown, **change)]}))
        with pytest.raises(ValueError):
            load_legs(path)


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


def test_order_rows_share_the_existing_leg_registry(catalog):
    from plangraph.planner.legs import spawnable_legs
    registered = {leg.id: leg for leg in spawnable_legs()}
    assert {leg.id for leg in catalog} <= registered.keys()
    for leg in catalog:
        assert leg.model == registered[leg.id].model
    assert all(leg.disabled and not leg.windows and leg.max_request_tokens is None
               and leg.trains_on_data is None and not leg.roles for leg in registered.values()
               if leg.id in ('agy-gemini-4-hydrogen-high', 'agy-gemini-4-hydrogen-medium',
                             'cx-gpt-6.1-terra-medium', 'cx-gpt-6.1-terra-xhigh',
                             'muse-ember-max', 'claude-fable-5.2-high'))


def test_order_capabilities_are_existing_facts_or_unset(catalog):
    from plangraph.planner.legs import spawnable_legs
    registered = {leg.model: leg for leg in spawnable_legs()}
    for leg in catalog:
        source = registered.get(leg.model)
        actual = (leg.windows, leg.max_request_tokens, leg.trains_on_data)
        assert actual == ((source.windows, source.max_request_tokens, source.trains_on_data)
                          if source else ((), None, None)), leg.id
    haiku = next(leg for leg in catalog if leg.id == 'claude-haiku-5.5')
    assert (haiku.windows, haiku.max_request_tokens) == (('128k', '256k'), 180000)
    hydrogen = next(leg for leg in catalog if leg.id == 'agy-gemini-4-hydrogen-high')
    assert (hydrogen.windows, hydrogen.max_request_tokens, hydrogen.trains_on_data) == ((), None, None)


def test_d5712_sonnet_source_and_position_2_to_4(catalog):
    from plangraph.planner.legs import spawnable_legs
    sonnet = next(leg for leg in spawnable_legs() if leg.id == 'agy-claude-sonnet-5-5-high')
    assert sonnet.model == 'claude-sonnet-5-5-high' and sonnet.effort == 'high'
    with pytest.raises(ValueError, match='D-5712: claude_code_subscription_forbidden_for_implement'):
        ordered((replace(sonnet, harness='claude'),), 'implement')
    assert ordered((sonnet,), 'implement') == (sonnet,)
    medium = next(leg for leg in spawnable_legs() if leg.id == 'agy-sonnet-5.5')
    assert not ordered((medium,), 'implement')  # Never alias a medium registration to high.
    other = replace(sonnet, harness='opencode')
    assert ordered((other,), 'implement') == (replace(other, effort='high'),)
    absent = tuple(leg for leg in catalog if leg.id != sonnet.id)
    assert [leg.id for leg in ordered(absent, 'implement')][:3] == [
        'cx-gpt-6.1-sol-xhigh', 'agy-gemini-4-hydrogen-high', 'mimo-v2.6-pro']


@pytest.mark.parametrize('model,replacement', [('gemini-3.8-flash', 'gemini-4-hydrogen'),
    ('omniroute/gemini-3.8-flash-high', 'gemini-4-hydrogen'),
    ('omniroute/cx-gpt-6-luna', 'gpt-6.1-terra')])
def test_live_legacy_door_keeps_d5696_open(model, replacement):
    from plangraph.planner.legs import DEFAULT_LEGS
    legacy = replace(next(leg for leg in DEFAULT_LEGS if leg.id == 'agy-gemini-3.8-flash'),
                     id='legacy', model=model)
    # Do not enshrine successful live legacy routing. Item 2 is explicitly UNMET
    # in the changelog/report; only the fixed-order core has this refusal.
    with pytest.raises(ValueError, match=replacement):
        payment_order((legacy,), 'implement', order_class='standard')


def test_order_never_bypasses_role_admission(catalog):
    assert not ordered((replace(catalog[0], roles=('orchestrate',)),), 'implement')
    sonnet = replace(catalog[2], roles=('review',))
    assert not ordered((sonnet,), 'implement')


def test_unknown_capability_declarations_are_not_live_admissions():
    from plangraph.routing_support.resolver import DEFAULT_FALLBACK_MODELS
    unknown = {'gemini-4-hydrogen-high', 'gemini-4-hydrogen-medium',
               'cx-gpt-6.1-terra-medium', 'cx-gpt-6.1-terra-xhigh', 'muse-ember-max',
               'claude-fable-5-2'}
    rows = [row for row in DEFAULT_FALLBACK_MODELS if row['id'].rsplit('/', 1)[-1] in unknown]
    assert len(rows) == 6 and all(not row['active'] and row['available'] == 0 for row in rows)


@pytest.mark.parametrize('effort', [None, 'low', 'max'])
def test_order_preserves_registered_effort_not_id_suffix(catalog, effort):
    leg = replace(catalog[0], effort=effort)
    assert ordered((leg,), 'implement') == (leg,)
    assert ordered((leg,), 'implement')[0] is leg


def test_disabled_declaration_cannot_bypass_capability_validation(tmp_path):
    import json
    from dataclasses import asdict
    from plangraph.planner.legs import DEFAULT_LEGS, load_legs
    raw = asdict(next(leg for leg in DEFAULT_LEGS if leg.id == 'muse-ember-max'))
    path = tmp_path / 'legs.json'
    path.write_text(json.dumps({'legs': [raw]}))
    with pytest.raises(ValueError, match='windows.*max_request_tokens.*trains_on_data'):
        load_legs(path)
