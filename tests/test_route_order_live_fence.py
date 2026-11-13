"""Legacy refusals exercise the planner and public spawner doors, not ordering."""
import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.planner import legs, routing
from plangraph.routing_support import resolver
from plangraph.spawner.service import SpawnService

ROLES = ('implement', 'review', 'orchestrate', 'research')
LEGACY = [('agy-gemini-3.8-flash', 'gemini-4-hydrogen'),
          ('cx-gpt-6-luna', 'gpt-6.1-terra')]
CARD = {'estimate_tokens': 1000, 'diff_ref': {'repo': '/repo', 'base': 'HEAD~1', 'head': 'HEAD'}}


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: ())
    reader = lambda *a, **kw: {'state': 'ok', 'evidence': None}
    service = SpawnService(state=tmp_path, leg_reader=reader)
    return service, reader


@pytest.mark.parametrize('door', ['plan_spawn', 'route'])
@pytest.mark.parametrize('role', ROLES)
def test_default_live_route_excludes_legacy(live, door, role):
    service, reader = live
    plan = (routing.plan_spawn(CARD, role=role, writer_families=('claude',), leg_reader=reader)
            if door == 'plan_spawn' else service.route(CARD, role, writer_families=('claude',)))
    assert plan.legs
    assert not any(token in (leg.id + leg.model).lower() for leg in (*plan.legs, *plan.review_seats)
                   for token in ('gemini-3.8-flash', 'gpt-6-luna'))
    for id, replacement in LEGACY:
        source = next(leg for leg in legs.DEFAULT_LEGS if leg.id == id)
        eligible = ('orchestrate' if role == 'research' else role) in source.roles
        assert any(row['leg'] == id and replacement in row['reason'] for row in plan.skipped) == eligible


@pytest.mark.parametrize('door', ['plan_spawn', 'route'])
@pytest.mark.parametrize('role', ROLES)
@pytest.mark.parametrize('id,replacement', LEGACY)
@pytest.mark.parametrize('identity', ['both', 'model', 'id'])
@pytest.mark.parametrize('fallback', [False, True])
def test_unpinned_legacy_alias_cannot_route(live, monkeypatch, door, role, id, replacement, identity, fallback):
    service, reader = live
    source = next(leg for leg in legs.DEFAULT_LEGS if leg.id == id)
    legacy = replace(source, roles=('implement', 'review', 'orchestrate'),
                     id='renamed' if identity == 'model' else source.id.upper(),
                     model='omniroute/' + source.model.upper() + '-high' if identity != 'id'
                     else 'omniroute/cx-gpt-6.1-sol-xhigh')
    allowed = next(leg for leg in legs.DEFAULT_LEGS if leg.id == 'mimo-v2.6-pro')
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: (
        {'id': legacy.model, 'active': True, 'available': 1},))
    expected = (allowed,) if fallback else ()
    monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, 'legs', (legacy, *expected))
    if door == 'plan_spawn':
        plan = routing.plan_spawn(CARD, role=role, writer_families=('claude',), leg_reader=reader)
        assert plan.legs == expected
        assert all(seat != legacy for seat in plan.review_seats)
        assert any(row['leg'] == legacy.id and replacement in row['reason'] for row in plan.skipped)
    else:
        if role == 'review' or not fallback:
            with pytest.raises(routing.SpawnRefused, match=replacement):
                service.route(CARD, role, writer_families=('claude',))
            assert replacement in service.ps()['runs'][0]['error']
        else:
            plan = service.route(CARD, role, writer_families=('claude',))
            assert plan.legs == expected
            assert any(row['leg'] == legacy.id and replacement in row['reason'] for row in plan.skipped)


@pytest.mark.parametrize('id,replacement', [*LEGACY,
    ('gemini-3.8-flash', 'gemini-4-hydrogen'),
    ('omniroute/gemini-3.8-flash-high', 'gemini-4-hydrogen'),
    ('agy/gemini-3.8-flash-tiered-high', 'gemini-4-hydrogen'),
    ('gpt-6-luna', 'gpt-6.1-terra'), ('cxa/gpt-6-luna-high', 'gpt-6.1-terra'),
    ('omniroute/cx-gpt-6-luna', 'gpt-6.1-terra')])
@pytest.mark.parametrize('role', ROLES)
def test_live_admission_refuses_legacy_but_allowed_pin_routes(live, tmp_path, id, replacement, role):
    service, _ = live
    pin = {'leg': id} if id in dict(LEGACY) else {'model': id}
    with pytest.raises(routing.SpawnRefused, match=replacement):
        asyncio.run(service.spawn(card=CARD, prompt='Review the supplied change.', role=role,
            writer_families=('claude',), **pin, override_reason='Explicit admission regression', dry_run=True))
    assert not (tmp_path / 'spawn-overrides.jsonl').exists()
    result = asyncio.run(service.spawn(card={'estimate_tokens': 1000}, prompt='Implement the supplied card.',
        leg='cx-gpt-6.1-sol-xhigh', override_reason='Explicit allowed leg regression', dry_run=True))
    assert result['status'] == 'dry_run'
    assert result['chosen_leg']['id'] == 'cx-gpt-6.1-sol-xhigh'


def test_live_exhaustion_retains_legacy_refusal(tmp_path):
    from plangraph.spawner.runner import spawn_service
    service = spawn_service(state=tmp_path, leg_reader=lambda *a, **kw: {'state': 'limited', 'evidence': None})
    record = asyncio.run(service.spawn(card={'estimate_tokens': 1000}, prompt='Implement public code.', dry_run=True))
    assert record['exit_code'] == 4
    assert any('gemini-4-hydrogen' in row['skip_reason'] for row in record['attempts'])


@pytest.mark.parametrize('door', ['gate_card', 'route_for_card'])
def test_keeper_route_fences_real_default_legs(monkeypatch, tmp_path, door):
    from plangraph.planner import service
    store = SimpleNamespace(get_card=lambda *a: CARD)
    if door == 'gate_card':
        monkeypatch.setattr(service, '_item_state', lambda *a: 'closed')
        monkeypatch.setattr(service, 'running_write_sets', lambda *a, **kw: {})
        store.conn = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: None))
        route = service.gate_card(store, None, 'demo:A', repo_root=tmp_path).route
    else:
        route = service.route_for_card(store, None, 'demo:A')
    assert route.legs
    assert not any(token in leg.id for leg in route.legs for token, _ in LEGACY)


@pytest.mark.parametrize('door', ['plan_spawn', 'route'])
@pytest.mark.parametrize('source', [leg for leg in legs.spawnable_legs()
    if any(token in leg.id for token, _ in LEGACY)], ids=lambda leg: leg.id)
def test_real_registry_legacy_efforts_refused(live, door, source):
    service, reader = live
    if door == 'plan_spawn':
        plan = routing.plan_spawn(CARD, role='review', legs=(source,), leg_reader=reader,
                                  writer_families=('claude',))
        assert not plan.legs and not plan.review_seats
        assert 'legacy model refused:' in plan.skipped[0]['reason']
    else:
        plan = service.route(CARD, 'review', writer_families=('claude',))
        assert source not in (*plan.legs, *plan.review_seats)
        assert any(row['leg'] == source.id for row in plan.skipped)
        with pytest.raises(routing.SpawnRefused, match='legacy model refused:'):
            asyncio.run(service.spawn(card=CARD, prompt='Review the supplied change.', role='review',
                writer_families=('claude',), leg=source.id,
                override_reason='Real registry legacy regression', dry_run=True))


@pytest.mark.parametrize('field', ['id', 'model'])
@pytest.mark.parametrize('value', [None, 42])
def test_nonstring_identity_does_not_mask_legacy_refusal(live, field, value):
    from plangraph.planner.route_policy import legacy_route_refusal
    from plangraph.spawner.target import admit_leg
    assert legacy_route_refusal(value) is None
    source = replace(next(leg for leg in legs.DEFAULT_LEGS if leg.id == LEGACY[0][0]), **{field: value})
    with pytest.raises(routing.SpawnRefused, match='gemini-4-hydrogen'):
        admit_leg(source)
    plan = routing.plan_spawn(CARD, role='review', legs=(source,), leg_reader=live[1],
                              writer_families=('claude',))
    assert not plan.legs and 'gemini-4-hydrogen' in plan.skipped[0]['reason']


def test_fence_uses_closed_order_core_legacy_list(monkeypatch):
    from plangraph.planner.route_policy import legacy_route_refusal
    from plangraph.routing_support.catalog_seed import LEGACY_ROUTE_REPLACEMENTS
    for legacy, replacement in LEGACY_ROUTE_REPLACEMENTS.items():
        assert replacement in legacy_route_refusal(None, 'provider/' + legacy.upper() + '-high')
    monkeypatch.setitem(LEGACY_ROUTE_REPLACEMENTS, 'retired-third', 'replacement-third')
    assert 'replacement-third' in legacy_route_refusal('provider/RETIRED-THIRD-high')


def test_legacy_rows_alone_never_mean_provider_exhaustion():
    from plangraph.routing_support.liveness import provider_exhausted
    assert not provider_exhausted([{'reason': 'legacy model refused: retired'}])


@pytest.mark.parametrize('contract', ['routing-scope', 'writer-order', 'policy', 'writer-family', 'inline-diff'])
def test_documentation_contract_preserved(contract):
    root = Path(__file__).resolve().parents[1]
    if contract == 'routing-scope':
        assert 'D-5696 live admission supersedes' not in (root / 'docs/routing.md').read_text()
    elif contract == 'writer-order':
        assert 'Writers use Sol xhigh then agy Gemini Flash (`WRITER_ORDER`); plain Sol is explicit-only.' in (root / 'docs/SPAWNER.md').read_text()
    else:
        assert {'policy': '``policy`` (default :func:`load_route_policy`)',
                'writer-family': 'ANY writer family',
                'inline-diff': 'the spawner inlines that diff'}[contract] in routing.plan_spawn.__doc__
