"""Synthetic runtime-boundary checks; not live acceptance evidence."""
import socket
import subprocess
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, host_keys


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*a, **kw):
        pytest.fail('external access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(host_keys, '_keys_path', denied)


def test_default_off(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    def forbidden(*a, **kw):
        pytest.fail('flag-off authority call')
    store = leg_state.LegStateStore(tmp_path / 'state',
        omni=SimpleNamespace(provider_state=forbidden), run=forbidden)
    assert store.leg_status('opencode-go/absent') == {'state': 'ok', 'evidence': None}
    assert store.leg_status('omniroute/absent') == {'state': 'ok', 'evidence': None}


@pytest.mark.parametrize('listed,expected', [('opencode-go/listed', 'ok'),
                                           ('other', 'not_listed')])
def test_list_admission(tmp_path, listed, expected):
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=listed)
    reader = SimpleNamespace(provider_state=lambda identity:
                             SimpleNamespace(available=True, stale=False, until=''))
    store = leg_state.LegStateStore(tmp_path / 'state', omni=reader, enabled=True, run=run)
    assert store.leg_status('opencode-go/listed')['state'] == expected
    store.leg_status('opencode-go/listed')
    assert len(calls) == 1 and calls[0][1]['timeout'] == 8
    assert calls[0][1].get('shell', False) is False


@pytest.mark.parametrize('state,expected', [(None, 'unavailable'),
    (SimpleNamespace(available=False, stale=False, until='later'), 'unavailable'),
    (SimpleNamespace(available=True, stale=True, until=''), 'stale')])
def test_authority(tmp_path, state, expected):
    reader = SimpleNamespace(provider_state=lambda identity: state)
    store = leg_state.LegStateStore(tmp_path / 'state', omni=reader, enabled=True)
    assert store.leg_status('omniroute/cx-gpt-6.1-sol')['state'] == expected


def test_failure_unknown(tmp_path):
    def run(*a, **kw):
        raise subprocess.TimeoutExpired('opencode', 8)
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=True, run=run)
    assert store.leg_status('opencode-go/absent')['state'] == 'unknown'


def declaration_violations(source, names):
    """Bounded to declarations and their construction loops, not runtime state."""
    bad = []
    for node in ast.parse(source).body:
        targets = ([node.target] if isinstance(node, ast.AnnAssign) else
                   node.targets if isinstance(node, ast.Assign) else [])
        selected = any(isinstance(t, ast.Name) and t.id in names for t in targets)
        selected |= isinstance(node, ast.ClassDef) and node.name == 'Leg'
        selected |= isinstance(node, ast.For) and any(
            isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))
        if not selected:
            continue
        for child in ast.walk(node):
            pairs = []
            if isinstance(child, ast.Dict):
                pairs = [(k.value, v) for k, v in zip(child.keys, child.values)
                         if isinstance(k, ast.Constant)]
            elif isinstance(child, ast.Call):
                pairs = [(k.arg, k.value) for k in child.keywords]
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                pairs = [(child.target.id, child.value)]
            elif isinstance(child, ast.Assign):
                pairs = [(t.slice.value, child.value) for t in child.targets
                         if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)]
            for key, value in pairs:
                if key == 'expires' or (key in {'available', '_catalog_available'}
                                        and not isinstance(value, (ast.Name, ast.Attribute, ast.Call))):
                    bad.append((key, child.lineno))
    return bad


def test_catalog_declarations_no_local_state():
    from plangraph.routing_support.resolver import DEFAULT_FALLBACK_MODELS
    assert all('available' not in row for row in DEFAULT_FALLBACK_MODELS)
    for file, names in [('routing_support/resolver.py', {'DEFAULT_FALLBACK_MODELS'}),
                        ('planner/legs.py', {'DEFAULT_LEGS', 'Leg'})]:
        assert not declaration_violations((Path('src/plangraph') / file).read_text(), names)


@pytest.mark.parametrize('declaration', [
    "DEFAULT_LEGS = [dict(expires='2099-01-01')]",
    "DEFAULT_LEGS = [{'available': True}]",
    "DEFAULT_LEGS = [dict(_catalog_available=True)]",
    "class Leg:\n expires: str = ''",
    "for row in DEFAULT_LEGS:\n row['_catalog_available'] = True",
])
def test_declaration_scan_negative_fixtures(declaration):
    assert declaration_violations(declaration, {'DEFAULT_LEGS'})


def test_declaration_scan_allows_dynamic_and_unrelated_state():
    assert not declaration_violations(
        "DEFAULT_LEGS = [dict(available=state.available)]\n"
        "def reader():\n return dict(available=False, expires='later')", {'DEFAULT_LEGS'})


def test_identity_and_recovery(tmp_path):
    from plangraph.routing_support.omniroute_state import OmniRouteUnreachable
    calls = []
    def read(identity):
        calls.append(identity)
        if len(calls) == 2:
            raise OmniRouteUnreachable('transport_error')
        return SimpleNamespace(available=identity == 'cx-gpt-6-terra', stale=False, until='')
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=True,
                                   omni=SimpleNamespace(provider_state=read))
    assert store.leg_status('omniroute/cx-gpt-6.1-sol')['state'] == 'unavailable'
    assert store.leg_status('omniroute/cx-gpt-6.1-sol')['state'] == 'unreachable'
    assert store.leg_status('omniroute/cx-gpt-6.1-sol')['state'] == 'unavailable'
    store.leg_status('agy/claude-sonnet-5-5-medium')
    store.leg_status('agy/claude-sonnet-5-5-high')
    assert calls[-1] == calls[-2]


def test_production_identity_aliases_and_disabled_retention(tmp_path):
    import json
    from plangraph.routing_support.omniroute_state import OmniRouteState
    from plangraph.planner.legs import DEFAULT_LEGS, leg_active
    rows = [dict(provider='cx-gpt-6-terra', isActive=True),
            dict(provider='agy/claude-sonnet-5-5', isActive=True),
            dict(provider='disabled', isActive=False)]
    reader = OmniRouteState('injected-endpoint', key='synthetic',
        fetch=lambda *a: (200, json.dumps({'connections': rows})))
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=True, omni=reader)
    assert store.leg_status('omniroute/cx-gpt-6-terra')['state'] == 'ok'
    assert store.leg_status('omniroute/cx-gpt-6.1-sol')['state'] == 'unavailable'
    for effort in ('medium', 'high'):
        assert store.leg_status('agy/claude-sonnet-5-5-' + effort)['state'] == 'ok'
    assert 'disabled' in reader.snapshot()
    assert not reader.snapshot()['disabled'].available
    disabled = [leg for leg in DEFAULT_LEGS if leg.disabled]
    assert disabled and all(not leg_active(leg) for leg in disabled)


@pytest.mark.parametrize('status', ['unavailable', 'stale'])
def test_refreshed_probe_recovery(tmp_path, monkeypatch, status):
    import asyncio
    from plangraph.spawner import leg_probe
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=True,
        omni=SimpleNamespace(provider_state=lambda identity:
                             SimpleNamespace(available=True, stale=False, until='')))
    store.record_probe_fail('omniroute/test', 'transient', caller='test')
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    calls = []
    async def probe(*a, **kw):
        calls.append(kw['store'])
        return dict(ok=True)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(SimpleNamespace(leg_reader=store),
        SimpleNamespace(model='omniroute/test'), 'fix', False)) is None
    assert calls == [store]


def test_flag_off_next_tick_baseline(monkeypatch):
    from plangraph.routing_support import resolver
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    rows = [dict(row, available=1) for row in resolver.DEFAULT_FALLBACK_MODELS]
    def tick(state):
        reader = lambda *a, **kw: dict(state=state)
        admitted = [r for r in rows if resolver.is_model_available(r, reader)]
        return [(r['id'], r.get('status', 'active'))
                for r in resolver.rank_candidates(admitted)]
    baseline = tick('ok')
    assert baseline
    assert tick('cooldown') == baseline
    assert tick('limited') == []
    for status in ('unavailable', 'stale', 'unknown', 'unreachable', 'not_listed'):
        assert tick(status) == baseline


def test_catalog_explicit_admission():
    from plangraph.routing_support import resolver as r
    row = next(dict(row, available=1) for row in r.DEFAULT_FALLBACK_MODELS if row['active'])
    for extra in ({}, {'note': 'synthetic'}):
        candidate = dict(row, **extra)
        assert r.catalog_row_admitted(candidate)
        assert r.catalog_row_admitted(r._normalize_catalog_row(candidate))
    row.pop('active')
    assert not r.catalog_row_admitted(row)
    assert not r.catalog_row_admitted(r._normalize_catalog_row(row))


def test_missing_available_fails_closed(monkeypatch):
    from plangraph.routing_support import resolver as r
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    row = dict(id='synthetic/model', active=True)
    for candidate in (row, r._normalize_catalog_row(row)):
        assert not r.catalog_row_admitted(candidate)
        assert not r.is_model_available(candidate)


@pytest.mark.parametrize('enabled', [False, True])
def test_operator_removal_before_reader(tmp_path, monkeypatch, enabled):
    from plangraph.routing_support import resolver as r
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', str(int(enabled)))
    model = 'synthetic/removed'
    monkeypatch.setattr(r, 'OPERATOR_REMOVED_MODELS', frozenset({model}))
    def forbidden(*a, **kw):
        pytest.fail('removed model reached availability reader')
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=enabled,
                                   omni=SimpleNamespace(provider_state=forbidden))
    assert not r.is_model_available(dict(id=model, active=True, available=1), store.leg_status)


@pytest.mark.parametrize('status', ['ok', 'limited', 'unavailable', 'unreachable',
                                   'stale', 'unknown', 'not_listed'])
@pytest.mark.parametrize('enabled', [False, True])
def test_reader_decides_policy(tmp_path, monkeypatch, status, enabled):
    from plangraph.routing_support import resolver as r
    if enabled:
        monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    else:
        monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '1')
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=enabled)
    monkeypatch.setattr(store, 'authoritative_status', lambda leg: dict(state=status))
    monkeypatch.setattr(leg_state.LegStateStore, 'leg_status',
                        lambda self, leg, **kw: dict(state=status))
    row = dict(id='synthetic/model', active=True, available=1)
    assert r.is_model_available(row, store.leg_status) == (
        status == 'ok' if enabled else status != 'limited')
    row.pop('available')
    assert r.is_model_available(row, store.leg_status) == (enabled and status == 'ok')


def test_before_launch_one_authority_read(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    calls = []
    def read(identity):
        calls.append(identity)
        return SimpleNamespace(available=True, stale=False, until='')
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=True,
                                   omni=SimpleNamespace(provider_state=read))
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '0')
    assert asyncio.run(leg_probe.before_launch(SimpleNamespace(leg_reader=store),
        SimpleNamespace(model='omniroute/synthetic'), 'fix', False)) is None
    assert calls == ['synthetic']


def test_default_store_tracks_environment(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    for name in ('first', 'second'):
        root = tmp_path / name
        monkeypatch.setenv('XDG_STATE_HOME', str(root))
        store = leg_state._default_store()
        assert store.path == root / 'plangraph' / 'leg-state.sqlite'
        assert not store.enabled


def test_changelog_contract():
    lines = Path('changelog.d/SPAWNER-LEG-STATE-FROM-OMNIROUTE-leg-adapter.md').read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith('- **SPAWNER-LEG-STATE-FROM-OMNIROUTE:**')


@pytest.mark.parametrize('health', ['available', 'unavailable', 'unreachable'])
@pytest.mark.parametrize('served', [True, False])
def test_enabled_route_matrix(tmp_path, monkeypatch, health, served):
    from plangraph.routing_support import resolver as r
    from plangraph.routing_support.omniroute_state import OmniRouteUnreachable
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '1')
    monkeypatch.setattr(r, 'host_probe_env', lambda env: {})
    monkeypatch.setattr('plangraph.routing_support.opencode_render.render_gateway_providers',
                        lambda env, model: None)
    calls = []
    def read(identity):
        calls.append(identity)
        if health == 'unreachable':
            raise OmniRouteUnreachable('transport_error')
        return SimpleNamespace(available=health == 'available' and identity == 'cx-gpt-6-terra',
                               stale=False, until='')
    store = leg_state.LegStateStore(tmp_path / 'state', enabled=True,
                                   omni=SimpleNamespace(provider_state=read))
    for model in ('cx-gpt-6-terra', 'cx-gpt-6.1-sol'):
        row = dict(id='omniroute/' + model, active=True, harness='opencode')
        available = r.is_model_available(row, store.leg_status)
        reason = r.host_skip_reason(row, probe=lambda env: {row['id']} if served else {'other/model'})
        assert (available and reason is None) == (health == 'available' and served and model == 'cx-gpt-6-terra')
    assert calls == ['cx-gpt-6-terra', 'cx-gpt-6.1-sol']
