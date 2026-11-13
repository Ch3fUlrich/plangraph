"""Quota refusals (omniroute, agy, opencode-go) book the leg down and end the run at once."""
import asyncio
import sys
import time
from types import SimpleNamespace

import pytest
from test_run_watchdog import _job

from plangraph.routing_support import leg_state, leg_state_flag as flag, omniroute_state, quota_snapshots
from plangraph.routing_support.leg_state import LegStateStore, RESET_CAP_S
from plangraph.routing_support.quota_snapshots import QuotaGate, fetch_snapshot_rows, pool_of
from plangraph.spawner.env_redact import early_refusal, harness_limit_line
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.spawner.gateway_witness import poll_quota, quota_refusal
from plangraph.spawner.proc_kill import root_identity
from plangraph.spawner.run_watchdog import collect

_production_reader = flag.default_reader

POLICY = 'quota policy filtered 1 account(s): codex-main (weekly usage 99%)'
THRESHOLD = 'All codex accounts reached configured quota threshold (reset after 141h 52m)'
AGY = 'RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 21h2m13s'
TEXTS = [(POLICY, None), (THRESHOLD, 141 * 3600 + 52 * 60), (AGY, 21 * 3600 + 2 * 60 + 13)]


@pytest.mark.parametrize('prefix', ['', 'Error: '])
@pytest.mark.parametrize('text,_', TEXTS)
def test_each_refusal_text_is_detected(text, _, prefix):
    assert harness_limit_line(1, '', prefix + text) == prefix + text
    assert early_refusal(prefix + text + '\n') == prefix + text


def test_prose_mentioning_a_quota_is_not_a_refusal():
    assert harness_limit_line(1, '', 'note: the quota policy filtered nothing') is None


@pytest.mark.parametrize('text,seconds', TEXTS)
def test_leg_is_down_until_the_stated_reset_and_route_skips_it(tmp_path, text, seconds):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit('omniroute/gpt-6.1-sol', text, 'spawner:harness', now=1000.0, down_s=1800)
    want = 1000.0 + (1800 if seconds is None else min(seconds, RESET_CAP_S))
    status = store.leg_status('omniroute/gpt-6.1-sol', now=1001.0)
    assert status['state'] == 'limited' and status['evidence']['reset_at'] == want
    assert store.leg_status('omniroute/gpt-6.1-sol', now=want + 1)['state'] != 'limited'
    from plangraph.planner.routing import plan_spawn
    card = {'estimate_tokens': 1000}
    first = plan_spawn(card, role='implement', leg_reader=store.leg_status, now=1001.0).legs[0]
    store.record_harness_limit(first.model, text, 'spawner:harness', now=1000.0, down_s=1800)
    during = plan_spawn(card, role='implement', leg_reader=store.leg_status, now=1001.0)
    assert first.id not in [leg.id for leg in during.legs]
    after = plan_spawn(card, role='implement', leg_reader=store.leg_status, now=want + 1)
    assert after.legs[0].id == first.id


ROWS = [{'sessionTag': 'plangraph-j', 'status': 429, 'error_summary': THRESHOLD},
        {'sessionTag': 'other', 'status': 429, 'error_summary': AGY}]


def test_call_log_error_summary_is_quota_evidence():
    assert quota_refusal(ROWS, 'plangraph-j') == THRESHOLD
    assert quota_refusal([{'sessionTag': 'plangraph-j', 'status': 200, 'error_summary': THRESHOLD}], 'plangraph-j') is None
    assert quota_refusal([{'sessionTag': 'plangraph-j', 'status': 500, 'error_summary': 'boom'}], 'plangraph-j') is None
    assert quota_refusal([{'sessionTag': 'plangraph-j', 'status': 429, 'error_summary': 'busy'}], 'plangraph-j') == 'HTTP 429: busy'
    job = type('J', (), {'model_or_combo': 'omniroute/gpt-6.1-sol'})()
    assert poll_quota('j', job, {}, fetch=lambda tag, env: ROWS) == THRESHOLD
    assert poll_quota('j', type('J', (), {'model_or_combo': 'agy/x'})(), {}, fetch=lambda t, e: ROWS) is None


def test_silent_gateway_leg_ends_on_logged_quota_refusal_without_waiting_for_stall(tmp_path, monkeypatch):
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_IDLE_S', 0)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_EVERY_S', .05)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.poll_quota', lambda *a, **k: THRESHOLD)
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        job.model_or_combo = 'omniroute/gpt-6.1-sol'
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert job.status == 'failed' and job.exit_code == 7 and job.refusal == THRESHOLD
    asyncio.run(run())


SOL = 'omniroute/cx-gpt-6.1-sol-xhigh'
OK = {'state': 'ok', 'evidence': None}


def _row(pct, reset, account='a1', window='7d', provider='codex', **extra):
    return dict(provider=provider, connection_id=account, window_key=window, remaining_percentage=pct,
                is_exhausted=int(pct <= 0), next_reset_at=reset, created_at='2026-11-13 10:00:00', **extra)


def test_pool_at_zero_percent_is_skipped_until_its_reset_and_routes_again_after(tmp_path):
    from plangraph.planner.routing import plan_spawn
    now = time.time()
    reset = now + 3600
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(reset))
    gate = QuotaGate(lambda: [_row(0, stamp), _row(40, None, window='5h')], clock=lambda: now)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    reader = lambda model, **scope: gate.apply(model, store.leg_status(model, **scope), scope.get('now'))
    card = {'estimate_tokens': 1000}
    assert plan_spawn(card, role='implement', leg_reader=store.leg_status, now=now).legs[0].model == SOL
    during = plan_spawn(card, role='implement', leg_reader=reader, now=now + 60)
    assert SOL not in [leg.model for leg in during.legs]
    assert any(s['leg'] == 'cx-gpt-6.1-sol-xhigh' and s['reason'] == 'limited' for s in during.skipped)
    after = plan_spawn(card, role='implement', leg_reader=reader, now=reset + 1)
    assert after.legs[0].model == SOL


def test_pool_stays_up_while_any_account_can_serve_and_other_pools_are_untouched():
    now = time.time()
    future = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 600))
    gate = QuotaGate(lambda: [_row(0, future, account='a1'), _row(55, None, account='a2'), _row(0, future, provider='claude')])
    assert gate.apply(SOL, OK, now) == OK
    assert gate.apply('omniroute/cx-gpt-6.1-sol', OK, now) == OK
    assert gate.apply('openrouter/free', OK, now) == OK
    assert gate.apply('omniroute/cc-claude-x', OK, now)['state'] == 'limited'
    assert pool_of('omniroute/gc/grok-4.7') == 'grok-cli' and pool_of('omniroute/muse-spark-1.3') is None


def _boom(exc):
    def rows():
        raise exc
    return rows


@pytest.mark.parametrize('source', [_boom(LookupError('x')), _boom(RuntimeError('x')), _boom(OSError('x')),
                                    lambda: None, lambda: 'junk', lambda: [None, 7, {'provider': 'codex'}], lambda: []])
def test_missing_or_unreadable_snapshot_never_blocks_routing(source):
    assert QuotaGate(source).apply(SOL, OK, time.time()) == OK


def test_snapshot_fetch_is_read_only_json_and_unreadable_is_a_lookup_error(monkeypatch):
    monkeypatch.setattr(quota_snapshots, 'effective_gateway_base_url', lambda env: 'http://gw/v1')
    monkeypatch.setattr(quota_snapshots, '_gateway_key', lambda env: 'k')
    seen = []
    def fetch(url, headers, timeout):
        seen.append((url, headers))
        return 200, b'{"data": [{"provider": "codex:a1", "remainingPct": 0}]}'
    assert fetch_snapshot_rows({}, fetch=fetch) == [{'provider': 'codex:a1', 'remainingPct': 0}]
    assert seen[0][0].startswith('http://gw/api/usage/utilization?') and seen[0][1] == {'Authorization': 'Bearer k'}
    for bad in (lambda u, h, t: (500, b'{}'), lambda u, h, t: (200, b'nope'), lambda u, h, t: (_ for _ in ()).throw(OSError())):
        with pytest.raises(LookupError):
            fetch_snapshot_rows({}, fetch=bad)
    monkeypatch.setattr(quota_snapshots, '_gateway_key', lambda env: None)
    with pytest.raises(LookupError):
        fetch_snapshot_rows({}, fetch=fetch)


def test_production_reader_applies_the_snapshot_gate(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(flag, 'default_reader', _production_reader)
    flag.default_reader.cache_clear()
    monkeypatch.setattr(leg_state, '_default_store', lambda: LegStateStore(tmp_path / 'state'))
    monkeypatch.setattr(omniroute_state, 'from_env', lambda: SimpleNamespace(snapshot=lambda: {}))
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: []))
    monkeypatch.setattr(quota_snapshots, 'fetch_snapshot_rows', lambda: [_row(0, '2099-01-01T00:00:00Z')])
    try:
        reader = flag.default_reader()
        status = reader.leg_status(SOL, now=now)
        assert status['state'] == 'limited' and status['reason'] == 'quota_snapshot'
        assert reader.leg_status('openrouter/free', now=now)['state'] == 'ok'
        monkeypatch.setattr(quota_snapshots, 'fetch_snapshot_rows', _boom(LookupError('down')))
        flag.default_reader.cache_clear()
        assert flag.default_reader().leg_status(SOL, now=now)['state'] == 'ok'
    finally:
        flag.default_reader.cache_clear()


def test_a_failing_call_log_check_does_not_fail_the_run(tmp_path, monkeypatch):
    from plangraph.spawner.gateway_witness import QuotaWatch
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_EVERY_S', .05)
    monkeypatch.setattr(QuotaWatch, 'check', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('shape')))
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        job.model_or_combo = 'omniroute/gpt-6.1-sol'
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(.4)', cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert getattr(job, 'refusal', None) is None and job.exit_code != 7
    asyncio.run(run())


def test_call_log_poll_is_time_bounded(monkeypatch):
    from plangraph.spawner.gateway_witness import QuotaWatch
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_TIMEOUT_S', .05, raising=False)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.poll_quota', lambda *a, **k: time.sleep(.4) or THRESHOLD)
    job = SimpleNamespace(model_or_combo='omniroute/gpt-6.1-sol')
    async def run():
        started = time.monotonic()
        assert await QuotaWatch(job, {}).poll('j', job, {}) is None
        assert time.monotonic() - started < .3
    asyncio.run(run())


@pytest.mark.parametrize('line', ['resource_exhausted was handled; rate limit resets in 5m',
                                  'quota policy filtered nothing; rate limit resets in 5m'])
def test_prose_with_gateway_tokens_is_not_quota_evidence(line):
    assert harness_limit_line(1, '', line) is None and harness_limit_line(1, line, '') is None
    assert early_refusal(line + '\n') is None


def test_watchdog_recomputes_gateway_leg_per_check():
    from plangraph.spawner.gateway_witness import QuotaWatch
    job = SimpleNamespace(model_or_combo='mimo/mimo-v2.6-pro', start_time=time.time() - 999, last_activity_ts=None)
    cfg = '{"provider": {"mimo": {"options": {"baseURL": "http://gw/v1"}}}}'
    watch = QuotaWatch(job, {'OMNIROUTE_BASE_URL': 'http://gw', 'OPENCODE_CONFIG_CONTENT': cfg})
    gone = {'OMNIROUTE_BASE_URL': 'http://gw', 'OPENCODE_CONFIG_CONTENT': cfg.replace('gw', 'elsewhere')}
    assert watch.check('j', job, {'OMNIROUTE_BASE_URL': 'http://gw', 'OPENCODE_CONFIG_CONTENT': cfg}, asyncio.Event()) is True
    watch.last = 0.0
    assert watch.check('j', job, gone, asyncio.Event()) is False   # env changed: no longer a gateway leg


def _booked(tmp_path, harness, model, text):
    service = SimpleNamespace(leg_reader=LegStateStore(tmp_path / 'legs.sqlite'))
    before = time.time()
    reason = fallthrough_reason(service, SimpleNamespace(model=model, harness=harness),
                                SimpleNamespace(exit_code=7, stdout='', stderr=text), {})
    return reason, service.leg_reader, before


@pytest.mark.parametrize('harness,model,key,text,seconds', [
    ('agy', 'claude-opus-5-5-high', 'agy/claude-opus-5-5-high', AGY, 21 * 3600 + 2 * 60 + 13),
    ('opencode', 'omniroute/cx-gpt-6.1-sol', 'omniroute/cx-gpt-6.1-sol', THRESHOLD, RESET_CAP_S)])
def test_printed_refusal_books_leg_until_stated_reset_and_falls_through(tmp_path, harness, model, key, text, seconds):
    reason, store, before = _booked(tmp_path, harness, model, text)
    assert reason.startswith('harness_limit:')
    status = store.leg_status(key)
    assert status['state'] == 'limited' and before + seconds - 5 <= status['evidence']['reset_at'] <= time.time() + seconds + 5


def test_silent_agy_is_not_a_gateway_leg_and_its_printed_refusal_ends_the_run_at_once(tmp_path):
    script = f'import sys, time; sys.stderr.write({AGY!r} + chr(10)); sys.stderr.flush(); time.sleep(60)'
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        job.model_or_combo = 'claude-opus-5-5-high'
        assert poll_quota('j', job, {}, fetch=lambda t, e: ROWS) is None   # no omniroute rows exist for agy
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        started = time.monotonic()
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert time.monotonic() - started < 15 and job.exit_code == 7 and AGY in job.refusal
    asyncio.run(run())
