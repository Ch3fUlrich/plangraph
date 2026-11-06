"""Synthetic boundary regressions, not live acceptance."""
import socket
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, resolver, host_keys


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def deny(*a, **kw):
        pytest.fail('external access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(subprocess, 'run', deny)
    monkeypatch.setattr(host_keys, '_keys_path', deny)


def adapter(tmp_path, *, available=True, served=('opencode-go/sample',), enabled=True):
    calls = []
    provider = SimpleNamespace(snapshot=lambda: calls.append(1) or {
        'opencode-go': SimpleNamespace(available=available, stale=False)})
    store = leg_state.LegStateStore(tmp_path / 'state')
    reader = leg_state.LegAdapter(store, provider, lambda: served, enabled=enabled,
                                  clock=lambda: 0)
    return reader, calls


@pytest.mark.parametrize('available,expected', [(True, True), (False, False)])
def test_authority(tmp_path, available, expected):
    reader, calls = adapter(tmp_path, available=available)
    row = dict(id='opencode-go/sample', available=0, active=True)
    assert resolver.is_model_available(row, reader) is expected
    assert resolver.is_model_available(row, reader) is expected
    assert calls == [1]


def test_not_listed_and_failure(tmp_path):
    reader, _ = adapter(tmp_path, served=())
    assert reader('opencode-go/sample')['reason'] == 'not_listed'
    reader.models = lambda: (_ for _ in ()).throw(RuntimeError())
    reader._at = None
    assert reader('opencode-go/sample')['reason'] == 'unknown'


def test_off_no_reads(tmp_path):
    reader, calls = adapter(tmp_path, enabled=False)
    assert reader('opencode-go/sample') == dict(state='ok', evidence=None)
    assert calls == []


def test_removed_first(tmp_path):
    reader, calls = adapter(tmp_path)
    for mid in resolver.OPERATOR_REMOVED_MODELS:
        assert not resolver.is_model_available(dict(id=mid, available=1, active=True), reader)
    assert calls == []


def test_identity(tmp_path):
    reader, _ = adapter(tmp_path, served=('omniroute/cx-gpt-6.1-terra',))
    assert reader('omniroute/cx-gpt-6.1-sol')['reason'] == 'not_listed'
    assert reader.identity('agy/claude-sonnet-5-5-medium') == reader.identity('agy/claude-sonnet-5-5')


def test_disabled_policy_retained(tmp_path):
    from plangraph.planner.legs import DEFAULT_LEGS
    reader, _ = adapter(tmp_path)
    disabled = [leg for leg in DEFAULT_LEGS if leg.disabled]
    assert disabled
    for leg in disabled:
        assert not resolver.is_model_available(dict(id=leg.model, active=True,
                                                   disabled=leg.disabled), reader)


def test_stale_and_unreachable(tmp_path):
    reader, _ = adapter(tmp_path)
    reader.provider.snapshot = lambda: {'opencode-go': SimpleNamespace(available=True, stale=True)}
    assert reader('opencode-go/sample')['reason'] == 'stale'
    reader._at = None
    reader.provider.snapshot = lambda: {}
    assert reader('opencode-go/sample')['reason'] == 'unreachable'


def test_catalog_policy_is_not_mutated(tmp_path):
    import copy
    before = copy.deepcopy(resolver.DEFAULT_FALLBACK_MODELS)
    reader, calls = adapter(tmp_path, enabled=False)
    first = [m['id'] for m in before if resolver.is_model_available(m, reader)]
    second = [m['id'] for m in resolver.DEFAULT_FALLBACK_MODELS
              if resolver.is_model_available(m, reader)]
    assert first == second
    assert resolver.DEFAULT_FALLBACK_MODELS == before
    assert not calls


def test_bounded_discovery(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ['opencode', 'models']
        assert kwargs['timeout'] == 8 and kwargs['shell'] is False
        return SimpleNamespace(stdout='opencode-go/sample\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == ['opencode-go/sample']


def test_refresh_recovers_without_failure_becoming_health(tmp_path):
    reader, _ = adapter(tmp_path, available=False)
    assert reader('opencode-go/sample')['state'] == 'limited'
    reader.clock = lambda: 61
    reader.provider.snapshot = lambda: (_ for _ in ()).throw(RuntimeError())
    assert reader('opencode-go/sample')['state'] == 'limited'
    reader.clock = lambda: 122
    reader.provider.snapshot = lambda: {'opencode-go': SimpleNamespace(available=True, stale=False)}
    assert reader('opencode-go/sample')['state'] == 'ok'
