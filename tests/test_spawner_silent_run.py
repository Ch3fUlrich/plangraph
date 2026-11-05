"""D-3769 hotfix pins: a hung leg never sits 'running' silently. REMOVE-AT-R10."""
import asyncio
import sys
from types import SimpleNamespace

from plangraph.spawner.env_redact import early_refusal, harness_limit_line
from plangraph.spawner.proc_kill import root_identity
from plangraph.spawner.run_refusal import LATE_QUIET_S, RefusalGate
from plangraph.spawner.run_watchdog import Watchdog, collect, thresholds
from test_run_watchdog import _job

COOLDOWN = 'Error: All credentials for model gpt-6.1-sol are cooling down'


def test_silent_writer_stalls_at_the_first_edit_default_not_45_min():
    limits = thresholds()
    w = Watchdog(limits, 0, ('files', 'head'))
    assert limits['first_edit_s'] == 15 * 60
    assert w.decide(14 * 60, ('files', 'head'), None, None) is None
    assert w.decide(15 * 60, ('files', 'head'), None, None)['rule'] == 'no-first-edit'


def test_silent_child_ends_stalled_with_no_activity(tmp_path):
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': .01, 'max_min': 30})
        job._watchdog_limits['interval'] = .05
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert job.status == 'stalled' and job.exit_code == 6 and job.last_activity_ts is None
        assert [e['rule'] for e in job.events if e['event'] == 'watchdog_stalled'] == ['no-first-edit']
    asyncio.run(run())


def test_cooldown_and_status_429_lines_count_prose_does_not():
    assert early_refusal(COOLDOWN + '\n') == COOLDOWN
    assert early_refusal('Error: request failed with status 429\n')
    assert harness_limit_line(1, '', COOLDOWN + '\n') == COOLDOWN
    assert early_refusal('the pool is cooling down, status 429\n') is None
    assert early_refusal('Error: bad value on line 429\n') is None


def _gate(refused):
    return RefusalGate(refused, SimpleNamespace(start_time=0, refusal=None), str)


def test_late_limit_line_books_after_silence_and_activity_cancels_it():
    refused = asyncio.Event()
    gate = _gate(refused)
    gate.late_candidate(COOLDOWN, 100)
    gate.poll(100 + LATE_QUIET_S - 1)
    assert not refused.is_set()
    gate.activity(100 + LATE_QUIET_S - 1)
    gate.poll(100 + 2 * LATE_QUIET_S)
    assert not refused.is_set()
    gate.late_candidate(COOLDOWN, 100)
    gate.poll(100 + LATE_QUIET_S)
    assert refused.is_set() and gate.job.refusal == COOLDOWN


def test_hung_child_after_a_late_limit_line_ends_failed_with_the_line(tmp_path, monkeypatch):
    monkeypatch.setattr('plangraph.spawner.run_watchdog.REFUSAL_WINDOW_S', 0)
    monkeypatch.setattr('plangraph.spawner.run_refusal.LATE_QUIET_S', .2)
    script = f"import time; print({COOLDOWN!r}, flush=True); time.sleep(60)"
    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30})
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        runner._owned = {'j': dict(pid=proc.pid, pid_start=root_identity(proc.pid), job_object=False)}
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert job.status == 'failed' and job.exit_code == 7
        assert [e['reason'] for e in job.events if e['event'] == 'dispatch_refused'] == [COOLDOWN]
    asyncio.run(run())
