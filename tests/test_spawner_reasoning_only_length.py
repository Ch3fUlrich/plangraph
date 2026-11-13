"""Recorded MiMo length stops must not wait for harness exit or a watchdog."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import SpawnerRunner, spawn_service


@pytest.fixture(params=[(31999, 1), (32002, 0)], ids=['run-e53273b7', 'run-ff4a9586'])
def recorded_length(request):
    reasoning, output = request.param
    return {'type': 'step_finish', 'sessionID': 'ses_recorded', 'part': {
        'reason': 'length', 'tokens': {'reasoning': reasoning, 'output': output}}}


@pytest.mark.parametrize('outcome', ['retry-answer', 'cooperative-reap', 'three-stops', 'retry-length', 'normal-stop',
                                   'continuation-timeout', 'first-timeout', 'retry-prose',
                                   'retry-other-verdict', 'retry-error'])
@pytest.mark.parametrize('dispatch', ['explicit', 'routed', 'base'])
def test_recorded_length_bounded_retry(tmp_path, monkeypatch, recorded_length, outcome, dispatch):
    from plangraph.planner.legs import DEFAULT_LEGS
    from plangraph.planner.routing import SpawnPlan

    chosen = next(leg for leg in DEFAULT_LEGS if leg.id == 'mimo-v2.6-flash')
    import time
    from pathlib import Path
    calls, killed = [], []
    clock = [time.time()]
    monkeypatch.setattr('plangraph.spawner.seat_spawn.time', SimpleNamespace(time=lambda: clock[0]))
    answer = 'No findings\nTEST-GAMING: no - inspected tests\nVERDICT: ACCEPT'
    if outcome == 'retry-prose': answer = 'The review is complete; there are no findings.'
    if outcome == 'retry-other-verdict': answer = 'VERDICT: PASS'
    prompt = 'Review the supplied evidence carefully. ' * 100

    async def ready(*args, **kwargs): return None
    async def plan_ready(plan, **kwargs): return plan

    async def factory(*args, **kwargs):
        proc = SimpleNamespace(pid=None, returncode=None,
                               stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        proc.stdin = SimpleNamespace(write=lambda data: None, drain=ready, close=lambda: None)
        calls.append((proc, kwargs['env'], args))
        if (outcome == 'continuation-timeout' and len(calls) == 2
                or outcome == 'first-timeout' and len(calls) == 1):
            next(reversed(runner.jobs.values()))._watchdog_limits['max_s'] = .01
            if outcome == 'first-timeout':
                job = next(reversed(runner.jobs.values()))
                job.events.append(dict(event='opencode_reasoning_exhausted', reason='reasoning_only_length'))
            return proc  # No new frames: the original length evidence must survive the watchdog.
        successful = outcome == 'normal-stop' or len(calls) == 2 and outcome in (
            'retry-answer', 'retry-prose', 'retry-other-verdict', 'cooperative-reap')
        if len(calls) == 2 and outcome == 'retry-error':
            proc.returncode = 2
            proc.stderr.feed_data(b'ordinary transport failure\n')
            proc.stdout.feed_eof(); proc.stderr.feed_eof()
            return proc
        events = ([{'type': 'text', 'sessionID': 'ses_recorded', 'part': {'text': answer}},
                   {'type': 'step_finish', 'sessionID': 'ses_recorded', 'part': {'reason': 'stop',
                    'tokens': {'reasoning': 26000, 'output': 20}}}]
                  if successful else [recorded_length])
        proc.stdout.feed_data(('\n'.join(map(json.dumps, events)) + '\n').encode())
        if len(calls) == 2 and outcome == 'retry-length':
            clock[0] += 2000
        if successful:
            proc.returncode = 0
            proc.stdout.feed_eof(); proc.stderr.feed_eof()
        return proc

    async def reap(jid, proc):
        if proc.returncode is None:
            killed.append(jid)
            proc.returncode = 0 if outcome == 'cooperative-reap' else 1
            proc.stdout.feed_eof(); proc.stderr.feed_eof()

    monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', ready)
    monkeypatch.setattr('plangraph.spawner.resolver.plan_for_launch_async', plan_ready)
    monkeypatch.setattr('plangraph.spawner.leg_probe.before_launch', ready)
    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness', lambda *args: None)
    monkeypatch.setattr('plangraph.spawner.runner._opencode_served_model', lambda *args: None)
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'test-credential')
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    monkeypatch.setattr(runner, '_reap_process', reap)
    from plangraph.spawner.service import SpawnService
    service = (SpawnService if dispatch == 'base' else spawn_service)(runner=runner, state=tmp_path / 'state')
    monkeypatch.setattr(service, '_route', lambda *args, **kwargs: SpawnPlan(
        (chosen,), (), 'fixture', (), ()))
    async def prepared(prompt, *args, **kwargs): return prompt
    monkeypatch.setattr(service, '_prepare_leg_prompt', prepared)

    async def run():
        return await asyncio.wait_for(service.spawn({'estimate_tokens': 1000}, prompt,
            role='review', writer_families=['deepseek'], wait_seconds=None,
            **({} if dispatch == 'routed' else dict(leg=chosen.id,
                override_reason='recorded event regression'))), 10)

    record = asyncio.run(run())
    assert service.result(record['run_id']) == record
    count = 0 if outcome in ('normal-stop', 'first-timeout') else 1
    assert len(calls) == count + 1
    assert len(killed) == (2 if outcome in ('retry-length', 'three-stops', 'continuation-timeout') else
                           1 if outcome == 'first-timeout' else count)
    assert not runner._tasks and not service._running_records
    assert record['ended'] >= record['started']
    assert any(e['event'].startswith('watchdog_') for e in record['events']) == ('timeout' in outcome)
    assert len({env['XDG_DATA_HOME'] for _, env, _ in calls}) == 1
    assert not Path(calls[0][1]['XDG_DATA_HOME']).exists()
    for _, env, _ in calls:
        entry = json.loads(env['OPENCODE_CONFIG_CONTENT'])['providers']['mimo']['models']['mimo-v2.6-flash']
        assert entry['limit']['output'] == int(env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX']) == 131072
        assert 'thinking' not in entry.get('body', {})
    jobs = list(runner.jobs.values()); assert jobs[0].task_prompt == prompt
    if outcome == 'cooperative-reap':
        assert jobs[0].status == 'failed' and jobs[0].exit_code == 7
    if count:
        for job in jobs[1:]:
            assert job.task_prompt == 'answer now: findings, then the VERDICT line; no further analysis'
            assert job.command[job.command.index('--session') + 1] == 'ses_recorded'
            assert job._watchdog_limits['hard_max'] and 0 < job._watchdog_limits['max_s'] <= 120
        events = [e for e in record['events'] if e['event'] == 'reasoning_continuation']
        assert record['reasoning_continuations'] == len(events) == count
        assert [e['count'] for e in events] == list(range(1, count + 1))
        assert all(e['reasoning_tokens'] == recorded_length['part']['tokens']['reasoning'] for e in events)
        assert all(e['output_tokens'] == recorded_length['part']['tokens']['output'] for e in events)
        assert any(e.get('reason') == 'reasoning_only_length' for e in record['attempts'][0]['events'])
    if 'timeout' in outcome:
        assert record['status'] == 'timed_out' and record['reason'] == 'timeout'
        assert record['exit_code'] == 124
        assert not any(e['event'] == 'reasoning_only_length' and 'continuations' in e for e in record['events'])
        assert len(record['attempts']) == count + 1
    elif outcome in ('retry-length', 'three-stops'):
        assert record['status'] == 'failed' and record['exit_code'] != 0
        assert record['reason'] == 'reasoning_only_length'
        if dispatch != 'base':
            assert (record['completion']['event'], record['completion']['reason']) == ('FAILED', 'reasoning_only_length')
        assert len(record['attempts']) == count + 1
    elif outcome == 'retry-error':
        assert record['status'] == 'failed' and record['exit_code'] == 2
        assert record.get('reason') != 'reasoning_only_length'
        assert 'ordinary transport failure' in record['stderr_tail']
    elif outcome == 'retry-prose':
        assert jobs[-1].status == 'completed' and jobs[-1].exit_code == 0
        assert record['status'] == 'failed' and record['reason'] == 'no_verdict'
        assert record['stdout_tail'] == answer
    else:
        assert record['status'] == 'completed' and record.get('reason') is None
        assert record['stdout_tail'] == answer


@pytest.mark.parametrize('case', ['length', 'text', 'later-text', 'writer', 'stop', 'bad-usage', 'plain'])
def test_live_length_gate_preserves_other_output(recorded_length, case):
    from plangraph.spawner.opencode_answer import ReasoningLengthGate
    from plangraph.spawner.runner import JobInfo
    job = JobInfo('fixture', 'running', '', 'mimo/mimo-v2.6-flash',
                  role='implement' if case == 'writer' else 'review', command=['opencode'])
    stopped = asyncio.Event()
    gate = ReasoningLengthGate(job, stopped)
    text = {'type': 'text', 'part': {'text': 'Answer'}}
    if case == 'stop': recorded_length['part']['reason'] = 'stop'
    if case == 'bad-usage': recorded_length['part']['tokens']['reasoning'] = True
    events = ([text] if case == 'text' else []) + [recorded_length] + ([text] if case == 'later-text' else [])
    payload = '\n'.join(map(json.dumps, events)) + '\n'
    if case == 'plain': payload = 'Complete plain-text answer\n' + payload
    gate.feed(payload[:17])
    assert not stopped.is_set()
    gate.feed(payload[17:])
    assert stopped.is_set() == (case == 'length')


def test_live_length_gate_clears_stale_wakeup(recorded_length):
    from plangraph.spawner.opencode_answer import ReasoningLengthGate
    from plangraph.spawner.runner import JobInfo
    gate = ReasoningLengthGate(JobInfo('fixture', 'running', '', 'mimo/mimo-v2.6-flash',
                                      role='review', command=['opencode']), asyncio.Event())
    gate.feed(json.dumps(recorded_length) + '\n')
    assert gate.exhausted and gate.stopped.is_set()
    gate.feed(json.dumps({'type': 'text', 'part': {'text': 'Recovered answer'}}) + '\n')
    assert not gate.exhausted and not gate.stopped.is_set()


def test_collect_records_length_stop_without_monitor(tmp_path, monkeypatch, recorded_length):
    import time
    from plangraph.spawner.run_watchdog import collect, thresholds
    from plangraph.spawner.runner import JobInfo

    async def run():
        job = JobInfo('fixture', 'running', '', 'mimo/mimo-v2.6-flash', role='review',
                      command=['opencode'], cwd=str(tmp_path), start_time=time.time())
        job._watchdog_limits = thresholds({'max_min': 1}, role='review')
        proc = SimpleNamespace(pid=None, returncode=None, stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        proc.stdout.feed_data((json.dumps(recorded_length) + '\n').encode())
        reaped = []
        async def reap(jid, process):
            reaped.append(jid)
            process.returncode = 0  # A cooperative reap must not erase the length verdict.
            process.stdout.feed_eof(); process.stderr.feed_eof()
        runner = SimpleNamespace(jobs={job.job_id: job}, _child_envs={job.job_id: {}},
                                 _stdin_payloads={}, _reap_process=reap, default_cwd=str(tmp_path))
        output = await asyncio.wait_for(collect(runner, job.job_id, proc), 2)
        assert output[0] == (json.dumps(recorded_length) + '\n').encode()
        assert reaped == [job.job_id]
        assert (job.status, job.exit_code) == ('failed', 7)
        assert dict(event='reasoning_only_length') in job.events
        assert dict(event='opencode_reasoning_exhausted', reason='reasoning_only_length') in job.events
        from plangraph.spawner.opencode_answer import record_steps
        record_steps(job, output[0].decode(), '', {})
        assert sum(e['event'] == 'opencode_reasoning_exhausted' for e in job.events) == 1
    asyncio.run(run())


def test_retry_instruction_replaces_prose_not_evidence():
    from plangraph.spawner.opencode_answer import retry_prompt
    evidence = '\n\n=== CONTEXT PACK ===\nsource\n\n=== INLINE DIFF (base a, head b) ===\ndiff'
    result = retry_prompt('Analyze thoroughly. ' * 100 + evidence)
    assert result == 'answer now: findings, then the VERDICT line; no further analysis'


@pytest.mark.parametrize('model', ['mimo-v2.6-pro', 'mimo-v2.6-flash', 'cx-gpt-6-luna'])
def test_model_output_cap_reaches_launch(tmp_path, monkeypatch, model):
    from plangraph.spawner.launch_spec import build
    monkeypatch.setenv('OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX', '17')
    result = build(dict(run_id='run-' + 'a' * 32, job_id='fixture', role='review', dry_run=True,
        leg={'id': model, 'explicit': True}, prompt='Review', card={}, cwd=str(tmp_path), state=str(tmp_path / 'state')))
    if model.startswith('mimo-'):
        entry = json.loads(result['env']['OPENCODE_CONFIG_CONTENT'])['providers']['mimo']['models'][model]
        assert entry['limit']['output'] == int(result['env']['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX']) == 131072
    else:
        assert 'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX' not in result['env']
