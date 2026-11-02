"""D-3605 F1: a refusal after a banner or '\r' spinner frames is caught."""
import asyncio
import sys
import time

from plangraph.spawner.run_watchdog import collect
from tests.test_run_watchdog import _job

LIMITS = {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30}
BANNER = ("for _l in ['opencode 1.4.2','loading config','connecting to provider']:\n"
          "    sys.stdout.write(_l + '\\n'); sys.stdout.flush()\n")
SPINNER = "for _i in range(12):\n    sys.stdout.write('frame %d\\r' % _i); sys.stdout.flush()\n"
REFUSE = ("sys.stdout.write('Error: Rate limit reached, please wait\\n'); sys.stdout.flush()\n"
          "time.sleep(40)\n")


def _collect(tmp_path, body):
    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import sys,time\n' + body,
            cwd=tmp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        t0 = time.time()
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        return job, proc, time.time() - t0
    return asyncio.run(run())


def test_refusal_after_banner_is_detected(tmp_path):
    job, proc, took = _collect(tmp_path, BANNER + REFUSE)
    assert took < 10 and job.status == 'failed' and job.exit_code == 7
    assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)


def test_refusal_after_spinner_frames_is_detected(tmp_path):
    job, proc, took = _collect(tmp_path, SPINNER + REFUSE)
    assert took < 10 and job.status == 'failed' and job.exit_code == 7
    assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)


def test_refusal_shaped_line_past_the_line_window_is_task_text(tmp_path):
    body = ("for _i in range(45):\n    sys.stdout.write('working step %d\\n' % _i); sys.stdout.flush()\n"
            "sys.stdout.write('Error: Rate limit reached, retrying in 2s\\n'); sys.stdout.flush()\nprint('done')\n")
    job, proc, _ = _collect(tmp_path, body)
    assert getattr(job, 'refusal', None) is None and proc.returncode == 0
