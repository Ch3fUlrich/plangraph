"""Synthetic boundary regressions, not live acceptance."""
import socket
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, resolver, host_keys


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    from plangraph.spawner import leg_probe
    def deny(*a, **kw):
        pytest.fail('external access forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(subprocess, 'run', deny)
    monkeypatch.setattr(host_keys, '_keys_path', deny)
    monkeypatch.setattr(leg_probe, '_INFLIGHT', {})
    yield
    assert leg_probe._INFLIGHT == {}


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
    reader.models = lambda: (_ for _ in ()).throw(RuntimeError('discovery read failed'))
    reader._at = None
    assert reader('opencode-go/sample')['reason'] == 'leg_state_snapshot_error: RuntimeError: discovery read failed'


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
    expected[expected.index('opencode-go/glm-5.2'):expected.index('opencode-go/glm-5.2')] = [
        'claude-opus-4-6-thinking', 'claude-opus-5-5-high', 'omniroute/deepseek-flash',
        'omniroute/muse-spark-1.3-contributor', 'claude-opus-5-5-medium', 'opencode/gemini-4-argon']
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
        assert calls == [1]  # admission checks the unavailable snapshot before probing
        probes.append(1)
        reader.provider.snapshot = lambda: calls.append(1) or {'opencode-go': SimpleNamespace(available=True, stale=False)}
        return dict(ok=True)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', recover)
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    assert probes == [1] and calls == [1, 1]


@pytest.mark.parametrize('initial_read_error', [False, True])
def test_probe_success_admits_after_snapshot_reread_error(tmp_path, monkeypatch, caplog,
                                                         initial_read_error):
    import asyncio
    from plangraph.spawner import leg_probe
    reader, _ = adapter(tmp_path, available=False)
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')
    runner = SimpleNamespace(leg_reader=reader)
    events = []

    def broken_snapshot():
        events.append('read error')
        raise OSError('snapshot unavailable')

    def initial_snapshot():
        events.append('read')
        return {'opencode-go': SimpleNamespace(available=False, stale=False)}

    reader.provider.snapshot = broken_snapshot if initial_read_error else initial_snapshot

    async def probe(actual_runner, actual_leg, **kwargs):
        assert actual_runner is runner and actual_leg is leg
        assert kwargs['store'] is reader.store
        events.append('probe')
        reader.store.record_probe_pass(leg.model, 'test')
        reader.provider.snapshot = broken_snapshot
        return dict(ok=True)

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    assert events == (['read error', 'read error', 'probe'] if initial_read_error else
                      ['read', 'probe', 'read error', 'read error'])
    assert reader.store.last_probe_ts(leg.model) is not None
    assert 'leg_state_snapshot_error: OSError: snapshot unavailable' in caplog.text


@pytest.mark.parametrize('error', [OSError('snapshot missing'), ValueError('partial snapshot'),
                                  RuntimeError('refresh rejected')])
def test_snapshot_refresh_keeps_error(tmp_path, error):
    reader, _ = adapter(tmp_path)
    assert reader('opencode-go/sample')['state'] == 'ok'
    previous = reader._states

    def snapshot():
        raise error

    reader.provider.snapshot = snapshot
    reader.clock = lambda: 61
    assert reader('opencode-go/sample') == dict(
        state='limited', evidence=None,
        reason=f'leg_state_snapshot_error: {type(error).__name__}: {error}')
    assert reader._states is previous  # stale health cannot mask a failed read


@pytest.fixture
def snapshot_failure(tmp_path):
    reader, _ = adapter(tmp_path)
    events = []

    def snapshot():
        events.append('read')
        raise OSError('snapshot unavailable')

    reader.provider.snapshot = snapshot
    leg = SimpleNamespace(model='opencode-go/sample', harness='opencode')
    return SimpleNamespace(reader=reader, runner=SimpleNamespace(leg_reader=reader),
                           leg=leg, events=events)


@pytest.mark.parametrize('role,ttl,fresh', [('review', '0', False),
    ('implement', '0', False), ('fix', '1800', False), ('implement', '1800', True)])
@pytest.mark.parametrize('probe_ok,probe_reason,infra', [(False, 'timeout', False),
    (True, '', False), (False, 'probe infra: git', True)])
def test_snapshot_error_retries_then_probes(snapshot_failure, monkeypatch, role, ttl, fresh,
                                           probe_ok, probe_reason, infra):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure
    if fresh:
        case.reader.store.record_probe_pass(case.leg.model, 'test', now=100)
        monkeypatch.setattr(leg_probe.time, 'time', lambda: 101)

    async def probe(runner, leg, **kwargs):
        assert runner is case.runner and leg is case.leg
        assert kwargs['store'] is case.reader.store
        assert case.events == ['read', 'read']  # retry precedes any probe/verdict
        case.events.append('probe')
        return dict(ok=probe_ok, reason=probe_reason, infra=infra)

    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', ttl)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    reason = asyncio.run(leg_probe.before_launch(case.runner, case.leg, role, False))
    assert case.events == ['read', 'read', 'probe']
    assert reason == (None if probe_ok else
        'leg_state_snapshot_error: OSError: snapshot unavailable; probe: ' + probe_reason)
    assert case.reader.store.last_probe_ts(case.leg.model) == (100 if fresh else None)
    # The same role/TTL/freshness inputs still control ordinary healthy admission.
    case.reader._at = None
    case.reader.provider.snapshot = lambda: {
        'opencode-go': SimpleNamespace(available=True, stale=False)}
    ordinary = []

    async def healthy_probe(*args, **kwargs):
        ordinary.append('probe')
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', healthy_probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, role, False)) is None
    assert ordinary == (['probe'] if role in ('implement', 'fix') and ttl != '0' and not fresh else [])


def test_snapshot_retry_recovers_without_probe(snapshot_failure, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure
    failed_read = case.reader.provider.snapshot

    def snapshot():
        if not case.events:
            return failed_read()
        case.events.append('read')
        return {'opencode-go': SimpleNamespace(available=True, stale=False)}

    async def probe(*args, **kwargs):
        pytest.fail('a recovered review snapshot needs no probe')

    monkeypatch.setattr(case.reader.provider, 'snapshot', snapshot)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False)) is None
    assert case.events == ['read', 'read']


def test_snapshot_probe_success_cannot_clear_new_provider_limit(snapshot_failure, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure
    monkeypatch.setattr(leg_probe.time, 'time', lambda: 100)

    async def probe(*args, **kwargs):
        assert case.events == ['read', 'read']
        case.events.append('probe')
        case.reader.store.record_harness_limit(case.leg.model, 'error: quota reached',
                                                'test', now=100, down_s=60)
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False)) == \
        'limited until 1970-01-01T00:02:40+00:00'
    assert case.events == ['read', 'read', 'probe']


def test_snapshot_probe_sharing_is_loop_hermetic(snapshot_failure, monkeypatch):
    import asyncio
    from plangraph.spawner import leg_probe
    case = snapshot_failure

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def probe(*args, **kwargs):
            case.events.append('probe')
            started.set()
            await release.wait()
            return dict(ok=True)

        monkeypatch.setattr(leg_probe, 'probe', probe)
        launches = [asyncio.create_task(leg_probe.before_launch(
            case.runner, case.leg, 'review', False)) for _ in range(3)]
        await asyncio.wait_for(started.wait(), 1)
        assert len(leg_probe._INFLIGHT) == 1
        release.set()
        assert await asyncio.gather(*launches) == [None] * 3
        assert leg_probe._INFLIGHT == {}

    for _ in range(2):  # a new event loop cannot inherit a previous loop's task
        case.reader._at = None
        case.events.clear()
        asyncio.run(run())
        assert case.events.count('probe') == 1
