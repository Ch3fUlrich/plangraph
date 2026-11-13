"""Quota refusals (omniroute, agy, opencode-go) book the leg down and end the run at once."""
import asyncio
import sys

import pytest
from test_run_watchdog import _job

from plangraph.routing_support.leg_state import LegStateStore, RESET_CAP_S
from plangraph.spawner.env_redact import early_refusal, harness_limit_line
from plangraph.spawner.gateway_witness import poll_quota, quota_refusal
from plangraph.spawner.proc_kill import root_identity
from plangraph.spawner.run_watchdog import collect

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
