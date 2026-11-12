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


# Recorded from origin/main with an empty local evidence store.
MAIN_ORDER = ['opencode-go/gemini-4-argon', 'mimo/mimo-v2.6-pro', 'opencode-go/qwen3.8-max', 'opencode-go/muse-spark-1.3-contributor', 'opencode-go/deepseek-v4.1-flash', 'Qwen3.8-Flash', 'opencode/deepseek-v4.1-flash-free', 'opencode/glm-5.3-flash-free', 'openrouter/free', 'omniroute/cx-gpt-6.1-sol', 'omniroute/cx-gpt-6-luna', 'gemini-3.8-flash', 'mimo/mimo-v2.6-flash', 'claude-sonnet-5-5-medium', 'opencode-go/glm-5.2', 'omniroute/deepseek-direct-flash', 'claude-haiku-5-5']


@pytest.mark.parametrize('available,expected', [(True, True), (False, False)])
def test_authority(tmp_path, available, expected):
    reader, calls = adapter(tmp_path, available=available)
    row = dict(id='opencode-go/sample', available=0, active=True, expires='2000-01-01')
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


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('state', ['ok', 'limited', 'down', 'off', 'reprobe'])
def test_legacy_availability_preserves_state_policy(enabled, state):
    def reader(model, **scope):
        assert model == 'opencode-go/sample'
        assert scope == dict(account='sample', combo='worker')
        return dict(state=state, evidence=None)

    reader.enabled = enabled
    row = dict(id='opencode-go/sample', available=1, active=True)
    expected = state == 'ok' if enabled else state != 'limited'
    assert resolver.is_model_available(row, reader, account='sample', combo='worker') is expected


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
    # New review seats extend the catalog, never reorder the existing entries.
    expected = MAIN_ORDER.copy()
    expected[expected.index('omniroute/cx-gpt-6.1-sol')] = 'omniroute/cx-gpt-6.1-sol-xhigh'
    expected.insert(expected.index('mimo/mimo-v2.6-flash'), 'gemini-4-argon-high')
    expected.insert(expected.index('mimo/mimo-v2.6-flash'), 'opencode/big-pickle')
    expected.insert(expected.index('mimo/mimo-v2.6-flash'), 'gpt-oss-120b-medium')
    for model in ('claude-opus-4-6-thinking', 'omniroute/deepseek-flash',
                  'omniroute/muse-spark-1.3-contributor'):
        expected.insert(expected.index('opencode-go/glm-5.2'), model)
    expected.insert(expected.index('omniroute/deepseek-direct-flash'), 'omniroute/gc/grok-4.7')
    expected.insert(expected.index('omniroute/deepseek-direct-flash'),
                    'console-meta/muse-spark-1.3-contributor')
    expected.insert(expected.index('omniroute/deepseek-direct-flash'), 'omniroute/gemini-3.8-flash')
    assert first == second == expected
    assert resolver.DEFAULT_FALLBACK_MODELS == before
    assert not calls


def test_bounded_discovery(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ['opencode', 'models', '--standalone']
        assert kwargs['timeout'] == 8 and kwargs['shell'] is False
        return SimpleNamespace(stdout='opencode-go/sample\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == ['opencode-go/sample']


def test_default_discovery_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(stdout='opencode-go/sample\n'))
    reader, _ = adapter(tmp_path)
    reader = leg_state.LegAdapter(reader.store, reader.provider, enabled=True)
    assert reader.models() == ['opencode-go/sample']
    assert reader('opencode-go/sample')['state'] == 'ok'


@pytest.mark.parametrize('error', [subprocess.CalledProcessError(1, 'opencode'),
                                  subprocess.TimeoutExpired('opencode', 8), OSError()])
def test_bounded_discovery_failure(monkeypatch, error):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, 'run', run)
    assert leg_state.LegAdapter.standalone_models() == []


def test_refresh_recovers_without_failure_becoming_health(tmp_path):
    reader, _ = adapter(tmp_path, available=False)
    assert reader('opencode-go/sample')['state'] == 'limited'
    reader.clock = lambda: 61
    reader.provider.snapshot = lambda: (_ for _ in ()).throw(RuntimeError())
    assert reader('opencode-go/sample')['state'] == 'limited'
    reader.clock = lambda: 122
    reader.provider.snapshot = lambda: {'opencode-go': SimpleNamespace(available=True, stale=False)}
    assert reader('opencode-go/sample')['state'] == 'ok'


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('policy', [dict(id='cxa/gpt-6-sol-x'), dict(status='retired')])
def test_shared_admission(tmp_path, enabled, policy):
    reader, _ = adapter(tmp_path, enabled=enabled, served=('cxa/gpt-6-sol-x', 'opencode-go/sample'))
    reader.provider.snapshot = lambda: {'cxa': SimpleNamespace(available=True, stale=False),
                                      'opencode-go': SimpleNamespace(available=True, stale=False)}
    assert not resolver.is_model_available(dict(id='opencode-go/sample', active=True,
                                                available=1) | policy, reader)


def test_enabled_probe_recovery(tmp_path, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    reader, calls = adapter(tmp_path, available=False)
    leg = SimpleNamespace(model='opencode-go/sample', account='sample', combo='worker')
    runner = SimpleNamespace(leg_reader=reader)
    probes = []
    async def recover(*args, **kwargs):
        probes.append(1)
        reader.provider.snapshot = lambda: calls.append(1) or {'opencode-go': SimpleNamespace(available=True, stale=False)}
        return dict(ok=True)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', recover)
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    assert probes == calls == [1]
