"""Quota refusals (omniroute, agy, opencode-go) book the leg down and end the run at once."""
import asyncio
import json
import sys
import sqlite3
import time
from types import SimpleNamespace

import pytest
from test_run_watchdog import _job

from plangraph.routing_support import leg_state, leg_state_flag as flag, quota_snapshots
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
    want = 1000.0 + (1800 if seconds is None else seconds)
    status = store.leg_status('omniroute/gpt-6.1-sol', now=1001.0)
    assert status['state'] == 'limited' and status['evidence']['reset_at'] == want
    if seconds is not None and seconds > RESET_CAP_S:
        assert store.leg_status('omniroute/gpt-6.1-sol', now=1000.0 + RESET_CAP_S + 3600)['state'] == 'limited'
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


@pytest.mark.parametrize('later', [200, 201, 0, 500])
@pytest.mark.parametrize('reverse', [False, True])
def test_only_latest_request_can_refuse_a_recovered_run(later, reverse):
    rows = [dict(sessionTag='j', id=1, status=429, error=THRESHOLD),
            dict(sessionTag='j', id=2, status=later)]
    assert quota_refusal(rows[::-1] if reverse else rows, 'j') is None


def test_429_without_summary_is_a_refusal():
    assert quota_refusal([dict(sessionTag='j', status=429)], 'j') == 'HTTP 429'


def test_resetless_api_refresh_does_not_extend_hold_forever(monkeypatch):
    """No stated reset: the first-seen exhausted observation holds until recovery is seen or the source is blind."""
    monkeypatch.setattr(quota_snapshots, 'effective_gateway_base_url', lambda env: 'https://gw/v1')
    monkeypatch.setattr(quota_snapshots, 'load_token', lambda env: 'k')
    clock = [1000.0]
    monkeypatch.setattr(quota_snapshots.time, 'time', lambda: clock[0])
    stale = quota_snapshots.SOURCE_STALE_S
    data = [dict(provider='codex:a1', remainingPct=0)]
    fetch = lambda *a: (200, json.dumps({'data': data}).encode())
    gate = QuotaGate(lambda: fetch_snapshot_rows({}, fetch=fetch), clock=lambda: clock[0])
    assert gate.apply(SOL, OK)['evidence']['reset_at'] == 1000 + stale
    clock[0] = 2900   # the old 30-minute clock hold is gone: nothing recorded recovery
    assert gate.apply(SOL, OK)['evidence']['reset_at'] == 1000 + stale   # a refresh never moves first-seen
    data[0]['remainingPct'] = 100   # a fresher row showing recovery clears it
    clock[0] = 3000
    assert gate.apply(SOL, OK) == OK
    data[0]['remainingPct'] = 0
    clock[0] = 3100
    assert gate.apply(SOL, OK)['evidence']['reset_at'] == 3100 + stale
    clock[0] = 3100 + stale + 1   # a blind source (no fresher row for SOURCE_STALE_S) never blocks
    assert gate.apply(SOL, OK) == OK


def test_db_returns_latest_row_of_each_window_in_id_order(tmp_path):
    path = tmp_path / 'snapshots.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider, connection_id, '
                   'window_key, remaining_percentage, is_exhausted, next_reset_at, created_at)')
        for ident, window, pct in [(1, 'weekly', 0), (2, 'daily', 30), (3, 'weekly', 100)]:
            db.execute('INSERT INTO quota_snapshots VALUES (?,?,?,?,?,?,?,?)',
                       (ident, 'codex', 'a', window, pct, 0, None, '2026-11-13 10:00:00'))
    rows = fetch_snapshot_rows({'QUOTA_SNAPSHOT_DB': str(path)})
    assert [(r['window_key'], r['remaining_percentage']) for r in rows] == [('daily', 30), ('weekly', 100)]


@pytest.mark.parametrize('with_sensitive_values', [False, True], ids=['quota', 'redacted-quota'])
def test_silent_gateway_leg_ends_on_logged_quota_refusal_without_waiting_for_stall(
        tmp_path, monkeypatch, with_sensitive_values):
    summary = THRESHOLD
    expected = THRESHOLD
    if with_sensitive_values:
        summary += ' ' + 'gh' + 'p_' + 'A9' * 16 + ' ' + 'ab12' * 10
        expected += ' [REDACTED] [REDACTED]'
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_IDLE_S', 0)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_EVERY_S', .05)
    rows = [{**ROWS[0], 'error_summary': summary}, ROWS[1]]
    monkeypatch.setattr('plangraph.spawner.gateway_witness.fetch_rows', lambda *a: rows)
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        job.model_or_combo = 'omniroute/gpt-6.1-sol'
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert job.status == 'failed' and job.exit_code == 7 and job.refusal == expected
        event = next(event for event in job.events if event['event'] == 'dispatch_refused')
        assert event['reason'] == expected
    asyncio.run(run())


SOL = 'omniroute/cx-gpt-6.2-sol-medium'
OK = {'state': 'ok', 'evidence': None}


def test_read_only_snapshot_source_preserves_reset(tmp_path):
    path = tmp_path / 'snapshots.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider, connection_id, window_key, '
                   'remaining_percentage, is_exhausted, next_reset_at, created_at)')
        db.execute('INSERT INTO quota_snapshots (provider,connection_id,window_key,remaining_percentage,is_exhausted,next_reset_at,created_at) VALUES (?,?,?,?,?,?,?)',
                   ('codex', 'a1', 'weekly', 0, 1, '2026-11-19T00:00:00Z', '2026-11-13T00:00:00Z'))
    def no_http(*args):
        raise AssertionError('local snapshot needs no gateway request or credentials')
    rows = fetch_snapshot_rows({'QUOTA_SNAPSHOT_DB': str(path)}, fetch=no_http)
    now = quota_snapshots._ts('2026-11-13T10:00:00Z')
    reset = quota_snapshots._ts('2026-11-19T00:00:00Z')
    gate = QuotaGate(lambda: rows)
    assert gate.apply(SOL, OK, now)['evidence']['reset_at'] == reset
    assert gate.apply(SOL, OK, reset + 1) == OK


def test_missing_or_corrupt_snapshot_db_never_blocks_or_creates_file(tmp_path):
    path = tmp_path / 'snapshot.sqlite'
    source = lambda: fetch_snapshot_rows({'QUOTA_SNAPSHOT_DB': str(path)})
    assert QuotaGate(source).apply(SOL, OK) == OK
    assert not path.exists()
    path.write_text('not a database')
    assert QuotaGate(source).apply(SOL, OK) == OK


def test_api_reset_and_resetless_observation_holds(monkeypatch):
    monkeypatch.setattr(quota_snapshots, 'effective_gateway_base_url', lambda env: 'https://gw/v1')
    monkeypatch.setattr(quota_snapshots, 'load_token', lambda env: 'k')
    monkeypatch.setattr(quota_snapshots.time, 'time', lambda: 1000.0)
    for extra, until in [('', 1000.0 + quota_snapshots.SOURCE_STALE_S), (', "next_reset_at": "1970-01-01T02:00:00Z"', 7200.0)]:
        def fetch(*args):
            return 200, ('{"data":[{"provider":"codex:a1","remainingPct":0' + extra + '}]}').encode()
        gate = QuotaGate(lambda: fetch_snapshot_rows({}, fetch=fetch), clock=lambda: 1000.0)
        assert gate.apply(SOL, OK, 1001)['evidence']['reset_at'] == until
        assert gate.apply(SOL, OK, until + 1) == OK


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
    assert any(s['leg'] == 'cx-gpt-6.2-sol-medium' and s['reason'] == 'quota_until ' + stamp for s in during.skipped)
    after = plan_spawn(card, role='implement', leg_reader=reader, now=reset + 1)
    assert after.legs[0].model == SOL


def test_exhausted_codex_pool_skips_every_cx_leg_with_quota_until_and_first_leg_is_next(tmp_path):
    from plangraph.planner.routing import plan_spawn
    now = time.time()
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 7200))
    gate = QuotaGate(lambda: [_row(0, stamp)], clock=lambda: now)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    reader = lambda model, **scope: gate.apply(model, store.leg_status(model, **scope), scope.get('now'))
    plan = plan_spawn({'estimate_tokens': 1000}, role='implement', leg_reader=reader, now=now + 60)
    cx = [leg for leg in plan.legs if leg.id.startswith('cx-gpt-')]
    assert not cx and plan.first_leg is not None and not plan.first_leg.id.startswith('cx-gpt-')
    skips = {s['leg']: s['reason'] for s in plan.skipped if s['leg'].startswith('cx-gpt-')}
    assert 'cx-gpt-6.2-sol-medium' in skips and 'cx-gpt-6.2-astra' in skips
    assert set(skips.values()) == {'quota_until ' + stamp}
    past = plan_spawn({'estimate_tokens': 1000}, role='implement', leg_reader=reader, now=now + 7201)
    assert past.first_leg.id == 'cx-gpt-6.2-sol-medium'
    assert not any(s['reason'].startswith('quota_until') for s in past.skipped)


def test_pool_stays_up_while_any_account_can_serve_and_other_pools_are_untouched():
    now = time.time()
    future = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 600))
    gate = QuotaGate(lambda: [_row(0, future, account='a1'), _row(55, None, account='a2'), _row(0, future, provider='claude')])
    assert gate.apply(SOL, OK, now) == OK
    assert gate.apply('omniroute/cx-gpt-6.2-sol', OK, now) == OK
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
    monkeypatch.setattr(quota_snapshots, 'effective_gateway_base_url', lambda env: 'https://gw/v1')
    monkeypatch.setattr(quota_snapshots, 'load_token', lambda env: 'k')
    seen = []
    def fetch(url, headers, timeout):
        seen.append((url, headers))
        return 200, b'{"data": [{"provider": "codex:a1", "remainingPct": 0}]}'
    rows = fetch_snapshot_rows({}, fetch=fetch)
    assert rows[0]['provider'] == 'codex:a1' and rows[0]['remainingPct'] == 0
    assert 'created_at' not in rows[0]  # observation time belongs to the gate, not each fetch
    assert seen[0][0].startswith('https://gw/api/usage/utilization?') and seen[0][1] == {'Authorization': 'Bearer k'}
    for bad in (lambda u, h, t: (500, b'{}'), lambda u, h, t: (200, b'nope'), lambda u, h, t: (_ for _ in ()).throw(OSError())):
        with pytest.raises(LookupError):
            fetch_snapshot_rows({}, fetch=bad)
    monkeypatch.setattr(quota_snapshots, 'load_token', lambda env: None)
    with pytest.raises(LookupError):
        fetch_snapshot_rows({}, fetch=fetch)
    def missing_token(env):
        raise LookupError('no PLANGRAPH_GATEWAY_LOG_TOKEN')
    monkeypatch.setattr(quota_snapshots, 'load_token', missing_token)
    with pytest.raises(LookupError):
        fetch_snapshot_rows({}, fetch=fetch)


def test_production_reader_applies_the_snapshot_gate(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(flag, 'default_reader', _production_reader)
    flag.default_reader.cache_clear()
    monkeypatch.setattr(leg_state, '_default_store', lambda: LegStateStore(tmp_path / 'state'))
    monkeypatch.setattr(quota_snapshots, 'from_env', lambda: SimpleNamespace(snapshot=lambda: {}))
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
    ('opencode', 'omniroute/cx-gpt-6.1-sol', 'omniroute/cx-gpt-6.1-sol', THRESHOLD, 141 * 3600 + 52 * 60)])
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


def test_agy_account_limit_keeps_other_harnesses_and_accounts_available(tmp_path):
    """An AgY booking is scoped to its account and model."""
    from dataclasses import replace
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import plan_spawn

    store = LegStateStore(tmp_path / "legs.sqlite")
    agy = next(leg for leg in DEFAULT_LEGS if leg.id == "agy-gemini-4-hydrogen-high")
    codex = replace(
        next(leg for leg in DEFAULT_LEGS if leg.id == "cx-gpt-6.2-sol-medium"),
        harness="codex", model="agy/shared-fixture", roles=("review",),
    )
    claude = next(leg for leg in DEFAULT_LEGS if leg.id == "claude-haiku-5.5")
    legs = (agy, codex, claude)
    store.record_harness_limit("agy/gemini-4-argon-low", "Individual quota reached. Resets in 2h.",
                               "fixture", now=100, account="A")
    store.record_harness_limit(claude.model, "Quota reached. Resets in 5m.", "fixture",
                               now=101, account="A")
    kwargs = dict(role="review", writer_families=("deepseek",), legs=legs, leg_reader=store,
                  catalog_rows=({"id": codex.model, "available": 1, "active": True},))
    diff = dict(repo=str(tmp_path), base="base", head="HEAD")

    def route(account, now):
        return plan_spawn(dict(estimate_tokens=40000, account=account, diff_ref=diff), now=now,
                          **kwargs)

    plan = route("A", 102)
    assert {leg.id for leg in plan.legs} == {agy.id, codex.id}, plan.skipped
    skipped = {row["leg"]: row for row in plan.skipped}
    assert skipped[claude.id]["reason"] == "limited"
    assert store.leg_status("agy:gemini-4-argon", now=102, account="A")["state"] == "limited"
    assert store.leg_status("agy:gemini-4-argon", now=102, account="B")["state"] == "ok"

    assert {leg.id for leg in route("B", 102).legs} == {leg.id for leg in legs}
    assert {leg.id for leg in route("A", 402).legs} == {leg.id for leg in legs}

    store.record_provider_response(codex.model, 429, "Codex rate limit", {"retry-after": "60"},
                                   "fixture", now=500, account="A")
    plan = route("A", 501)
    codex_evidence = next(row["evidence"] for row in plan.skipped if row["leg"] == codex.id)
    assert codex_evidence["body_excerpt"] == "Codex rate limit"
    assert {leg.id for leg in plan.legs} == {agy.id, claude.id}


def test_a_successful_probe_clears_a_stored_usage_limit_before_its_reset(tmp_path):
    """D-6804: an upgraded plan lifts the limit; the stored Nov-27 reset must not keep the leg down."""
    now = time.time()
    far = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 3 * 86400))
    probes = {}
    gate = QuotaGate(lambda: [_row(0, far)], clock=lambda: now, probe_ts=probes.get)
    assert gate.apply(SOL, OK, now)['state'] == 'limited'
    probes[SOL] = quota_snapshots._ts('2026-11-12T00:00:00Z')   # older than the snapshot: still limited
    assert gate.apply(SOL, OK, now)['state'] == 'limited'
    probes[SOL] = now                                            # newer than the snapshot: cleared
    assert gate.apply(SOL, OK, now) == OK
    assert gate.apply('omniroute/cc-claude-x', OK, now) == OK    # other pools never read this probe
    newer = dict(_row(0, far), created_at='2099-01-01T00:00:00Z')   # a later snapshot that is exhausted again wins
    again = QuotaGate(lambda: [newer], clock=lambda: now, probe_ts=probes.get)
    assert again.apply(SOL, OK, now)['state'] == 'limited'


def test_an_unreadable_probe_store_never_clears_a_limit():
    now = time.time()
    far = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 3600))
    gate = QuotaGate(lambda: [_row(0, far)], clock=lambda: now, probe_ts=_boom(OSError('x')))
    assert gate.apply(SOL, OK, now)['state'] == 'limited'


def test_production_reader_clears_the_snapshot_limit_after_a_probe_pass(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(flag, 'default_reader', _production_reader)
    flag.default_reader.cache_clear()
    store = LegStateStore(tmp_path / 'state')
    monkeypatch.setattr(leg_state, '_default_store', lambda: store)
    monkeypatch.setattr(quota_snapshots, 'from_env', lambda: SimpleNamespace(snapshot=lambda: {}))
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: []))
    monkeypatch.setattr(quota_snapshots, 'fetch_snapshot_rows', lambda: [_row(0, '2099-01-01T00:00:00Z')])
    try:
        reader = flag.default_reader()
        assert reader.leg_status(SOL, now=now)['state'] == 'limited'
        store.record_probe_pass(SOL, 'review-probe', now=now)
        assert reader.leg_status(SOL, now=now)['state'] == 'ok'
    finally:
        flag.default_reader.cache_clear()


def test_one_decision_reads_the_snapshot_once_even_when_it_is_never_cached():
    """The probe comparison uses the rows that showed the exhaustion, not a second read."""
    now = time.time()
    far = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 3600))
    exhausted = dict(_row(0, far), created_at='1970-01-01T00:15:00Z')   # observed at 900
    reads = [[exhausted], []]
    gate = QuotaGate(lambda: reads.pop(0) if reads else [], clock=lambda: now, ttl_s=0,
                     probe_ts=lambda model: 800.0)
    assert gate.apply(SOL, OK, now)['state'] == 'limited'   # 800 < 900: the probe predates the snapshot
    assert reads == [[]]                                    # exactly one read


def test_production_reader_clears_the_snapshot_limit_of_a_sibling_after_a_quota_recovery(tmp_path, monkeypatch):
    """A sibling leg of the probed account has no probe pass of its own; the recovery row is its signal."""
    now = time.time()
    sibling = 'omniroute/cx-gpt-6.2-astra'
    monkeypatch.setattr(flag, 'default_reader', _production_reader)
    flag.default_reader.cache_clear()
    store = LegStateStore(tmp_path / 'state')
    monkeypatch.setattr(leg_state, '_default_store', lambda: store)
    monkeypatch.setattr(quota_snapshots, 'from_env', lambda: SimpleNamespace(snapshot=lambda: {}))
    monkeypatch.setattr(leg_state.LegAdapter, 'standalone_models', staticmethod(lambda: []))
    monkeypatch.setattr(quota_snapshots, 'fetch_snapshot_rows', lambda: [_row(0, '2099-01-01T00:00:00Z')])
    try:
        reader = flag.default_reader()
        assert reader.leg_status(sibling, now=now)['state'] == 'limited'
        store.record_quota_recovery(sibling, 'review-probe', now=now)
        assert reader.leg_status(sibling, now=now)['state'] == 'ok'
        assert store.last_probe_ts(sibling) is None
    finally:
        flag.default_reader.cache_clear()


def test_an_exhausted_snapshot_does_not_clear_by_the_clock_at_its_stored_reset():
    """The replay of the review: limited just before the stored reset, still limited at it, until recovery is recorded."""
    now = 1_800_000_000.0
    reset = now + 1000
    iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
    seen = iso(now - 60)
    row = dict(_row(0, iso(reset)), created_at=seen)
    gate = QuotaGate(lambda: [row], clock=lambda: now, ttl_s=0)
    assert gate.apply(SOL, OK, reset - 1)['state'] == 'limited'
    at_reset = gate.apply(SOL, OK, reset)
    assert at_reset['state'] == 'limited' and at_reset['evidence']['reset_at'] > reset   # waits for recovery evidence
    # (1) a fresher row of the window showing recovery clears it
    fresh = dict(_row(100, iso(reset + 3600)), created_at=iso(reset + 1))
    assert QuotaGate(lambda: [row, fresh], clock=lambda: now, ttl_s=0).apply(SOL, OK, reset + 5) == OK
    # (2) a successful probe newer than the snapshot clears it
    probed = QuotaGate(lambda: [row], clock=lambda: now, ttl_s=0, probe_ts=lambda model: reset + 2)
    assert probed.apply(SOL, OK, reset + 5) == OK
    # (3) a blind source (no fresher row for SOURCE_STALE_S) never blocks
    stale_at = now - 60 + quota_snapshots.SOURCE_STALE_S
    assert gate.apply(SOL, OK, stale_at - 1)['state'] == 'limited'
    assert gate.apply(SOL, OK, stale_at + 1) == OK
