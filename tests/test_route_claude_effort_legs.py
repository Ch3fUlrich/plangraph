"""Claude Sonnet/Opus 5.5 legs and effort-variant legs (operator 2026-11-09).

Pins: the leg data (harness, served model id, family, roles), the family fence
(a claude seat is refused for a claude writer, given or via review_of; with mixed
writers D-3084 admits a seat that covers another writer part), the D-3828 writer rule (the
new writers are hand-picked only, never a routed fallback), the ``agy/`` model
spelling the MCP override passes, and the per-run opencode model entry an
effort variant needs to be routable at all.
"""
import asyncio
import json

import pytest

from plangraph.planner.legs import (DEFAULT_LEGS, EXPLICIT_LEGS, EXPLICIT_WRITERS, LEAD_WRITER,
                                    spawnable_legs)
from plangraph.planner.route_policy import RoutePolicy
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.routing_support import leg_state

BY_ID = {leg.id: leg for leg in spawnable_legs()}
CLAUDE = {f"agy-claude-{m}-5-5-{e}": f"claude-{m}-5-5-{e}"
          for m in ("sonnet", "opus") for e in ("low", "medium", "high")}
SOL = {f"cx-gpt-6.1-sol-{e}": f"omniroute/cx-gpt-6.1-sol-{e}"
       for e in ("low", "medium", "high", "xhigh", "max")}
LUNA = {f"cx-gpt-6-luna-{e}": f"omniroute/cx-gpt-6-luna-{e}"
        for e in ("low", "medium", "high", "xhigh", "max")}
GEMINI = {}  # Gemini effort variants retired; Claude/Sol/Luna retain effort coverage.
NEW = {**CLAUDE, **SOL, **LUNA, **GEMINI}
MANUAL = (set(NEW) - {LEAD_WRITER}) | {'cx-gpt-6.1-sol'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: [])
    st = leg_state.LegStateStore(tmp_path / 'leg-state.sqlite')
    monkeypatch.setattr(leg_state, '_default_store', lambda: st)
    return st


def plan(store, role='review', writers=('xiaomi',), legs=DEFAULT_LEGS):
    # No diff_ref: inline-only legs (luna) skip with inline_only_needs_diff.
    return plan_spawn(dict(estimate_tokens=1000), role=role, writer_families=writers, leg_reader=store, now=100,
                      policy=RoutePolicy(), legs=legs, catalog_rows=())


def test_leg_data():
    for leg_id, model in CLAUDE.items():
        leg = BY_ID[leg_id]
        assert (leg.harness, leg.model, leg.family, leg.claude_family, leg.tier) == (
            'agy', model, 'claude', True, 'subscription'), leg_id
        assert leg.roles == ('review', 'implement') and leg.disabled is None, leg_id
        assert leg.windows == ('128k', '256k', '1m')
    for leg_id, model in SOL.items():
        leg = BY_ID[leg_id]
        assert (leg.harness, leg.model, leg.family, leg.roles, leg.disabled) == (
            'opencode', model, 'gpt', ('implement',), None), leg_id
    for leg_id, model in LUNA.items():
        leg = BY_ID[leg_id]
        assert (leg.harness, leg.model, leg.family, leg.roles, leg.tool_mode) == (
            'opencode', model, 'gpt', ('review',), 'inline-only'), leg_id
    assert not any("flash" in leg.model and "gemini" in leg.model for leg in BY_ID.values())
    ids = [leg.id for leg in spawnable_legs()]
    assert len(ids) == len(set(ids))


def test_explicit_legs_stay_out_of_every_route():
    """D-5206 promotes only Sol xhigh; every other effort leg stays explicit-only."""
    declarations = {'cx-gpt-6.1-terra-medium', 'cx-gpt-6.1-terra-xhigh',
                    'muse-ember-max', 'claude-fable-5.2-high'}
    assert {leg.id for leg in EXPLICIT_LEGS} == MANUAL | declarations
    assert all(leg.disabled and not leg.roles and leg.expires is None
               for leg in EXPLICIT_LEGS if leg.id in declarations)
    assert {leg.id for leg in DEFAULT_LEGS} & set(NEW) == {LEAD_WRITER}
    assert spawnable_legs() == DEFAULT_LEGS + EXPLICIT_LEGS


def test_spawnable_legs_follow_a_rebound_default_table(monkeypatch):
    """Tests and callers rebind legs.DEFAULT_LEGS; explicit lookups must see it."""
    from dataclasses import replace
    from plangraph.planner import legs
    fake = (replace(DEFAULT_LEGS[0], id='zz-test', model='test/zz', disabled=None),)
    monkeypatch.setattr(legs, 'DEFAULT_LEGS', fake)
    assert spawnable_legs() == fake + EXPLICIT_LEGS
    from plangraph.spawner.target import resolve_target
    assert resolve_target('zz-test')[1].id == 'zz-test'
    assert EXPLICIT_WRITERS == (frozenset(CLAUDE) | frozenset(SOL) | {'cx-gpt-6.1-sol'}) - {LEAD_WRITER}


@pytest.mark.parametrize('role,listed', [('review', {**CLAUDE, **GEMINI, **LUNA}),
                                         ('implement', (set(CLAUDE) | set(SOL) | {'cx-gpt-6.1-sol'}) - {LEAD_WRITER})])
def test_route_lists_them_as_explicit_only(store, tmp_path, role, listed):
    writers = ('xiaomi',) if role == 'review' else ()
    routed = _svc(tmp_path).route({'estimate_tokens': 1000}, role=role, writer_families=writers)
    assert not {leg.id for leg in routed.legs} & MANUAL
    explicit = {s['leg'] for s in routed.skipped if s['reason'] == 'explicit_only'}
    assert explicit == set(listed)
    if role == 'implement':
        from test_route_free_first import WRITERS
        assert [leg.id for leg in routed.legs] == WRITERS


@pytest.mark.parametrize('role', ['review', 'implement'])
def test_plan_spawn_skip_accounting_unchanged(store, role):
    """Only `route` output lists them; plan_spawn (spawn plans, keeper) stays as on main."""
    result = plan(store, role=role, writers=('xiaomi',) if role == 'review' else ())
    assert not any(s['reason'] == 'explicit_only' for s in result.skipped)
    assert not {leg.id for leg in result.legs} & MANUAL


def _svc(tmp_path):
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner.service import SpawnService
    return SpawnService(SpawnerRunner(), state=tmp_path)


@pytest.mark.parametrize('leg_id', ['agy-claude-sonnet-5-5-high', 'agy-claude-opus-5-5-high'])
def test_explicit_claude_review_seat(tmp_path, leg_id):
    result = asyncio.run(_svc(tmp_path).spawn(
        card={'estimate_tokens': 20000}, prompt='Review public code', role='review',
        writer_families=('gpt',), leg=leg_id, dry_run=True,
        override_reason='operator 2026-11-09 claude seats'))
    assert result['chosen_leg']['id'] == leg_id
    with pytest.raises(SpawnRefused, match='excluded writer family'):
        asyncio.run(_svc(tmp_path).spawn(
            card={'estimate_tokens': 20000}, prompt='Review public code', role='review',
            writer_families=('claude',), leg=leg_id, dry_run=True,
            override_reason='operator 2026-11-09 claude seats'))


@pytest.mark.parametrize('leg_id', ['agy-claude-opus-5-5-high', 'cx-gpt-6.1-sol', *SOL, 'agy-gemini-4-argon'])
def test_explicit_writer_implement_allowed(tmp_path, leg_id):
    result = asyncio.run(_svc(tmp_path).spawn(
        card={'estimate_tokens': 20000}, prompt='Implement public code', role='implement',
        leg=leg_id, dry_run=True, override_reason='operator 2026-11-09 explicit writer'))
    assert result['chosen_leg']['id'] == leg_id


def test_other_writers_still_refused(tmp_path):
    with pytest.raises(SpawnRefused, match='D-3828'):
        asyncio.run(_svc(tmp_path).spawn(
            card={'estimate_tokens': 20000}, prompt='Implement public code', role='implement',
            leg='agy-gpt-oss-120b', dry_run=True,
            override_reason='operator 2026-11-09 explicit writer'))


@pytest.mark.parametrize('leg_id', sorted(CLAUDE))
def test_agy_prefixed_model_resolves_to_its_leg(leg_id):
    from plangraph.spawner.target import resolve_target
    target, leg = resolve_target('agy/' + CLAUDE[leg_id])
    assert (target, leg.id) == (CLAUDE[leg_id], leg_id)


@pytest.mark.parametrize('model,leg_id', [
    ('agy/gemini-4-argon-high', 'agy-gemini-4-argon'),
    # agy-sonnet-5.5 (orchestrate only) shares this model; the seat leg wins.
    ('agy/claude-sonnet-5-5-medium', 'agy-claude-sonnet-5-5-medium')])
def test_agy_prefixed_model_prefers_the_seat_leg(model, leg_id):
    from plangraph.spawner.target import resolve_target
    assert resolve_target(model)[1].id == leg_id


def test_claude_seat_refused_for_a_claude_run_named_by_review_of(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(svc, 'result', lambda *a, **k: {'given_writer_families': ['claude'],
                                                        'writer_family': 'claude'})
    with pytest.raises(SpawnRefused, match='excluded writer family'):
        asyncio.run(svc.spawn(card={'estimate_tokens': 20000}, prompt='Review public code',
                              role='review', review_of='run-' + '0' * 32,
                              leg='agy-claude-opus-5-5-high', dry_run=True,
                              override_reason='operator 2026-11-09 claude seats'))


def test_service_route_lists_explicit_legs(tmp_path):
    """The real `route` entry point (not only plan_spawn called directly)."""
    plan = _svc(tmp_path).route({'estimate_tokens': 20000}, role='review',
                                writer_families=('gpt',))
    skipped = plan['skipped'] if isinstance(plan, dict) else plan.skipped
    assert {s['leg'] for s in skipped if s['reason'] == 'explicit_only'} >= set(CLAUDE)


def test_agy_command_passes_the_served_model():
    from plangraph.routing_support.clients import build_command, launch_stdin_payload
    cmd = build_command('hi', 'claude-opus-5-5-high', role='review', client='agy')
    assert cmd == ['agy', '--dangerously-skip-permissions', '--model',
                   'claude-opus-5-5-high', '-p', '', '--input-format', 'stream-json', '--output-format', 'stream-json']
    assert json.loads(launch_stdin_payload(cmd, 'hi', 'agy')) == {
        'event': 'user', 'message': {'role': 'user', 'content': 'hi'}}


def _render(model, models=None):
    """Render as a spawned run does: the root overlay's ``providers`` block, no global layer."""
    from plangraph.routing_support.opencode_render import render_gateway_providers
    root = {'providers': {'omniroute': {'env': ['AUTOOS_OMNIROUTE_KEY'],
                                        'models': dict(models or {})}}}
    env = {'OMNIROUTE_BASE_URL': 'http://gateway.invalid',
           'OPENCODE_CONFIG_CONTENT': json.dumps(root)}
    assert render_gateway_providers(env, model) is None
    return json.loads(env['OPENCODE_CONFIG_CONTENT'])['providers']['omniroute']['models']


@pytest.mark.parametrize('model', sorted(SOL.values()) + sorted(LUNA.values()))
def test_effort_variant_gets_a_per_run_opencode_model(model):
    key = model.split('/', 1)[1]
    entry = _render(model)[key]
    assert entry['modelID'] == 'cx/' + key[len('cx-'):]
    assert entry['limit'] == {'context': 400000, 'output': 128000}


def test_effort_variant_takes_its_base_model_limit():
    base = {'modelID': 'cx/gpt-6.1-sol', 'limit': {'context': 475000, 'output': 128000}}
    models = _render('omniroute/cx-gpt-6.1-sol-xhigh', {'cx-gpt-6.1-sol': base})
    assert models['cx-gpt-6.1-sol-xhigh']['limit'] == base['limit']
    assert models['cx-gpt-6.1-sol'] == base


@pytest.mark.parametrize('model', ['omniroute/cx-gpt-6.1-sol', 'omniroute/cx-gpt-6-luna',
                                   'omniroute/agy/gemini-4-argon-high',
                                   'omniroute/cx-gpt-6.1-sol-ultra'])
def test_base_and_unknown_models_get_no_entry(model):
    assert _render(model) == {}


def test_declared_entry_is_kept():
    mine = {'modelID': 'cx/gpt-6.1-sol-high', 'limit': {'context': 1, 'output': 1}}
    models = _render('omniroute/cx-gpt-6.1-sol-high', {'cx-gpt-6.1-sol-high': mine})
    assert models['cx-gpt-6.1-sol-high'] == mine
