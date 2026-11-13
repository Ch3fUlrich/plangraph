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


@pytest.mark.parametrize('remaining', [0, 0.01, 30, 30.01, 150])
def test_reasoning_retry_needs_useful_budget(launch_leg_case, monkeypatch, remaining):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time',
                        SimpleNamespace(time=lambda: 201 if len(calls) == 2 else 200 - remaining))
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100, 'hard_max': True}))
    assert len(calls) == (2 if remaining > 0 else 1)
    if remaining > 0:
        assert calls[1]['watchdog'] == {'max_s': pytest.approx(min(120, remaining)), 'hard_max': True}


@pytest.mark.parametrize('status', ['completed', 'failed', 'terminated_after_answer'])
@pytest.mark.parametrize('answer', ['A completed review in prose.', 'VERDICT: PASS'])
def test_continuation_without_exhaustion_is_not_relaunched(launch_leg_case, monkeypatch, status, answer):
    import asyncio
    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    jobs[1].status, jobs[1].stdout, jobs[1].events = status, answer, []
    job = asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 300}))
    assert job is jobs[1] and job.status == status and job.stdout == answer
    assert len(calls) == 2 and record['reasoning_continuations'] == 1
    assert record.get('reason') is None


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


@pytest.mark.parametrize('size_kb', [25, 55, 100])
@pytest.mark.parametrize('model', ['mimo-v2.6-flash', 'mimo-v2.6-pro'])
@pytest.mark.parametrize('routed', [False, True])
def test_large_mimo_review_prompt_split(launch_leg_case, monkeypatch, size_kb, model, routed):
    import asyncio
    from dataclasses import replace
    from plangraph.spawner.opencode_answer import REVIEW_PROMPT_MAX_BYTES
    from plangraph.spawner.runner import JobInfo
    seat, record, chosen, jobs, calls = launch_leg_case
    chosen = replace(chosen, model='mimo/' + model)
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    line = 'evidence: λ and a complete source line\n'
    prompt = line * ((size_kb * 1024 - 1) // len(line.encode()))
    prompt += 'x' * (size_kb * 1024 - len(prompt.encode()) - 1) + '\n'
    assert len(prompt.encode()) == size_kb * 1024
    payloads, sessions = [], []

    async def spawn(**options):
        calls.append(options)
        sessions.append((options['review_session'], options['review_session'].id))
        payload = options['prompt']
        assert len(payload.encode()) <= REVIEW_PROMPT_MAX_BYTES
        payloads.append(payload.split('=== REVIEW PROMPT PART ===\n', 1)[1].split('\n=== END REVIEW PROMPT PART ===', 1)[0])
        job = JobInfo(str(len(calls)), 'completed', payload, chosen.model, role='review', exit_code=0,
                      stdout='VERDICT: PASS')
        job.session_id = 'ses_fixture'
        return job

    seat.runner.spawn = spawn
    job = asyncio.run(seat._launch_leg(record, chosen, routed=routed, prompt=prompt,
                                       prompt_record=None, watchdog={'max_s': 300}))
    assert ''.join(payloads) == prompt
    assert len(calls) > 1 and job.job_id == str(len(calls))
    assert all(session is sessions[0][0] for session, _ in sessions)
    assert sessions[0][0].closed
    assert [sid for _, sid in sessions] == [None] + ['ses_fixture'] * (len(calls) - 1)
    assert len(record['attempts']) == len(calls) - 1
    assert all(attempt['status'] == 'context_loaded' for attempt in record['attempts'])
    assert record['events'][0] == dict(event='review_prompt_split', parts=len(calls),
                                      bytes=len(prompt.encode()), max_bytes=REVIEW_PROMPT_MAX_BYTES)
    assert 'Do not review yet' in calls[0]['prompt']
    assert 'Now review the complete request' in calls[-1]['prompt']


@pytest.mark.parametrize('failure', ['failed', 'timeout', 'missing-session', 'length', 'terminated_after_answer', 'cancelled'])
def test_prompt_split_cannot_publish_partial_review(launch_leg_case, monkeypatch, failure):
    import asyncio
    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    jobs[0].stdout = 'VERDICT: ACCEPT'  # A partial verdict cannot authorize the remaining request.
    if failure != 'length':
        jobs[0].events = []
        jobs[0].status = 'completed' if failure in ('missing-session', 'cancelled') else failure
    if failure == 'cancelled':
        seat._path(record['run_id']).with_name('cancelled').touch()
    if failure == 'missing-session':
        async def missing(**options):
            calls.append(options)
            return jobs[0]
        seat.runner.spawn = missing
    job = asyncio.run(seat._launch_leg(record, chosen, prompt='source line\n' * 10000,
                                       prompt_record=None, watchdog={'max_s': 300}))
    assert job.status == (failure if failure in ('timeout', 'cancelled') else 'failed')
    assert len(calls) == (2 if failure == 'length' else 1)
    if failure == 'missing-session':
        assert record['reason'] == 'reasoning_session_missing'
    if failure == 'length':
        assert record['reason'] == 'reasoning_only_length' and record['reasoning_continuations'] == 1
    if failure == 'terminated_after_answer':
        assert record['reason'] == 'reasoning_session_missing'


def test_prompt_split_preserves_source_redaction_boundaries():
    from plangraph.spawner.env_redact import _source_block, _redact_prompt, redact
    from plangraph.spawner.opencode_answer import split_review_prompt, REVIEW_PROMPT_MAX_BYTES
    prompt = 'TOKEN=caller-secret\n' + _source_block('TOKEN=source-fixture\n' * 3000)
    parts = split_review_prompt(prompt)
    rendered = [_redact_prompt(part, redact) for part in parts]
    assert len(parts) > 1
    assert all(len(part.encode()) <= REVIEW_PROMPT_MAX_BYTES for part in parts)
    assert sum(part.count('TOKEN=source-fixture') for part in rendered) == 3000
    assert 'caller-secret' not in ''.join(rendered)
    assert 'TOKEN=[REDACTED]' in rendered[0]


def test_prompt_split_refuses_unsplittable_line():
    from plangraph.spawner.context_pack import PromptBoundError
    from plangraph.spawner.opencode_answer import split_review_prompt, REVIEW_PROMPT_MAX_BYTES
    with pytest.raises(PromptBoundError, match='review source line exceeds split bound'):
        split_review_prompt('x' * (REVIEW_PROMPT_MAX_BYTES + 1))
    assert split_review_prompt('x' * REVIEW_PROMPT_MAX_BYTES) == ['x' * REVIEW_PROMPT_MAX_BYTES]


def test_prompt_chunks_share_one_retry_budget(launch_leg_case, monkeypatch):
    import asyncio
    from plangraph.spawner.runner import JobInfo
    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    async def spawn(**options):
        calls.append(options)
        exhausted = len(calls) != 2
        job = JobInfo(str(len(calls)), 'failed' if exhausted else 'completed', '', chosen.model,
                      role='review', exit_code=1 if exhausted else 0, events=[dict(
                          event='opencode_reasoning_exhausted', reason='reasoning_only_length')] if exhausted else [])
        job.session_id = 'ses_fixture'
        return job
    seat.runner.spawn = spawn
    job = asyncio.run(seat._launch_leg(record, chosen, prompt='source line\n' * 10000,
                                       prompt_record=None, watchdog={'max_s': 300}))
    assert len(calls) == 3 and record['reasoning_continuations'] == 1
    assert record['status'] == job.status == 'failed'
    assert record['reason'] == 'reasoning_only_length'
    assert len(record['attempts']) == 3
