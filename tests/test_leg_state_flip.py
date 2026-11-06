"""Hermetic default-on, fallback and next-tick ordering proofs."""
from dataclasses import asdict
import json
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state_flag as flag, leg_state
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.service import SpawnService

_production_reader = flag.default_reader


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(flag, 'default_reader', _production_reader)
    flag.default_reader.cache_clear()
    monkeypatch.delenv('PLANGRAPH_OMNIROUTE_LEG_STATE', raising=False)
    monkeypatch.setattr(leg_state, '_default_store', lambda: leg_state.LegStateStore(tmp_path / 'state'))
    from plangraph.routing_support import omniroute_state
    monkeypatch.setattr(omniroute_state, 'from_env', lambda: SimpleNamespace(snapshot=lambda: {}))
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: []))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('unexpected subprocess'))
    yield
    flag.default_reader.cache_clear()


def service(tmp_path):
    return SpawnService(runner=SimpleNamespace(), state=tmp_path)


CARD = dict(estimate_tokens=1000)


@pytest.mark.parametrize('value', [None, '', '1', 'false', '0'])
def test_default_and_single_construction(boundary, tmp_path, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', value)
    a, b = service(tmp_path), service(tmp_path)
    if value == '0':
        assert a.leg_reader is None and b.leg_reader is None
        return
    assert a.leg_reader.enabled
    assert a.leg_reader is b.leg_reader is a.runner.leg_reader
    assert a.hung.reader is a.leg_reader


@pytest.mark.parametrize('factory', [False, True])
def test_off_byte_identical(boundary, tmp_path, monkeypatch, factory):
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    if factory:
        from plangraph.spawner.runner import spawn_service
        s = spawn_service(runner=SimpleNamespace(), state=tmp_path)
    else:
        s = service(tmp_path)
    baseline = plan_spawn(CARD, role='implement', leg_reader=None)
    s.hung.waiting.add(baseline.legs[0].model)
    encode = lambda p: json.dumps(asdict(p), sort_keys=True)
    assert encode(s._route(CARD)) == encode(baseline)
    assert s.leg_reader is None


@pytest.mark.parametrize('reason', ['unknown', 'unreachable', 'stale'])
def test_dead_reader_declared_and_reported(boundary, tmp_path, reason):
    s = service(tmp_path)
    s.leg_reader.leg_status = lambda *a, **k: dict(state='limited', reason=reason)
    baseline = plan_spawn(CARD, role='implement', leg_reader=s.leg_reader.store)
    plan = s._route(CARD)
    assert plan.legs == baseline.legs and plan.legs
    assert any(c.get('leg_state_reason') == reason for c in plan.candidates)


def test_hung_stable_next_tick(boundary, tmp_path):
    s = service(tmp_path)
    s.leg_reader.leg_status = lambda *a, **k: dict(state='ok')
    first = s._route(CARD).legs
    assert len(first) > 2
    s.hung.waiting.add(first[0].id)
    expected = tuple(l for l in first if l.id != first[0].id) + tuple(
        l for l in first if l.id == first[0].id)
    assert s._route(CARD).legs == expected


def test_adapter_default_off(boundary, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail('default-off adapter must not read provider or discovery')

    store = leg_state.LegStateStore(tmp_path / 'state')
    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=unexpected),
                                  models=unexpected)
    assert reader.enabled is False
    assert reader('opencode-go/sample') == store.leg_status('opencode-go/sample')
    assert reader._at is None


def test_standalone_exact(monkeypatch):
    def run(argv, **kw):
        assert argv == ['opencode', 'models', '--standalone']
        assert kw == dict(capture_output=True, text=True, timeout=8, shell=False, check=True)
        return SimpleNamespace(stdout='sample\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == ['sample']


def test_cache_path_resolved(boundary, tmp_path, monkeypatch):
    a = service(tmp_path).leg_reader
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'different-state'))
    monkeypatch.setattr(leg_state, '_default_store', lambda: pytest.fail('second store'))
    assert service(tmp_path).leg_reader is a
    assert a._at is None


def test_service_fixture_never_constructs_live_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(leg_state, 'LegAdapter', lambda *a, **k: pytest.fail('live adapter'))
    from plangraph.routing_support import omniroute_state
    monkeypatch.setattr(omniroute_state, 'from_env', lambda: pytest.fail('live provider'))
    s = service(tmp_path)
    assert s.leg_reader is None  # disabled fixture is identical to main's None


@pytest.mark.parametrize('reason', ['unknown', 'unreachable', 'stale'])
def test_factory_preserves_adapter_and_hung_rank(boundary, tmp_path, reason):
    from plangraph.spawner.runner import spawn_service
    s = spawn_service(runner=SimpleNamespace(), state=tmp_path)
    reader = s.leg_reader
    reader.leg_status = lambda *a, **k: dict(state='limited', reason=reason, evidence={})
    baseline = plan_spawn(CARD, role='implement', leg_reader=reader.store)
    assert len(baseline.legs) > 2
    s.hung.waiting.add(baseline.legs[0].id)
    plan = s._route(CARD)
    assert plan.legs == baseline.legs[1:] + baseline.legs[:1]
    assert any(c.get('leg_state_reason') == reason for c in plan.candidates)
    assert s.leg_reader is reader is s.hung.reader is s.runner.leg_reader


def test_fixture_disabled_reader_matches_main_plan(tmp_path, monkeypatch):
    import plangraph.planner.routing as routing
    original = routing.plan_spawn
    seen = []

    def spy(*args, **kwargs):
        seen.append(kwargs['leg_reader'])
        return original(*args, **kwargs)

    monkeypatch.setattr(routing, 'plan_spawn', spy)
    s = service(tmp_path)
    baseline = original(CARD, role='implement', leg_reader=None)
    s.hung.waiting.add(baseline.legs[0].id)
    assert asdict(s._route(CARD)) == asdict(baseline)
    assert seen == [None]


def test_rank_subset_and_leg_keys(boundary, tmp_path):
    s = service(tmp_path)
    s.leg_reader.leg_status = lambda *a, **k: dict(state='ok')
    first = s._route(CARD).legs
    s.hung.rank = lambda keys: [first[-1].id]
    assert s._route(CARD).legs == (first[-1],) + first[:-1]
    s.hung.waiting.add(first[0].id)
    from plangraph.spawner.hung_capture import HungBoundary
    s.hung.rank = HungBoundary.rank.__get__(s.hung)
    assert s._route(CARD).legs == first[1:] + first[:1]


@pytest.mark.parametrize('reader_enabled', [None, True, False])
@pytest.mark.parametrize('factory', [False, True])
def test_off_injected_readers_identical_to_main(boundary, tmp_path, monkeypatch,
                                               reader_enabled, factory):
    monkeypatch.setenv('PLANGRAPH_OMNIROUTE_LEG_STATE', '0')
    reader = None if reader_enabled is None else SimpleNamespace(
        enabled=reader_enabled, leg_status=lambda *a, **k: dict(state='ok', reason='stale'))
    from plangraph.spawner.runner import spawn_service
    make = spawn_service if factory else SpawnService
    s = make(runner=SimpleNamespace(), state=tmp_path, leg_reader=reader)
    baseline = plan_spawn(CARD, role='implement', leg_reader=reader)
    s.hung.rank = lambda keys: list(reversed(keys))
    assert json.dumps(asdict(s._route(CARD)), sort_keys=True) == json.dumps(asdict(baseline), sort_keys=True)
    assert s.leg_reader is reader


@pytest.mark.parametrize('reason', ['unknown', 'unreachable', 'stale'])
def test_storeless_dead_reader_preserves_reported_status(boundary, tmp_path, reason):
    reader = SimpleNamespace(enabled=True, leg_status=lambda *a, **k: dict(state='ok', reason=reason))
    s = SpawnService(runner=SimpleNamespace(), state=tmp_path, leg_reader=reader)
    baseline = plan_spawn(CARD, role='implement', leg_reader=reader)
    plan = s._route(CARD)
    assert plan.legs == baseline.legs and plan.legs
    assert any(c.get('leg_state_reason') == reason for c in plan.candidates)


def test_empty_xdg_preserves_main_store_path(monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('XDG_STATE_HOME', '')
    monkeypatch.setattr(leg_state, 'LegStateStore', lambda path: path)
    assert leg_state._default_store() == Path('plangraph/leg-state.sqlite')


def test_production_hooks(boundary, tmp_path):
    import os
    from plangraph.spawner import proc_kill
    s = service(tmp_path)
    pid = os.getpid()
    ident = proc_kill._stat(pid)[4]
    assert s.hung.identity(pid, ident)
    assert not s.hung.identity(pid, ident + 1)
    evidence = s.hung.sample(dict(pid=pid, root_identity=ident, tokens=0, output_bytes=0))
    assert evidence['tokens'] == evidence['output_bytes'] == 0
    assert any(row['pid'] == pid for row in evidence['tree'])
