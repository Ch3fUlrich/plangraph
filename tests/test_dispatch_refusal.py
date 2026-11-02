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
from tests.test_run_watchdog import _job

LIMITS = {'stall_min': 10, 'first_edit_min': 20, 'max_min': 30}
HANG = "import sys,time\nsys.%s.write(%r + '\\n'); sys.%s.flush()\ntime.sleep(40)\n"

FAKE_HARNESSES = {
    'agy 429': ('stderr', 'error: Individual quota reached. Resets in 10h40m26s.'),
    'opencode rate limit': ('stderr', 'Error: rate limit exceeded, retry later'),
    'qodercli credits out': ('stdout', 'Error: credits exhausted for this account'),
    'auth error': ('stderr', 'Error: HTTP 401 Unauthorized: invalid api key'),
}


@pytest.mark.parametrize('name', sorted(FAKE_HARNESSES))
def test_refused_dispatch_ends_within_10s(tmp_path, name):
    stream, line = FAKE_HARNESSES[name]
    script = HANG % (stream, line, stream)

    async def run():
        runner, job = _job(tmp_path, LIMITS)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        started = time.time()
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
        task = asyncio.ensure_future(collect(runner, 'j', proc))
        await asyncio.sleep(.8)
        assert not task.done() and job.status == 'running' and not getattr(job, 'refusal', None)
        output, _ = await task
        assert b'done' in output and proc.returncode == 0
        assert getattr(job, 'refusal', None) is None
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


def test_refusal_notifies_parent_once_with_run_leg_reason(tmp_path):
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
