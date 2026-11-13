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


@pytest.mark.parametrize('outcome', ['retry-answer', 'three-stops', 'retry-length', 'normal-stop', 'continuation-timeout'])
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
    prompt = 'Review the supplied evidence carefully. ' * 100

    async def ready(*args, **kwargs): return None
    async def plan_ready(plan, **kwargs): return plan

    async def factory(*args, **kwargs):
        proc = SimpleNamespace(pid=None, returncode=None,
                               stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        proc.stdin = SimpleNamespace(write=lambda data: None, drain=ready, close=lambda: None)
        calls.append((proc, kwargs['env'], args))
        if outcome == 'continuation-timeout' and len(calls) == 2:
            next(reversed(runner.jobs.values()))._watchdog_limits['max_s'] = .01
            return proc  # No new frames: the original length evidence must survive the watchdog.
        successful = outcome == 'normal-stop' or len(calls) == (4 if outcome == 'three-stops' else 2) and outcome != 'retry-length'
        events = ([{'type': 'text', 'sessionID': 'ses_recorded', 'part': {'text': answer}},
                   {'type': 'step_finish', 'sessionID': 'ses_recorded', 'part': {'reason': 'stop',
                    'tokens': {'reasoning': 26000, 'output': 20}}}]
                  if successful else [recorded_length])
        proc.stdout.feed_data(('\n'.join(map(json.dumps, events)) + '\n').encode())
        if outcome == 'retry-length' and len(calls) == 5:
            clock[0] += 2000
        if successful:
            proc.returncode = 0
            proc.stdout.feed_eof(); proc.stderr.feed_eof()
        return proc

    async def reap(jid, proc):
        if proc.returncode is None:
            killed.append(jid)
            proc.returncode = 1
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
    count = {'normal-stop': 0, 'retry-answer': 1, 'three-stops': 3, 'retry-length': 4, 'continuation-timeout': 1}[outcome]
    assert len(calls) == count + 1
    assert len(killed) == (5 if outcome == 'retry-length' else 2 if outcome == 'continuation-timeout' else count)
    assert not runner._tasks and not service._running_records
    assert record['ended'] >= record['started']
    assert any(e['event'].startswith('watchdog_') for e in record['events']) == (outcome == 'continuation-timeout')
    assert len({env['XDG_DATA_HOME'] for _, env, _ in calls}) == 1
    assert not Path(calls[0][1]['XDG_DATA_HOME']).exists()
    for _, env, _ in calls:
        entry = json.loads(env['OPENCODE_CONFIG_CONTENT'])['providers']['mimo']['models']['mimo-v2.6-flash']
        assert entry['limit']['output'] == int(env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX']) == 131072
        assert 'thinking' not in entry.get('body', {})
    jobs = list(runner.jobs.values()); assert jobs[0].task_prompt == prompt
    if count:
        for job in jobs[1:]:
            assert job.task_prompt == 'answer now: findings, then the VERDICT line; no further analysis'
            assert job.command[job.command.index('--session') + 1] == 'ses_recorded'
            assert job._watchdog_limits['hard_max'] and job._watchdog_limits['max_s'] > 0
        events = [e for e in record['events'] if e['event'] == 'reasoning_continuation']
        assert record['reasoning_continuations'] == len(events) == count
        assert [e['count'] for e in events] == list(range(1, count + 1))
        assert all(e['reasoning_tokens'] == recorded_length['part']['tokens']['reasoning'] for e in events)
        assert all(e['output_tokens'] == recorded_length['part']['tokens']['output'] for e in events)
        assert any(e.get('reason') == 'reasoning_only_length' for e in record['attempts'][0]['events'])
    if outcome in ('retry-length', 'continuation-timeout'):
        assert record['status'] == 'failed' and record['exit_code'] != 0
        assert record['reason'] == 'reasoning_only_length'
        if dispatch != 'base':
            assert (record['completion']['event'], record['completion']['reason']) == ('FAILED', 'reasoning_only_length')
        assert len(record['attempts']) == count + 1
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
