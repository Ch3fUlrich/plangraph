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


def test_default_off(tmp_path):
    store = leg_state.LegStateStore(tmp_path / 'state')
    assert store.leg_status('opencode-go/absent') == {'state': 'ok', 'evidence': None}


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


def test_catalog_declarations_no_local_state():
    for file, names in [('routing_support/resolver.py', {'DEFAULT_FALLBACK_MODELS'}),
                        ('planner/legs.py', {'DEFAULT_LEGS', 'Leg'})]:
        tree = ast.parse((Path('src/plangraph') / file).read_text())
        for node in tree.body:
            targets = ([node.target] if isinstance(node, ast.AnnAssign) else
                       node.targets if isinstance(node, ast.Assign) else [])
            if any(isinstance(t, ast.Name) and t.id in names for t in targets):
                for child in ast.walk(node):
                    if isinstance(child, ast.Dict):
                        assert not any(isinstance(k, ast.Constant) and
                                       k.value in {'expires', 'available'} for k in child.keys)
            if isinstance(node, ast.ClassDef) and node.name == 'Leg':
                assert 'expires' not in {n.target.id for n in node.body
                                        if isinstance(n, ast.AnnAssign)}


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
