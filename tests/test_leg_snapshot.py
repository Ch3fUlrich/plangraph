"""Snapshot recovery must not override independent admission or retain frames."""
import asyncio
import builtins
import logging
from types import SimpleNamespace

import pytest

from plangraph.routing_support import leg_state, leg_state_flag
from plangraph.spawner import leg_probe


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
    leg = SimpleNamespace(model='snapshot/sample', account='account', combo='combo')
    reads = []

    def snapshot():
        reads.append(1)
        raise RuntimeError('refresh failed')

    store = leg_state.LegStateStore(tmp_path / 'leg-state.sqlite')
    reader = leg_state.LegAdapter(store, SimpleNamespace(snapshot=snapshot), models=(),
                                  enabled=True, clock=lambda: 0)
    return SimpleNamespace(leg=leg, reader=reader, store=store, reads=reads,
                           runner=SimpleNamespace(leg_reader=reader))


@pytest.mark.parametrize('outcome', ['ok', 'serving', 'failed', 'infra'])
@pytest.mark.parametrize('reason', ['limited', 'quota reached', 'not_listed', 'unavailable'])
@pytest.mark.parametrize('initial_error', [False, True])
def test_post_probe_admission_is_rechecked(case, monkeypatch, outcome, reason, initial_error):
    calls = []

    def admission(model, **scope):
        assert model == case.leg.model
        assert scope == dict(account='account', combo='combo')
        calls.append(reason)
        return dict(state='limited', reason='unknown' if len(calls) == 1 and initial_error else reason)

    async def probe(*args, **kwargs):
        calls.append('probe')
        return dict(ok=outcome == 'ok', reason='timeout', infra=outcome == 'infra',
                    state='probe_failed_leg_serving' if outcome == 'serving' else None)

    monkeypatch.setattr(case.reader, 'leg_status', admission)
    monkeypatch.setattr(leg_probe, 'probe', probe)
    role = 'review' if initial_error else 'implement'
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, role, False)) == reason
    assert calls == ([reason, 'probe', reason] if initial_error else ['probe', reason])


@pytest.mark.parametrize('outcome', ['ok', 'serving', 'failed', 'infra'])
def test_provider_limit_booked_during_probe_wins(case, monkeypatch, outcome):
    async def probe(*args, **kwargs):
        case.store.record_provider_response(case.leg.model, 429, 'rate limited',
                                            {'retry-after': '60'}, 'fixture',
                                            account='account', combo='combo')
        return dict(ok=outcome == 'ok', reason='timeout', infra=outcome == 'infra',
                    state='probe_failed_leg_serving' if outcome == 'serving' else None)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    reason = asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False))
    assert reason.startswith('limited until ')
    assert case.store.leg_status(case.leg.model, account='account', combo='combo')['state'] == 'limited'
    assert case.reads == [1, 1]


@pytest.mark.parametrize('role', ['implement', 'fix', 'review'])
def test_snapshot_reuses_success_until_ttl_expires(case, monkeypatch, role):
    now, probes = [1000.0], []
    monkeypatch.setattr(leg_probe.time, 'time', lambda: now[0])
    case.store.record_probe_pass(case.leg.model, 'fixture')

    async def probe(*args, **kwargs):
        probes.append(now[0])
        case.store.record_probe_pass(case.leg.model, 'fixture')
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    for instant in (1000.0, 2799.0, 2800.0, 2801.0):
        now[0] = instant
        assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, role, False)) is None
        assert probes == ([] if instant < 2800 else [2800.0])
    assert case.reads == [1, 1]


@pytest.mark.parametrize('reason', ['limited', 'quota reached', 'unavailable'])
def test_fresh_success_does_not_override_non_snapshot_admission(case, monkeypatch, reason):
    case.store.record_probe_pass(case.leg.model, 'fixture')
    monkeypatch.setattr(case.reader, 'leg_status', lambda *a, **k: dict(state='limited', reason=reason))

    async def probe(*args, **kwargs):
        pytest.fail('fresh evidence must not launch a probe')

    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'implement', False)) == reason


def test_snapshot_recovery_rechecks_changed_reason(case, monkeypatch):
    read, calls = case.reader.leg_status, []

    async def probe(*args, **kwargs):
        calls.append('probe')
        monkeypatch.setattr(case.reader, 'leg_status', lambda *a, **k: dict(state='ok', reason='ok'))
        return dict(ok=True)

    monkeypatch.setattr(leg_probe, 'probe', probe)
    assert read(case.leg.model)['reason'] == 'unknown'
    assert asyncio.run(leg_probe.before_launch(case.runner, case.leg, 'review', False)) is None
    assert calls == ['probe']


def test_snapshot_helper_import_failure_is_not_snapshot_evidence(case, monkeypatch):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == 'leg_state_flag':
            raise ImportError('snapshot helper unavailable')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', importing)
    with pytest.raises(ImportError, match='snapshot helper unavailable'):
        case.reader.leg_status(case.leg.model)
    assert case.reads == [] and case.reader._at is None


def test_snapshot_failure_cache_is_detached_and_refresh_clears_it(case, caplog):
    with caplog.at_level(logging.WARNING):
        assert case.reader.leg_status(case.leg.model)['reason'] == 'unknown'
        assert case.reader.leg_status(case.leg.model)['reason'] == 'unknown'
    assert case.reader._snapshot_error == ('RuntimeError', 'refresh failed')
    assert case.reads == [1, 1]
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == 'Leg state snapshot read failed (RuntimeError); retrying once'
    assert caplog.records[0].exc_info is None and 'refresh failed' not in caplog.text
    case.reader.clock = lambda: 60
    case.reader.models = [case.leg.model]
    case.reader.provider.snapshot = lambda: {'snapshot': SimpleNamespace(available=True, stale=False)}
    assert case.reader.leg_status(case.leg.model)['state'] == 'ok'
    assert case.reader._snapshot_error is None


def test_successful_retry_clears_a_previous_failure(case, caplog):
    assert case.reader.leg_status(case.leg.model)['reason'] == 'unknown'
    case.reader.clock = lambda: 60
    reads = []

    def snapshot():
        reads.append(1)
        if len(reads) == 1:
            raise ValueError('retry failure')
        return {'snapshot': SimpleNamespace(available=True, stale=False)}

    case.reader.provider.snapshot = snapshot
    case.reader.models = [case.leg.model]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert case.reader.leg_status(case.leg.model)['state'] == 'ok'
    assert case.reader._snapshot_error is None and reads == [1, 1]
    assert len(caplog.records) == 1 and '(ValueError)' in caplog.text
    assert 'retry failure' not in caplog.text


def test_discovery_failure_retries_the_entire_refresh(case, caplog):
    snapshots, discoveries = [], []

    def snapshot():
        snapshots.append(1)
        return {'snapshot': SimpleNamespace(available=True, stale=False)}

    def models():
        discoveries.append(1)
        if len(discoveries) == 1:
            raise RuntimeError('discovery failure')
        return [case.leg.model]

    case.reader.provider.snapshot, case.reader.models = snapshot, models
    with caplog.at_level(logging.WARNING):
        assert case.reader.leg_status(case.leg.model)['state'] == 'ok'
        assert case.reader.leg_status(case.leg.model)['state'] == 'ok'
    assert snapshots == discoveries == [1, 1]
    assert case.reader._snapshot_error is None
    assert len(caplog.records) == 1 and '(RuntimeError)' in caplog.text


def test_snapshot_retry_keeps_final_error_and_previous_snapshot(case, caplog):
    previous = {'snapshot': SimpleNamespace(available=True, stale=False)}
    case.reader.provider.snapshot = lambda: previous
    case.reader.models = [case.leg.model]
    assert case.reader.leg_status(case.leg.model)['state'] == 'ok'
    calls = []

    def snapshot():
        calls.append(1)
        raise (PermissionError('first failure') if len(calls) == 1 else ValueError('second failure'))

    case.reader.provider.snapshot = snapshot
    case.reader.clock = lambda: 60
    with caplog.at_level(logging.WARNING):
        assert case.reader.leg_status(case.leg.model)['reason'] == 'unknown'
    assert calls == [1, 1] and case.reader._states is previous
    assert case.reader._snapshot_error == ('ValueError', 'second failure')
    assert len(caplog.records) == 1 and '(PermissionError)' in caplog.text
    assert 'first failure' not in caplog.text and 'second failure' not in caplog.text


def test_snapshot_scrubs_before_splitting_secret_paths():
    secret = '/'.join(('synthetic-private', 'credential'))
    reader = SimpleNamespace(_snapshot_error=('PermissionError', 'locked: ' + secret))
    calls = []

    def scrub(text):
        calls.append(text)
        return text.replace(secret, '[REDACTED]')

    assert leg_state_flag.snapshot_detail(reader, scrub) == 'PermissionError: locked: [REDACTED]'
    assert calls == ['PermissionError: locked: ' + secret]


@pytest.mark.parametrize('prefix', ['/', 'C:\\', '~/'])
def test_snapshot_path_whitespace_suffix_is_withheld(prefix):
    private = prefix + 'fixture/private snapshot opaque-value'
    reader = SimpleNamespace(_snapshot_error=('PermissionError', f'locked: "{private}"; retry'))
    assert leg_state_flag.snapshot_detail(reader, lambda text: text) == 'PermissionError: locked: "[path]"; retry'
