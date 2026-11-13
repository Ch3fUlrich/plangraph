"""Synthetic boundary regressions, not live acceptance."""
import socket
import subprocess
import asyncio
import json
import sqlite3
from pathlib import Path
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
    reader.provider.snapshot = lambda: {'other-provider': SimpleNamespace(available=True, stale=False)}
    assert reader('opencode-go/sample')['reason'] == 'unreachable'
    reader._at = None
    reader.provider.snapshot = lambda: {}
    assert reader('opencode-go/sample')['reason'] == 'unknown'
    assert reader._snapshot_error == ('ValueError', 'empty provider snapshot')


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
        probes.append(1)
        reader.provider.snapshot = lambda: calls.append(1) or {'opencode-go': SimpleNamespace(available=True, stale=False)}
        return dict(ok=True)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    monkeypatch.setattr(leg_probe, 'probe', recover)
    assert asyncio.run(leg_probe.before_launch(runner, leg, 'implement', False)) is None
    assert probes == calls == [1]


@pytest.fixture
def snapshot_attempt(tmp_path, monkeypatch):
    """Synthetic reported seat-2 candidate order, not recorded attempts or errors."""
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner import leg_probe
    from plangraph.spawner.runner import spawn_service
    from plangraph.spawner.service import SpawnService
    from registry_route_util import _Runner
    legs = tuple(next(l for l in DEFAULT_LEGS if l.id == name) for name in
                 ('agy-gemini-3.8-flash', 'agy-sonnet-5.5', 'agy-opus-5.5'))
    reads, probes = [], []
    error = RuntimeError('synthetic snapshot refresh failure')

    def snapshot():
        reads.append(1)
        raise error

    store = leg_state.LegStateStore(tmp_path / 'leg-state.sqlite')
    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=snapshot), models=(), enabled=True)
    runner = _Runner()
    svc = spawn_service(runner, state=tmp_path / 'state', leg_reader=reader)
    monkeypatch.setattr(SpawnService, '_leg_prompt', lambda *a, **k: 'review')
    monkeypatch.setattr('plangraph.spawner.notify.completion_event', lambda *a: {})

    return SimpleNamespace(svc=svc, reader=reader, store=store, legs=legs, reads=reads, probes=probes)


@pytest.mark.parametrize('error', [FileNotFoundError('snapshot missing'),
                                  sqlite3.OperationalError('database is locked'),
                                  json.JSONDecodeError('partial snapshot', '', 0),
                                  RuntimeError('synthetic snapshot refresh failure')])
@pytest.mark.parametrize('probe_ok,probe_state,infra', [(False, None, False), (True, None, False),
    (False, 'probe_failed_leg_serving', False), (False, 'host-not-ready', True), (False, None, True)])
@pytest.mark.parametrize('role,ttl', [('review', '0'), ('implement', '0'), ('implement', '1800')])
@pytest.mark.parametrize('declared_fallback', [False, True])
def test_snapshot_error_retries_then_probes(snapshot_attempt, monkeypatch, error, probe_ok, probe_state, infra, role, ttl, declared_fallback):
    from plangraph.spawner import leg_probe
    case = snapshot_attempt
    assert case.reader.store is case.store
    real_probe = leg_probe.probe

    if infra and probe_state is None:
        def broken_probe_git(*a, **k):
            raise OSError('synthetic probe git infrastructure failure')
        monkeypatch.setattr(subprocess, 'check_output', broken_probe_git)

    def snapshot():
        case.reads.append(1)
        raise error

    async def probe(*a, **k):
        assert k['store'] is case.store
        case.probes.append(case.legs[0].id)
        if infra and probe_state is None:
            result = await real_probe(*a, **k)
            assert result['ok'] is False and result['infra'] is True
            assert result.get('state') is None and result['reason'] == 'probe infra: git'
            return result
        return dict(ok=probe_ok, reason='timeout', state=probe_state, infra=infra)

    monkeypatch.setattr(case.reader.provider, 'snapshot', snapshot)
    if declared_fallback:  # default_reader keeps unknown even when local state says ok.
        read = case.reader.leg_status
        monkeypatch.setattr(case.reader, 'leg_status', lambda *a, **k: dict(read(*a, **k), state='ok'))
    monkeypatch.setattr(leg_probe, 'probe', probe)
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', ttl)
    reason = asyncio.run(leg_probe.before_launch(case.svc.runner, case.legs[0], role, False))
    assert case.probes == [case.legs[0].id]
    assert len(case.reads) == 2
    if probe_ok or probe_state == 'probe_failed_leg_serving':
        assert reason is None
    else:
        assert f'leg_state_snapshot_error: {type(error).__name__}: {error}' in reason
        assert ('probe infra: git' if infra and probe_state is None else 'timeout') in reason
    if infra:
        assert case.store.last_probe_attempt_ts(leg_probe.key(case.legs[0])) is None
        assert case.store.leg_status(leg_probe.key(case.legs[0])) == dict(state='ok', evidence=None)
        with case.store._connect() as db:
            assert db.execute('SELECT source, status FROM leg_events').fetchall() == []


def test_snapshot_retry_recovers_without_probe(snapshot_attempt, monkeypatch):
    from plangraph.spawner import leg_probe
    case = snapshot_attempt

    def snapshot():
        case.reads.append(1)
        if len(case.reads) == 1:
            raise RuntimeError('concurrent refresh')
        return {'agy': SimpleNamespace(available=True, stale=False)}

    case.reader.models = [leg_probe.key(case.legs[0])]
    monkeypatch.setattr(case.reader.provider, 'snapshot', snapshot)
    async def unexpected_probe(*a, **k):
        pytest.fail('a recovered snapshot needs no review probe')
    monkeypatch.setattr(leg_probe, 'probe', unexpected_probe)
    assert asyncio.run(leg_probe.before_launch(case.svc.runner, case.legs[0], 'review', False)) is None
    assert len(case.reads) == 2 and not case.probes


def test_snapshot_failed_probe_reason_scrubs_paths_and_secrets(snapshot_attempt, monkeypatch):
    from plangraph.spawner import leg_probe
    case = snapshot_attempt
    secret = '-'.join(('synthetic-snapshot', 'credential'))
    bearer = '-'.join(('synthetic-bearer', 'credential'))
    home_path = str(Path.home() / 'private snapshot' / 'snapshot.json')
    spaced_path = '/'.join(('', 'fixture', 'private snapshot', 'snapshot.json'))
    monkeypatch.setenv('SNAPSHOT_SECRET', secret)

    def snapshot():
        raise PermissionError(f'snapshot locked: {secret}; Bearer {bearer} ; {home_path}; "{spaced_path}"')

    async def probe(*a, **k):
        return dict(ok=False, reason='timeout')

    monkeypatch.setattr(case.reader.provider, 'snapshot', snapshot)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    reason = asyncio.run(leg_probe.before_launch(case.svc.runner, case.legs[0], 'review', False))
    assert 'PermissionError: snapshot locked:' in reason
    assert secret not in reason and home_path not in reason
    assert bearer not in reason and spaced_path not in reason
    assert 'private snapshot' not in reason and 'snapshot.json' not in reason
    assert '[autoos:redacted]' in reason and 'Bearer [REDACTED]' in reason
    assert reason.endswith('Bearer [REDACTED] ; [home][path]; "[path]"; probe: timeout')


@pytest.mark.parametrize('serving_index', [0, 1])
def test_snapshot_seat_uses_witnessed_non_writer_before_opus(snapshot_attempt, monkeypatch, serving_index):
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner import leg_probe
    case = snapshot_attempt
    assert [case.reader.leg_status(leg_probe.key(leg)).get('reason', 'ok') for leg in case.legs] == [
        'unknown', 'unknown', 'unknown']
    serving = case.legs[serving_index]
    spawn = case.svc.runner.spawn

    async def witnessed(**kwargs):
        job = await spawn(**kwargs)
        job.exit_code = 0
        job.stdout, job.response_finish_reason = 'VERDICT: PASS\nTEST_GAMING: no\n', 'stop'
        if kwargs['model_or_combo'] != case.legs[2].model:
            job.served_model, job.served_family = kwargs['model_or_combo'], serving.family
            job.served_witness_reason = 'response-side fixture'
        return job

    async def probe(_runner, leg, **kwargs):
        case.probes.append(leg.id)
        return dict(ok=leg.id == serving.id, reason='timeout')

    monkeypatch.setattr(case.svc.runner, 'spawn', witnessed)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    plan = SpawnPlan(case.legs, (case.legs[0],), 'fixture', (), (), seat_coverage=(
        dict(seat_group='seats-fixture', seat_index=2, eligible_families=['google', 'anthropic']),))
    run = asyncio.run(case.svc.spawn(card={'estimate_tokens': 1000}, role='review',
                                   writer_families=['gpt'], _plan=plan, wait_seconds=None))
    assert run['status'] == 'completed', run.get('error')
    assert run['chosen_leg']['id'] == serving.id
    assert run['served_model'] == serving.model and run['served_family'] != 'gpt'
    assert [a['leg']['id'] for a in run['attempts'] if a.get('leg') and
            a.get('skip_reason') != 'earlier_leg_succeeded'] == [leg.id for leg in case.legs[:serving_index + 1]]
    assert case.probes == [leg.id for leg in case.legs[:serving_index + 1]]
    assert all(c['model_or_combo'] != case.legs[2].model for c in case.svc.runner.calls)
    saved = case.svc.result(run['run_id'])
    assert saved['chosen_leg'] == run['chosen_leg']
    if serving_index:
        skipped = run['attempts'][0]
        assert skipped['status'] == 'skipped'
        assert 'RuntimeError: synthetic snapshot refresh failure' in skipped['skip_reason']
