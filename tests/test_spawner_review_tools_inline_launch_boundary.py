"""Launch-boundary reasoning retries, terminal records, and cancellation guards."""
from types import SimpleNamespace

import pytest

from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def launch_leg_case(tmp_path):
    """Observe the shared launch boundary independently of caller final saves."""
    import json
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner.runner import JobInfo

    chosen = next(l for l in DEFAULT_LEGS if l.id == 'mimo-v2.6-flash')
    jobs = [JobInfo(name, 'failed', 'review', chosen.model, role='review', exit_code=1,
                   events=[dict(event='opencode_reasoning_exhausted', reason='reasoning_only_length')])
            for name in ('first', 'second')]
    calls = []
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(_processes={}, _tasks={}, _owned={}, default_cwd=str(tmp_path))
    seat._running_records = {}
    seat._path = lambda run_id: tmp_path / 'record.json'
    seat._save = lambda record: seat._path(record['run_id']).write_text(json.dumps(record))
    record = dict(run_id='run-test', role='review', status='running', started=100,
                  attempts=[], events=[], writer_family='original')

    async def spawn(**options):
        job = jobs[len(calls)]
        job.session_id = 'ses_fixture'
        calls.append(options)
        return job

    seat.runner.spawn = spawn
    return seat, record, chosen, jobs, calls


@pytest.mark.parametrize('scenario', ['deadline', 'twice', 'missing-session'])
def test_reasoning_terminal_saved_at_launch_boundary(launch_leg_case, monkeypatch, scenario):
    import asyncio
    import json

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(
        time=lambda: 201 if scenario == 'deadline' or len(calls) == 2 else 101))
    if scenario == 'missing-session':
        async def missing(**options):
            calls.append(options)
            return jobs[0]
        seat.runner.spawn = missing
    job = asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    saved = json.loads(seat._path(record['run_id']).read_text())
    assert saved['status'] == job.status == 'failed'
    reason = 'reasoning_session_missing' if scenario == 'missing-session' else 'reasoning_only_length'
    assert saved['reason'] == reason and saved['exit_code'] != 0
    assert len(saved['attempts']) == len(calls) == (2 if scenario == 'twice' else 1)
    assert all(a['events'] for a in saved['attempts'])
    assert saved['events'][-1] == dict(event=reason, continuations=len(calls) - 1)


@pytest.mark.parametrize('remaining', [0, 0.01, 30, 30.01])
def test_reasoning_retry_needs_useful_budget(launch_leg_case, monkeypatch, remaining):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time',
                        SimpleNamespace(time=lambda: 201 if len(calls) == 2 else 200 - remaining))
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100, 'hard_max': True}))
    assert len(calls) == (2 if remaining > 0 else 1)
    if remaining > 0:
        assert calls[1]['watchdog'] == {'max_s': pytest.approx(remaining), 'hard_max': True}


@pytest.mark.parametrize('guard', ['timeout', 'timed_out', 'stalled', 'cancelled', 'watchdog'])
def test_reasoning_retry_respects_late_terminal_mapping(launch_leg_case, monkeypatch, guard):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    if guard == 'watchdog':
        jobs[0].events.append(dict(event='watchdog_timeout', rule='max'))
    else:
        record.update(status=guard, reason=guard)
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    assert len(calls) == 1 and record.get('reason') != 'reasoning_output_limit'


@pytest.mark.parametrize('guard', ['cancelled', 'timed_out', 'kill_incomplete'])
def test_retry_render_refusal_cannot_override_terminal_guard(launch_leg_case, monkeypatch, guard):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    jobs[1].events = [dict(event='render_refused', reason='reasoning_output_limit: unavailable')]
    original = seat.runner.spawn

    async def spawn(**options):
        job = await original(**options)
        if len(calls) == 2:
            if guard == 'cancelled':
                seat._path(record['run_id']).with_name('cancelled').touch()
            elif guard == 'kill_incomplete':
                job.survivors = [123]
            else:
                record.update(status=guard, reason=guard)
        return job

    seat.runner.spawn = spawn
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    assert len(calls) == 2 and record.get('reason') != 'reasoning_output_limit'


@pytest.mark.parametrize('exhausted', [False, True])
def test_cancel_marker_only_changes_exhausted_leg(launch_leg_case, exhausted):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    if not exhausted:
        jobs[0].events = []
    seat._path(record['run_id']).with_name('cancelled').touch()
    job = asyncio.run(seat._launch_leg(record, chosen, prompt_record=None))
    assert job.status == ('cancelled' if exhausted else 'failed')
    assert len(calls) == 1 and record.get('reason') != 'reasoning_output_limit'


def test_normal_base_launch_keeps_original_schema(launch_leg_case):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    record['role'], jobs[0].events = 'orchestrate', []
    seat.runner._owned['first'] = dict(pid=1, pid_start=2, job_object=True,
                                     job_name='owned', escapees=[], extra_owned='private')
    before = dict(record)
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None))
    assert record == dict(before, job_id='first', pid=None, pid_start=2, job_name='owned',
                          escapees=[], cwd=seat.runner.default_cwd)


@pytest.mark.parametrize('cancel', ['outer', 'inner'])
def test_base_launch_preserves_shield_cancellation(launch_leg_case, cancel):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    record['role'], jobs[0].events = 'orchestrate', []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def monitor():
            entered.set()
            await release.wait()

        task = seat.runner._tasks['first'] = asyncio.create_task(monitor())
        launch = asyncio.create_task(seat._launch_leg(record, chosen, prompt_record=None))
        await entered.wait()
        # A save means launch has reached the monitor await, without timing sleeps.
        while not seat._path(record['run_id']).exists():
            await asyncio.sleep(0)
        (launch if cancel == 'outer' else task).cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(launch, 1)
            if cancel == 'outer':
                assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, launch, return_exceptions=True)

    asyncio.run(run())
