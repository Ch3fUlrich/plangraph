"""D-3605 F1: a refusal after a banner or '\r' spinner frames is caught."""
import asyncio
import sys
import time
from pathlib import Path

import pytest

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


def test_quota_prose_in_a_healthy_run_is_not_a_refusal(tmp_path):
    body = ("sys.stdout.write('Error: usage limit test case passed\\n'); sys.stdout.flush()\n"
            "for _i in range(3):\n    time.sleep(1.5)\n    sys.stdout.write('step %d\\n' % _i); sys.stdout.flush()\n")
    job, proc, _ = _collect(tmp_path, body)
    assert getattr(job, 'refusal', None) is None and proc.returncode == 0


def test_real_refusal_then_hang_is_confirmed_in_time(tmp_path):
    job, proc, took = _collect(tmp_path, REFUSE)
    assert took < 10 and job.status == 'failed' and job.exit_code == 7 and job.refusal


def test_refusal_then_nonzero_exit_is_booked_at_once(tmp_path):
    body = "sys.stdout.write('Error: Rate limit reached, please wait\\n'); sys.stdout.flush()\nsys.exit(1)\n"
    job, proc, took = _collect(tmp_path, body)
    assert took < 2 and job.refusal and proc.returncode == 1 and job.status == "failed" and job.exit_code == 7


def test_spinner_frame_flood_past_the_frame_cap_is_task_text(tmp_path):
    # F1: 600 '\r' frames burn the frame budget (400); the refusal-shaped line
    # after them is task text and the healthy run is never killed.
    frames = "for _i in range(600):\n    sys.stdout.write('frame %d\\r' % _i); sys.stdout.flush()\n"
    body = (frames + "sys.stdout.write('Error: Rate limit reached, please wait\\n'); sys.stdout.flush()\n"
            "time.sleep(6)\nprint('done')\n")
    job, proc, took = _collect(tmp_path, body)
    assert took >= 5                          # hangs past any quiet-confirm; never reaped as a refusal
    assert getattr(job, 'refusal', None) is None and proc.returncode == 0
    assert not any(e['event'] == 'dispatch_refused' for e in job.events)


def test_spinner_frames_within_the_frame_cap_still_refuse(tmp_path):
    frames = "for _i in range(50):\n    sys.stdout.write('frame %d\\r' % _i); sys.stdout.flush()\n"
    job, proc, took = _collect(tmp_path, frames + REFUSE)
    assert took < 10 and job.status == 'failed' and job.exit_code == 7
    assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)


def test_pending_candidate_past_the_window_is_never_booked():
    # F2: a candidate whose timestamp passed job.start_time + REFUSAL_WINDOW_S
    # is dropped at a non-zero exit, never booked.
    from types import SimpleNamespace
    from plangraph.spawner.run_refusal import RefusalGate
    async def run():
        job = SimpleNamespace(start_time=time.time() - 31, status='running', exit_code=0, events=[])
        async def _reap(job_id, proc):
            pass
        gate = RefusalGate(asyncio.Event(), job, lambda text: text)
        gate.candidate('Error: Rate limit reached, please wait', time.time())
        return job, await gate.finish(SimpleNamespace(_reap_process=_reap), 'j', SimpleNamespace(returncode=1), 'out')
    job, result = asyncio.run(run())
    assert result == 'out'
    assert getattr(job, 'refusal', None) is None
    assert job.status == 'running' and job.exit_code == 0 and job.events == []


def test_candidate_at_exit_books_and_reaps_the_process_tree():
    # F4: the non-zero-exit booking reaps the remaining process tree, exactly
    # like the quiet-confirmed refusal path in collect().
    from types import SimpleNamespace
    from plangraph.spawner.run_refusal import RefusalGate
    async def run():
        job = SimpleNamespace(start_time=time.time() - 1, status='running', exit_code=0, events=[])
        reaped = []
        async def _reap(job_id, proc):
            reaped.append((job_id, proc.pid))
        gate = RefusalGate(asyncio.Event(), job, lambda text: text)
        gate.candidate('Error: Rate limit reached, please wait', time.time())
        await gate.finish(SimpleNamespace(_reap_process=_reap), 'j', SimpleNamespace(returncode=1, pid=42), 'out')
        return job, reaped
    job, reaped = asyncio.run(run())
    assert job.status == 'failed' and job.exit_code == 7
    assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)
    assert reaped == [('j', 42)]


@pytest.mark.skipif(sys.platform != 'linux', reason='real /proc process tree')
def test_refusal_then_exit_reaps_grandchild_holding_the_pipe(tmp_path, monkeypatch):
    # F4: refusal then non-zero exit with a grandchild still holding the pipe:
    # the whole process tree is reaped on the finish-booking path.
    from plangraph.spawner import run_watchdog
    monkeypatch.setattr(run_watchdog, 'READER_GRACE_S', .5)   # finish books before the 3 s quiet-confirm
    body = ("import subprocess\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open('gp', 'w').write(str(p.pid))\n"
            "sys.stdout.write('Error: Rate limit reached, please wait\\n'); sys.stdout.flush()\n"
            "sys.exit(1)\n")
    job, proc, took = _collect(tmp_path, body)
    assert took < 5 and job.status == 'failed' and job.exit_code == 7
    assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)
    pid = int((tmp_path / 'gp').read_text())
    path = Path(f'/proc/{pid}/stat')          # a killed grandchild may linger as a zombie until its reaper runs
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            stat = path.read_text()
        except OSError:
            break
        if stat.rsplit(')', 1)[1].split()[0] == 'Z':
            break
        time.sleep(.05)
    assert not path.exists() or path.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
