"""D-4681: harnesses without repo tools receive review diffs regardless of tool_mode."""
from types import SimpleNamespace
import subprocess

import pytest

from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
from plangraph.spawner.seat_spawn import SeatSpawnMixin


@pytest.fixture
def diff_ref(tmp_path, monkeypatch):
    monkeypatch.delenv('PLANGRAPH_CONTEXT_STRICT', raising=False)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')

    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init', '-q')
    path = tmp_path / 'change.txt'
    path.write_text('before\n')
    git('add', 'change.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    path.write_text('after\n')
    git('add', 'change.txt')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'head')
    return {'repo': str(tmp_path), 'base': base, 'head': git('rev-parse', 'HEAD')}


def prompt(diff_ref, *, harness='opencode', mode='tools', role='review',
           missing=False, max_tokens=100000, document=False):
    seat = SeatSpawnMixin()
    seat.runner = SimpleNamespace(default_cwd=diff_ref['repo'])
    leg = SimpleNamespace(harness=harness, tool_mode=mode, max_request_tokens=max_tokens)
    card = {} if missing else {'diff_ref': diff_ref}
    result = seat._leg_prompt('review this', card, role, leg, document_review=document)
    assert leg.tool_mode == mode
    return result


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
def test_review_diff_inlined(diff_ref, mode):
    out = prompt(diff_ref, mode=mode)
    assert out == inline_diff_prompt('review this', diff_ref, max_chars=300000)
    assert '-before\n+after' in out


def test_tools_missing_diff_ref_unchanged(diff_ref):
    assert prompt(diff_ref, missing=True) == 'review this'


def test_inline_only_missing_diff_ref_refused(diff_ref):
    with pytest.raises(InlineDiffError, match='no diff_ref'):
        prompt(diff_ref, mode='inline-only', missing=True)


@pytest.mark.parametrize('harness', ['claude', 'agy', 'qodercli'])
@pytest.mark.parametrize('missing', [False, True])
def test_real_tool_harness_unchanged(diff_ref, harness, missing):
    assert prompt(diff_ref, harness=harness, missing=missing) == 'review this'


def test_opencode_implementation_unchanged(diff_ref):
    assert prompt(diff_ref, role='implement', missing=True) == 'review this'


def test_document_review_exception_preserved(diff_ref):
    assert prompt(diff_ref, missing=True, document=True) == 'review this'


@pytest.mark.parametrize('mode', ['tools', 'inline-only'])
def test_same_diff_cap(diff_ref, mode):
    from plangraph.spawner.context_pack import ContextError, PromptBoundError
    assert issubclass(PromptBoundError, ContextError)
    assert issubclass(PromptBoundError, InlineDiffError)
    with pytest.raises(PromptBoundError, match='prompt_too_large') as caught:
        prompt(diff_ref, mode=mode, max_tokens=1)
    assert isinstance(caught.value, InlineDiffError)
    assert isinstance(caught.value, ContextError)
    assert isinstance(caught.value, PromptBoundError)
    with pytest.raises(InlineDiffError, match='prompt_too_large: diff too large') as direct:
        inline_diff_prompt('review this', diff_ref, max_chars=3)
    assert direct.value.chars > direct.value.max_chars == 3


def test_tools_invalid_diff_ref_refused(diff_ref):
    with pytest.raises(InlineDiffError):
        prompt(dict(diff_ref, base='nonexistent-revision'))


@pytest.mark.parametrize('dispatch', ['routed', 'explicit', 'base'])
@pytest.mark.parametrize('scenario, launches, terminal', [
    ('verdict', 2, False), ('twice', 2, True), ('error', 2, True),
    ('no-cap', 1, True), ('normal', 1, False), ('non-review', 1, False),
    ('already-capped', 1, True), ('stderr-error', 2, True),
    ('zero-exit', 2, True), ('cancelled', 1, False),
    ('timeout', 1, False), ('retry-timeout', 2, False), ('deadline', 1, True),
    ('provider-stop', 2, True), ('thin-budget', 1, True),
    ('stalled', 1, False), ('retry-stalled', 2, False), ('retry-cancelled', 2, False),
    ('ordinary-timeout', 1, False), ('ordinary-cancelled', 1, False),
])
def test_reasoning_output_retry(diff_ref, tmp_path, monkeypatch, dispatch, scenario, launches, terminal):
    """Use real spawn/render/monitor/record paths; only transport and witnesses are fake."""
    import asyncio
    import json
    import time
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner.service import SpawnService

    chosen = next(l for l in DEFAULT_LEGS if l.id == 'mimo-v2.6-flash')
    if scenario == 'no-cap':
        chosen = next(l for l in DEFAULT_LEGS if l.id == 'cx-gpt-6-luna')
    role = 'orchestrate' if scenario == 'non-review' else 'review'
    good = 'Review complete\nTEST-GAMING: no - searched tests\nVERDICT: ACCEPT'
    exhausted = json.dumps({'type': 'step_finish', 'part': {
        'reason': 'length', 'tokens': {'output': 0, 'reasoning': 32768}}})
    if scenario == 'error':
        exhausted = json.dumps({'type': 'error', 'error': {'type': 'output-limit',
            'message': 'Response still truncated after two output token limit continuations'}})
    verdict = '\n'.join(json.dumps(e) for e in [
        {'type': 'text', 'part': {'text': good}},
        {'type': 'step_finish', 'part': {'reason': 'stop', 'tokens': {'output': 20, 'reasoning': 0}}}])
    calls, clock = [], []
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time',
                        SimpleNamespace(time=lambda: clock[0] if clock else time.time()))

    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=None, returncode=0 if scenario in ('normal', 'zero-exit') or len(calls) == 2 and scenario == 'verdict' else 1)

    async def collect(runner, jid, proc):
        child_env = runner._child_envs[jid]
        assert child_env is calls[-1][1]['env']
        config = json.loads(child_env['OPENCODE_CONFIG_CONTENT'])
        if scenario == 'already-capped':
            assert config['providers']['mimo']['models']['mimo-v2.6-flash']['body']['thinking'] == {'type': 'disabled'}
        successful = scenario == 'normal' or len(calls) == 2 and scenario == 'verdict'
        if scenario in ('cancelled', 'ordinary-cancelled') or scenario == 'retry-cancelled' and len(calls) == 2:
            runner.jobs[jid].status = 'cancelled'
        if scenario in ('timeout', 'stalled', 'ordinary-timeout') or scenario in ('retry-timeout', 'retry-stalled') and len(calls) == 2:
            status = 'stalled' if scenario.endswith('stalled') else 'timeout'
            runner.jobs[jid].status, runner.jobs[jid].exit_code = status, 6 if status == 'stalled' else 124
            runner.jobs[jid].events.append({'event': 'watchdog_' + status, 'rule': 'max'})
        if scenario in ('deadline', 'thin-budget'):
            clock[:] = [time.time() + (2000 if scenario == 'deadline' else 1475)]
        if scenario == 'provider-stop':
            runner.provider_responses[jid] = {'status': 429}
        if scenario == 'stderr-error':
            return b'', b'Error: output-limit: Response still truncated after two output token limit continuations'
        if scenario.startswith('ordinary-'):
            return b'', b''
        return (verdict if successful else exhausted).encode(), b''

    async def ready(*args, **kwargs):
        return None

    async def launch(plan, **kwargs):
        return plan

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', ready)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', launch)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', ready)
    def witness(runner, jid, job, env):
        job.request_side_model, job.witness_kind = chosen.model, 'hermetic-witness'
        job.served_family = 'fixture-family'
    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness', witness)
    monkeypatch.setattr('plangraph.spawner.runner._opencode_served_model', lambda *a: None)
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'test-credential')
    if scenario == 'already-capped':
        monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', json.dumps({'providers': {'mimo': {
            'package': '@opencode/ai/providers/openai-compatible', 'models': {
                'mimo-v2.6-flash': {'body': {'thinking': {'type': 'disabled'}}}}}}}))
    # The second plan leg must never turn terminal exhaustion into fall-through.
    plan = SpawnPlan((chosen, replace(chosen, id='spare')), (), 'test', (), ())
    runner = SpawnerRunner(default_cwd=diff_ref['repo'], process_factory=factory)
    monkeypatch.setattr(runner, '_reap_process', reap)
    service = (SpawnService if dispatch == 'base' else spawn_service)(runner=runner, state=tmp_path / 'state')
    continuation = AsyncMock(wraps=service._continue_writer)
    monkeypatch.setattr(service, '_continue_writer', continuation)
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    options = {'leg': chosen.id, 'override_reason': 'hermetic reasoning retry test'} if dispatch == 'explicit' else {}
    card = {'estimate_tokens': 1000, **({'diff_ref': diff_ref} if role == 'review' else {})}
    record = asyncio.run(service.spawn(card, 'Review this change', role=role,
                                     writer_families=['deepseek'], wait_seconds=None, **options))
    assert len(calls) == launches
    assert service.result(record['run_id']) == record
    assert record['ended'] >= record['started'] and not service._running_records and not runner._tasks
    assert (record.get('reason') == 'reasoning_output_limit') == terminal
    real = [a for a in record['attempts'] if a['status'] != 'skipped']
    assert len(real) == launches
    if launches == 2:
        first, second = [json.loads(c[1]['env']['OPENCODE_CONFIG_CONTENT']) for c in calls]
        model = chosen.model.split('/', 1)[1]
        assert 'thinking' not in first['providers']['mimo']['models'][model].get('body', {})
        assert second['providers']['mimo']['models'][model]['body']['thinking'] == {'type': 'disabled'}
        assert first['permission'] == second['permission']
        assert all(c[0][c[0].index('--model') + 1] == chosen.model for c in calls)
        assert runner.jobs[record['job_id']].task_prompt == next(iter(runner.jobs.values())).task_prompt
        assert runner.jobs[record['job_id']]._watchdog_limits['max_s'] < next(iter(runner.jobs.values()))._watchdog_limits['max_s']
        assert record['reasoning_retry'] == 1
        assert any(e['event'] == 'opencode_reasoning_exhausted' for e in record['events'])
        assert any(e['event'] == 'opencode_reasoning_exhausted' for e in real[0]['events'])
    if scenario in ('normal', 'verdict'):
        assert record['status'] == 'completed'
        assert record['stdout_tail'] == good
        assert record.get('reason') is None
    if terminal:
        assert record['status'] == 'failed' and record['exit_code'] != 0
        assert record['writer_family'] == 'fixture-family'
        assert continuation.await_count == 0
        assert all(any(e['event'] == 'opencode_reasoning_exhausted' for e in a['events']) for a in real)
        assert any(e['event'] == 'reasoning_output_limit' for e in record['events'])
        if dispatch == 'routed':
            job = runner.jobs[record['job_id']]
            assert record['request_side_model'] == job.request_side_model and record['witness_kind'] == job.witness_kind
            assert record['served_model_mismatch'] == any(e.get('event') == 'served_model_mismatch' for e in job.events)
            assert record['completion']['event'] == 'FAILED' and record['completion']['reason'] == 'reasoning_output_limit'
            assert record['error'] and record['completion']['rc'] == job.exit_code
            assert all(a['request_side_model'] == chosen.model and a['witness_kind'] == 'hermetic-witness' for a in real)
    if scenario in ('cancelled', 'ordinary-cancelled', 'retry-cancelled'):
        assert record['status'] == 'cancelled'
    if scenario == 'non-review':
        assert not any(e['event'].startswith(('opencode_step_finish', 'opencode_reasoning', 'reasoning_'))
                       for e in record['events'])
    if scenario in ('timeout', 'retry-timeout', 'ordinary-timeout'):
        assert record['status'] == 'timed_out' and record['reason'] == 'timeout'
    if scenario in ('stalled', 'retry-stalled'):
        assert record['status'] == record['reason'] == 'stalled'
    if 'timeout' in scenario or 'stalled' in scenario:
        assert sum(e['event'].startswith('watchdog_') for e in record['events']) == 1


@pytest.fixture
def launch_leg_case(tmp_path):
    """Observe the shared launch boundary independently of caller final saves."""
    import json
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.spawner.runner import JobInfo

    chosen = next(l for l in DEFAULT_LEGS if l.id == 'mimo-v2.6-flash')
    jobs = [JobInfo(name, 'failed', 'review', chosen.model, role='review', exit_code=1,
                   events=[dict(event='opencode_reasoning_exhausted', cap_available=True)])
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
        calls.append(options)
        return job

    seat.runner.spawn = spawn
    return seat, record, chosen, jobs, calls


@pytest.mark.parametrize('scenario', ['no-cap', 'already-retried', 'twice', 'render-refused'])
def test_reasoning_terminal_saved_at_launch_boundary(launch_leg_case, monkeypatch, scenario):
    import asyncio
    import json

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: 101))
    if scenario == 'no-cap':
        jobs[0].events[0]['cap_available'] = False
    if scenario == 'already-retried':
        record['reasoning_retry'] = 1
    if scenario == 'render-refused':
        jobs[1].events = [dict(event='render_refused', reason='reasoning_output_limit: unavailable')]
    job = asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100}))
    saved = json.loads(seat._path(record['run_id']).read_text())
    assert saved['status'] == job.status == 'failed'
    assert saved['reason'] == 'reasoning_output_limit' and saved['exit_code'] != 0
    assert len(saved['attempts']) == len(calls) == (2 if scenario in ('twice', 'render-refused') else 1)
    assert all(a['events'] for a in saved['attempts'])
    assert saved['events'][-1] == {'event': 'reasoning_output_limit'}


@pytest.mark.parametrize('remaining', [0, 0.01, 30, 30.01])
def test_reasoning_retry_needs_useful_budget(launch_leg_case, monkeypatch, remaining):
    import asyncio

    seat, record, chosen, jobs, calls = launch_leg_case
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time',
                        SimpleNamespace(time=lambda: 200 - remaining))
    asyncio.run(seat._launch_leg(record, chosen, prompt_record=None, watchdog={'max_s': 100, 'hard_max': True}))
    assert len(calls) == (2 if remaining > 30 else 1)
    if remaining > 30:
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
        if options.get('reasoning_cap'):
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
