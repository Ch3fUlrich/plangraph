"""Hermetic default-on and next-tick boundary regressions."""
import json
import socket
import subprocess
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, leg_state_flag as flag, resolver
from plangraph.spawner.service import SpawnService


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    def deny(*a, **kw):
        pytest.fail('external access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(subprocess, 'run', deny)
    flag.default_reader.cache_clear()
    yield
    flag.default_reader.cache_clear()


def service(monkeypatch, tmp_path, snapshot=None):
    from plangraph.routing_support import omniroute_state
    provider = SimpleNamespace(snapshot=snapshot or (lambda: {}))
    monkeypatch.setattr(omniroute_state, 'from_env', lambda: provider)
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: [m['id'] for m in resolver.DEFAULT_FALLBACK_MODELS]))
    return SpawnService(runner=SimpleNamespace(), state=tmp_path)


@pytest.mark.parametrize('value', [None, '', '1', 'false'])
def test_default_on(monkeypatch, tmp_path, value):
    if value is not None:
        monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', value)
    s = service(monkeypatch, tmp_path)
    assert s.leg_reader.enabled is True
    assert s.runner.leg_reader is s.leg_reader is flag.default_reader()
    assert SpawnService(runner=SimpleNamespace(), state=tmp_path).leg_reader is s.leg_reader


def test_off_byte_identical(monkeypatch, tmp_path):
    from plangraph.planner.routing import plan_spawn
    from test_leg_adapter import MAIN_ORDER
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    s = service(monkeypatch, tmp_path, lambda: pytest.fail('disabled reader called'))
    assert s.leg_reader.enabled is False
    card = dict(estimate_tokens=1000)
    s.hung.waiting.update(m['id'] for m in resolver.DEFAULT_FALLBACK_MODELS)
    assert json.dumps(asdict(s._route(card))) == json.dumps(asdict(plan_spawn(card, role='implement', leg_reader=s.leg_reader.store)))
    assert [m['id'] for m in resolver.DEFAULT_FALLBACK_MODELS if resolver.is_model_available(m, s.leg_reader)] == MAIN_ORDER


def test_off_forwards_injected_reader(monkeypatch, tmp_path):
    from plangraph.planner import routing
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    reader = leg_state.LegStateStore(tmp_path / 'legs')
    s = SpawnService(runner=SimpleNamespace(), state=tmp_path, leg_reader=reader)
    def plan_spawn(**kwargs):
        assert kwargs['leg_reader'] is reader
        return 'legacy'
    monkeypatch.setattr(routing, 'plan_spawn', plan_spawn)
    assert s._route(dict(estimate_tokens=1000)) == 'legacy'


@pytest.mark.parametrize('failure', ['empty', 'exception', 'stale'])
def test_reader_failure_reported(monkeypatch, tmp_path, failure):
    def snapshot():
        if failure == 'exception':
            raise RuntimeError('unreachable')
        if failure == 'empty':
            return {}
        return {m['id'].split('/')[0]: SimpleNamespace(available=True, stale=True)
                for m in resolver.DEFAULT_FALLBACK_MODELS}
    s = service(monkeypatch, tmp_path, snapshot)
    plan = s._route(dict(estimate_tokens=1000))
    assert plan.legs
    assert any(c.get('leg_state_reason') in ('unknown', 'unreachable', 'stale') for c in plan.candidates)
    row = dict(id='opencode-go/gemini-4-argon', active=True, available=1)
    assert resolver.is_model_available(row, s.leg_reader)
    assert not resolver.is_model_available(dict(row, available=0), s.leg_reader)
    assert s.leg_reader(row['id'])['state'] == 'limited'


@pytest.mark.parametrize('on', [True, False])
def test_hung_routing_stable(monkeypatch, tmp_path, on):
    from plangraph.spawner.leg_probe import key
    if not on:
        monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    s = service(monkeypatch, tmp_path)
    card = dict(estimate_tokens=1000)
    before = s._route(card).legs
    assert len(before) > 2
    s.hung.waiting.add(key(before[0]))
    after = s._route(card).legs
    assert after == (before[1:] + before[:1] if on else before)


def test_standalone_argv(monkeypatch):
    def run(argv, **kw):
        assert argv == ['opencode', 'models', '--standalone']
        assert kw == dict(capture_output=True, text=True, timeout=8, shell=False, check=True)
        return SimpleNamespace(stdout='provider/sample\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == ['provider/sample']


def test_flip_disabled_store_equivalence(monkeypatch, tmp_path):
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    s = service(monkeypatch, tmp_path, lambda: pytest.fail('disabled reader called'))
    for model in resolver.DEFAULT_FALLBACK_MODELS:
        assert resolver.is_model_available(model, None) == resolver.is_model_available(model, s.leg_reader.store.leg_status)


@pytest.mark.parametrize('on', [False, True])
def test_flip_explicit_plan_reader(monkeypatch, tmp_path, on):
    from plangraph.planner import routing
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '1' if on else '0')
    s = service(monkeypatch, tmp_path)
    reader = leg_state.LegStateStore(tmp_path / 'override')
    original = routing.plan_spawn
    def plan_spawn(**kwargs):
        assert kwargs['leg_reader'] is reader
        return original(dict(estimate_tokens=1000), role='implement', **kwargs)
    monkeypatch.setattr(routing, 'plan_spawn', plan_spawn)
    assert flag.service_plan(s, leg_reader=reader).legs
