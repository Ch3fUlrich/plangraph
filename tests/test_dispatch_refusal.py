"""D-3605 (a): a refused/rate-limited/credit/auth dispatch ends at once, books the leg down and notifies."""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.spawner.run_watchdog import collect
from tests.test_run_watchdog import _job, _own

LIMITS = {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30}
HANG = "import sys,time\nsys.%s.write(%r + '\\n'); sys.%s.flush()\ntime.sleep(40)\n"

FAKE_HARNESSES = {
    'agy 429': ('stderr', 'error: Individual quota reached. Resets in 10h40m26s.'),
    'opencode rate limit': ('stderr', 'Error: rate limit exceeded, retry later'),
    'qodercli credits out': ('stdout', 'Error: credits exhausted for this account'),
    'auth error': ('stderr', 'Error: HTTP 401 Unauthorized: invalid api key'),
}

# F1: refusal-looking output from a healthy run. None of these is error-shaped
# evidence of a refusal (start-anchored retry log / marker prose under an
# error shape / bare 403 prose): the run must finish rc 0, never be killed.
HEALTHY_LINES = {
    'retrying log': ('stderr', 'Rate limit reached, retrying in 2s'),
    'auth guard prose': ('stdout', 'Error: HTTP 403 Forbidden is returned by the auth guard'),
    'expected 403': ('stdout', 'HTTP 403: forbidden (expected, test_fetch)'),
}

HEALTHY = "import sys,time\nsys.%s.write(%r + '\\n'); sys.%s.flush()\ntime.sleep(.5)\nprint('done', flush=True)\n"


@pytest.mark.parametrize('name', sorted(FAKE_HARNESSES))
def test_refused_dispatch_ends_within_10s(tmp_path, name):
    stream, line = FAKE_HARNESSES[name]
    script = HANG % (stream, line, stream)

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        started = time.time()
        _own(runner, proc)
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert time.time() - started < 10          # the child sleeps 40 s: it was stopped, not awaited
        assert job.status == 'failed' and job.exit_code == 7
        assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)
        assert proc.returncode is not None
    asyncio.run(run())


def test_healthy_slow_run_stays_running(tmp_path):
    script = ("import time\nprint('I fixed the rate limit handling and the unauthorized path', flush=True)\n"
              "time.sleep(1.5)\nprint('done', flush=True)\n")

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        _own(runner, proc)
        task = asyncio.ensure_future(collect(runner, 'j', proc))
        await asyncio.sleep(.8)
        assert not task.done() and job.status == 'running' and not getattr(job, 'refusal', None)
        output, _ = await task
        assert b'done' in output and proc.returncode == 0
        assert getattr(job, 'refusal', None) is None
    asyncio.run(run())


@pytest.mark.parametrize('name', sorted(HEALTHY_LINES))
def test_healthy_run_with_refusal_looking_prose_not_killed(tmp_path, name):
    stream, line = HEALTHY_LINES[name]
    script = HEALTHY % (stream, line, stream)

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        _own(runner, proc)
        output, _ = await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert proc.returncode == 0 and job.status == 'running'
        assert getattr(job, 'refusal', None) is None
        assert b'done' in output
    asyncio.run(run())


# F1 (D-3605): a real refusal is the FIRST thing a harness says. A healthy run
# that prints normal activity first and an error-shaped marker later (retry
# log / fixture prose) must never be killed or booked down. The 3 normal lines
# go out first (with a drain beat so arrival order matches write order); the
# marker is then the run's 4th activity line and may not speak a refusal.
LATE_ERRORS = {
    'retrying rate limit': ('stderr', 'Error: Rate limit reached, retrying in 2s'),
    'http 429 retry': ('stdout', 'HTTP 429: retrying in 2s'),
    'usage limit passed': ('stdout', 'error: usage limit test case passed'),
    'credits fixture': ('stdout', 'Error: credits exhausted fixture loaded'),
}

LATE = ("import sys,time\n"
        "for _i in range(45): print('step', _i, flush=True)\n"   # late = past REFUSAL_MAX_LINES (F1)
        "time.sleep(.1)\n"
        "sys.%s.write(%r + '\\n'); sys.%s.flush()\n"
        "time.sleep(.5)\nprint('done', flush=True)\n")


@pytest.mark.parametrize('name', sorted(LATE_ERRORS))
def test_healthy_run_late_error_line_not_killed(tmp_path, name):
    stream, line = LATE_ERRORS[name]
    script = LATE % (stream, line, stream)

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        _own(runner, proc)
        output, _ = await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert getattr(job, 'refusal', None) is None
        assert b'done' in output and proc.returncode == 0
    asyncio.run(run())


def test_refusal_as_second_activity_line_still_detected(tmp_path):
    # F1: the run's first 3 activity lines may speak a refusal; the 2nd one
    # (after a single 'starting' line) must still stop the run at once.
    script = ("import sys, time\n"
              "print('starting', flush=True)\n"
              "time.sleep(.1)\n"
              "sys.stderr.write('Error: rate limit exceeded, retry later\\n')\n"
              "sys.stderr.flush()\n"
              "time.sleep(40)\n")

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        started = time.time()
        _own(runner, proc)
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert time.time() - started < 10          # the child sleeps 40 s: it was stopped, not awaited
        assert job.status == 'failed' and job.exit_code == 7
        assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)
    asyncio.run(run())


@pytest.mark.parametrize('name', ['qodercli credits out', 'auth error'])
def test_credit_and_auth_refusal_books_leg_unavailable_until(tmp_path, name):
    stream, line = FAKE_HARNESSES[name]
    store = LegStateStore(tmp_path / 'legs.sqlite')
    job = SimpleNamespace(exit_code=7, stdout=line if stream == 'stdout' else '', stderr=line if stream == 'stderr' else '')
    reason = fallthrough_reason(SimpleNamespace(leg_reader=store),
                                SimpleNamespace(model='m', harness='qodercli'), job, {})
    assert reason.startswith('harness_limit:')
    state = store.leg_status('m')
    assert state['state'] == 'limited'
    assert state['evidence']['reset_at'] > time.time() + 1000      # unavailable_until, not a 10-minute hint


def test_refusal_notifies_parent_once_with_run_leg_reason(tmp_path, fake_base_fresh):
    from plangraph.spawner.runner import spawn_service, JobInfo
    from plangraph.planner.routing import SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = DEFAULT_LEGS[0]
    line = 'error: Individual quota reached. Resets in 1h.'

    class Runner:
        default_cwd = str(tmp_path)
        jobs, _processes, _tasks, provider_responses = {}, {}, {}, {}
        async def spawn(self, **kwargs):
            job = JobInfo('j1', 'failed', '', leg.model, role=kwargs['role'], cwd=str(tmp_path))
            job.exit_code, job.stderr, job.end_time = 7, line, time.time()
            self.jobs['j1'] = job
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            self._tasks['j1'] = done
            return job

    async def run():
        service = spawn_service(runner=Runner(), state=tmp_path / 'state',
                                leg_reader=LegStateStore(tmp_path / 'legs.sqlite'))
        service._route = lambda *a, **kw: SpawnPlan((leg,), (), 'test', (), ())
        return await service.spawn({'estimate_tokens': 1000}, wait_seconds=None, no_fallthrough=True)
    record = asyncio.run(run())
    assert record['status'] == 'failed'
    lines = [json.loads(l) for l in (tmp_path / 'state' / 'inbox' / 'parent.jsonl').read_text().splitlines()]
    refused = [e for e in lines if e['event'] == 'LEG_REFUSED']
    assert len(refused) == 1
    assert refused[0]['run_id'] == record['run_id'] and refused[0]['leg'] == leg.model
    assert 'quota reached' in refused[0]['reason']


def test_refusal_never_stomps_a_cancel_verdict(tmp_path, monkeypatch):
    # F2 (Q14/M9/M15): a cancel/stall/timeout verdict is final. A refusal line
    # is booked on the job but must not overwrite the verdict or fire
    # dispatch_refused; the loop keeps observing the run normally. (The child
    # outlives the F2 quiet-confirmation period so the refusal is booked.)
    monkeypatch.setenv('PLANGRAPH_SPAWN_WATCHDOG_INTERVAL', '0.05')
    script = "import time\nprint('Error: rate limit exceeded', flush=True)\ntime.sleep(4.5)\n"

    async def run():
        runner, job = _job(tmp_path, {'stall_min': 10, 'first_edit_min': 20, 'max_min': 0.03})
        job.status = 'cancelled'
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        _own(runner, proc)
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert getattr(job, 'refusal', None)     # the refusal itself was seen and booked
        assert not any(e['event'] == 'dispatch_refused' for e in job.events)
        assert job.exit_code != 7
        assert job.status == 'cancelled'
    asyncio.run(run())


def test_refusal_without_trailing_newline_still_detected(tmp_path):
    # F3: a refusal line printed without a trailing newline while the child
    # hangs lives in the unterminated tail; it must still stop the run at once.
    script = ("import sys, time\n"
              "sys.stderr.write('Error: rate limit exceeded')\n"
              "sys.stderr.flush()\n"
              "time.sleep(40)\n")

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        started = time.time()
        _own(runner, proc)
        await asyncio.wait_for(collect(runner, 'j', proc), timeout=20)
        assert time.time() - started < 10          # the child sleeps 40 s: it was stopped, not awaited
        assert job.status == 'failed' and job.exit_code == 7
        assert job.refusal and any(e['event'] == 'dispatch_refused' for e in job.events)
    asyncio.run(run())


def test_refusal_with_unproven_owner_returns_without_waiting(tmp_path):
    async def run():   # #336 fail-closed: nothing signalled, survivors booked, the 40 s child is not awaited
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', HANG % (('stderr', FAKE_HARNESSES['auth error'][1], 'stderr')), cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(collect(runner, 'j', proc), timeout=10)
            assert job.status == 'failed' and job.exit_code == 7 and proc.pid in job.survivors and b'401' in out
            assert all(proc._transport.get_pipe_transport(fd).is_closing() for fd in (1, 2))   # no transport leak
        finally:
            proc.kill(); await proc.wait()
    asyncio.run(run())
