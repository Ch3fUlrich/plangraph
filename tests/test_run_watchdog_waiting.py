"""Slow model turns and observable child CLIs are not three-minute stalls."""
import asyncio
import sys
import time
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
    gate = Watchdog(thresholds({'stuck_min': 1, 'stall_min': 1, 'max_min': 10}, role='review'),
                    0, 'same', writer=False)
    assert gate.decide(0, 'same', 0, 0) is None
    for now in range(30, 600, 30):
        assert gate.decide(now, 'same', 0, now, children={(101, 1)}) is None
    assert gate.decide(600, 'same', 0, 600, children={(101, 1)})['rule'] == 'max'


@pytest.mark.parametrize('state,expected', [('S', True), ('R', True), ('Z', False)])
def test_cpu_sample_records_visible_live_child(monkeypatch, state, expected):
    from plangraph.spawner import run_cpu
    rows = {100: (1, 100, 0, 'S', 1), 101: (100, 100, 0, state, 2)}
    monkeypatch.setattr(run_cpu.sys, 'platform', 'linux')
    monkeypatch.setattr(run_cpu, 'proc_scan', lambda: rows)
    job = SimpleNamespace()
    runner = SimpleNamespace(jobs={'job': job})
    assert asyncio.run(run_cpu.cpu_seconds(runner, 'job', 100, 1)) == 0
    assert job._watchdog_children == ({(101, 2)} if expected else set())


def test_idle_default_follows_card_stall_window():
    limits = thresholds({'stall_min': 40}, role='implement')
    assert limits['stuck_s'] == limits['stall_s'] + 300


@pytest.mark.parametrize('edited', [False, True])
def test_persistent_live_child_cannot_reset_writer_stall_progress(edited):
    limits = thresholds({'stall_min': 1, 'stuck_min': 2, 'max_min': 10}, role='implement')
    gate = Watchdog(limits, 0, 'before', writer=True)
    edit = 'after' if edited else 'before'
    assert gate.decide(1, edit, 1, 0, children={(101, 1)}) is None
    for now in (20, 40, 60):
        assert gate.decide(now, edit, 1, 0, children={(101, 1)}) is None
    event = gate.decide(61, edit, 1, 0, children={(101, 1)})
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
    script = ("import subprocess, sys, time; time.sleep(0.08); "
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "print(child.pid, flush=True); time.sleep(60)")
    async def run():
        runner, job = _job(tmp_path, {'stall_min': .01, 'stuck_min': .02, 'max_min': .05})
        job._watchdog_limits['interval'] = .05
        job._watchdog_limits['hard_max'] = True
        job.model_or_combo = 'omniroute/gpt-6.1-sol'
        observed = []
        runner.on_sample = lambda *a: observed.append(bool(getattr(job, '_watchdog_children', None)))
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=proc_kill.root_identity(proc.pid), job_object=False)}
        try:
            # Start the monitoring scenario only after its live child exists.
            ready = await asyncio.wait_for(proc.stdout.readline(), 5)
            assert ready.strip().isdigit()
            proc.stdout.feed_data(ready)
            job.start_time = job.last_activity_ts = time.time()
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


@pytest.mark.parametrize('active', [False, True])
def test_review_child_cpu_progress_controls_stuck_deadline(active):
    gate = Watchdog(thresholds({'stuck_min': 1, 'max_min': 10}, role='review'),
                    0, None, writer=False)
    for now in (0, 30, 59):
        assert gate.decide(now, None, None, now if active else 0, children={(101, 1)}) is None
    finding = gate.decide(60, None, None, 60 if active else 0, children={(101, 1)})
    if active:
        assert finding is None
    else:
        assert finding['rule'] == 'stuck'
        assert gate.decide(60, None, None, 0, children={(101, 2)}) is None
        assert gate.decide(120, None, None, 0, children={(101, 2)})['rule'] == 'stuck'


def test_windows_job_tree_cpu_progress_with_unobserved_children(monkeypatch):
    from plangraph.spawner import run_cpu

    monkeypatch.setattr(run_cpu.sys, 'platform', 'win32')
    job = SimpleNamespace(_watchdog_children={(101, 1)})
    totals = iter([0, 1, 1, 1])
    runner = SimpleNamespace(jobs={'job': job}, _win_jobs={'job': 42},
                             win_job_api=SimpleNamespace(cpu_seconds=lambda _: next(totals)))
    gate = Watchdog(thresholds({'stuck_min': 1, 'max_min': 10}, role='review'),
                    0, None, writer=False)
    findings = []
    for now in (0, 30, 60, 90):
        cpu = asyncio.run(run_cpu.cpu_seconds(runner, 'job', 100, now))
        assert job._watchdog_children is None
        findings.append(gate.decide(now, None, None, cpu, children=job._watchdog_children))
    assert findings[:3] == [None, None, None]
    assert findings[3]['rule'] == 'stuck'
