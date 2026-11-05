"""D-3769 hotfix pins: a hung leg never sits 'running' silently. REMOVE-AT-R10."""
import asyncio
import sys

from plangraph.spawner.env_redact import early_refusal, harness_limit_line
from plangraph.spawner.proc_kill import root_identity
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
