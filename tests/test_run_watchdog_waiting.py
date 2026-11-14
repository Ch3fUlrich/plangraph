"""Slow model turns and observable child CLIs are not three-minute stalls."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from plangraph.spawner.run_watchdog import Watchdog, thresholds


@pytest.mark.parametrize('role', ['review', 'implement'])
def test_silent_model_turn_survives_three_minutes(role):
    limits = thresholds(role=role)
    assert limits['stuck_s'] > limits['stall_s']
    gate = Watchdog(limits, 0, None, writer=role == 'implement')
    assert gate.decide(0, None, None, 0, stdout_activity=0) is None
    assert gate.decide(300, None, None, 0, stdout_activity=0) is None


def test_card_controls_idle_limit_without_extending_hard_wall():
    limits = thresholds({'stuck_min': 8, 'max_min': 10}, role='review')
    assert limits['stuck_s'] == 480
    gate = Watchdog(limits, 0, None, writer=False)
    assert gate.decide(0, None, None, 0) is None
    assert gate.decide(479, None, None, 0) is None
    assert gate.decide(480, None, None, 0)['rule'] == 'stuck'
    assert gate.decide(600, None, None, 0)['rule'] == 'max'


@pytest.mark.parametrize('value', [0, -1, True, '2', float('inf'), float('nan')])
def test_invalid_card_idle_limit_is_refused(value):
    from plangraph.routing_errors import SpawnRefused
    with pytest.raises(SpawnRefused, match='stuck_min'):
        thresholds({'stuck_min': value})


def test_live_child_counts_as_activity_but_keeps_hard_deadline():
    # Non-writer roles have no ordinary activity/edit stall; child presence defers stuck only.
    gate = Watchdog(thresholds({'stuck_min': 1, 'stall_min': 1, 'max_min': 10}, role='review'),
                    0, 'same', writer=False)
    assert gate.decide(0, 'same', 0, 0) is None
    assert gate.decide(300, 'same', 0, 0, waiting=True) is None
    assert gate.decide(600, 'same', 0, 0, waiting=True)['rule'] == 'max'


@pytest.mark.parametrize('state,expected', [('S', True), ('R', True), ('Z', False)])
def test_cpu_sample_records_visible_live_child(monkeypatch, state, expected):
    from plangraph.spawner import run_cpu
    rows = {100: (1, 100, 0, 'S'), 101: (100, 100, 0, state)}
    monkeypatch.setattr(run_cpu.sys, 'platform', 'linux')
    monkeypatch.setattr(run_cpu, 'proc_scan', lambda: rows)
    job = SimpleNamespace()
    runner = SimpleNamespace(jobs={'job': job})
    assert asyncio.run(run_cpu.cpu_seconds(runner, 'job', 100, 1)) == 0
    assert job._watchdog_child_active is expected


def test_idle_default_follows_card_stall_window():
    limits = thresholds({'stall_min': 40}, role='implement')
    assert limits['stuck_s'] == limits['stall_s'] + 300


@pytest.mark.parametrize('edited', [False, True])
def test_persistent_live_child_cannot_reset_writer_stall_progress(edited):
    limits = thresholds({'stall_min': 1, 'stuck_min': .1, 'max_min': 10}, role='implement')
    gate = Watchdog(limits, 0, 'before', writer=True)
    edit = 'after' if edited else 'before'
    assert gate.decide(1, edit, 1, 0, waiting=True) is None
    for now in (20, 40, 60):
        assert gate.decide(now, edit, 1, 0, waiting=True) is None
    event = gate.decide(61, edit, 1, 0, waiting=True)
    assert event is not None
    assert event['rule'] == ('no-progress' if edited else 'no-activity')
    assert event['last_activity_ts'] == 1
    assert gate.progress['activity'] == 1


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux descendant sampling')
@pytest.mark.parametrize('quota_refusal', [False, True])
def test_persistent_child_run_stalls_or_fails_fast_on_quota(tmp_path, monkeypatch, quota_refusal):
    import sys
    from plangraph.spawner import proc_kill
    from plangraph.spawner.run_watchdog import collect
    from test_run_watchdog import _job
    refusal = 'Error: request failed with status 429'
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_IDLE_S', .1)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.QUOTA_POLL_EVERY_S', .05)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.poll_quota', lambda *a, **k: refusal if quota_refusal else None)
    script = ("import subprocess, sys, time; "
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "print(child.pid, flush=True); time.sleep(60)")
    async def run():
        runner, job = _job(tmp_path, {'stall_min': .01, 'stuck_min': .001, 'max_min': .05})
        job._watchdog_limits['interval'] = .05
        job._watchdog_limits['hard_max'] = True
        job.model_or_combo = 'omniroute/gpt-6.1-sol'
        observed = []
        runner.on_sample = lambda *a: observed.append(getattr(job, '_watchdog_child_active', False))
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=proc_kill.root_identity(proc.pid), job_object=False)}
        try:
            output, _ = await asyncio.wait_for(collect(runner, 'j', proc), 10)
            assert any(observed), 'the live descendant must actually be sampled'
            if quota_refusal:
                assert job.status == 'failed' and job.exit_code == 7 and job.refusal == refusal
            else:
                assert job.status == 'stalled' and job.exit_code == 6
                assert any(e.get('rule') == 'no-activity' for e in job.events)
            assert not proc_kill.alive(int(output.splitlines()[0]), None)
        finally:
            await runner._reap_process('j', proc)
    asyncio.run(run())
