import asyncio
from types import SimpleNamespace as NS

import pytest

from plangraph.planner.legs import Leg
from plangraph.planner.routing import SpawnPlan
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner.env_redact import harness_limit_line
from plangraph.spawner.exit_outcome import fallthrough_reason, terminal_reason
from plangraph.spawner.service import SpawnService


LINE = ("You've reached your credit usage limit. Please upgrade your subscription plan "
        "to get more resources. Report Issue (input /feedback)")


@pytest.mark.parametrize('code', [None, 0, 1, 7])
@pytest.mark.parametrize('stdout', [LINE, 'Starting\n' + LINE + '\n \n',
                                   LINE + '\nAgent continued.'])
def test_bare_stdout_credit_limit_requires_failed_final_line(code, stdout):
    expected = LINE if code in (1, 7) and stdout.rstrip().endswith(LINE) else None
    assert harness_limit_line(code, stdout, '') == expected


@pytest.mark.parametrize('prefix', ['', 'error: '])
def test_streaming_credit_limit_requires_error_shape(prefix):
    from plangraph.spawner.env_redact import early_refusal
    text = prefix + LINE
    assert early_refusal(text + '\n') == (text if prefix else None)


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('terminal', [None, 'git_config_mutated', 'writer_no_changes',
                                     'cancelled', 'kill_incomplete', 'model_refusal'])
def test_recorded_refusal_books_and_runs_next_leg(tmp_path, monkeypatch, stream, terminal):
    legs = tuple(Leg(name, 'subscription', 'qodercli', name, 'synthetic',
                     ('128k',), 131072, False) for name in ('first', 'next', 'unused'))
    store = LegStateStore(tmp_path / 'legs.sqlite')
    runner = NS(_processes={}, _tasks={}, provider_responses={}, jobs={})
    calls = []

    async def spawn(**kwargs):
        name = kwargs['model_or_combo']
        calls.append(name)
        if terminal:
            kwargs['prompt_record'][1]['reason'] = terminal
        job = NS(job_id=name, status='failed' if name == 'first' else 'completed',
                 exit_code=1 if name == 'first' else 0, stdout='', stderr='',
                 served_model=name, served_family='synthetic', served_witness_reason='', events=[])
        setattr(job, stream, ('error: ' + LINE if stream == 'stdout' else LINE)
                if name == 'first' else 'review complete')
        if name != 'first':
            job.stdout += '\nVERDICT: pass\n'
        runner.jobs[name] = job
        return job

    runner.spawn = spawn
    service = SpawnService(runner, state=tmp_path, leg_reader=store)
    monkeypatch.setattr('plangraph.spawner.context_pack.annotate', lambda *a: None)
    monkeypatch.setattr(service, '_leg_prompt', lambda *a, **kw: 'review')
    record = asyncio.run(service._spawn(card={'title': 'synthetic'}, role='review',
        wait_seconds=None, _plan=SpawnPlan(legs, (), 'test', (), ())))
    if terminal:
        assert calls == ['first']
        assert record['status'] == 'failed' and record['reason'] == terminal
        assert store.leg_status('first')['state'] == 'ok'
        assert [a['status'] for a in record['attempts']] == ['failed', 'skipped', 'skipped']
        assert all(a['skip_reason'] == 'run_terminated_before_leg' for a in record['attempts'][1:])
        return
    assert calls == ['first', 'next']
    assert store.leg_status('first')['state'] == 'limited'
    assert record['status'] == 'completed' and 'reason' not in record
    assert [(a['leg']['id'], a['status'], a['skip_reason']) for a in record['attempts']] == [
        ('first', 'failed', 'harness_limit: ' + ('error: ' if stream == 'stdout' else '') + LINE),
        ('next', 'completed', None), ('unused', 'skipped', 'earlier_leg_succeeded')]


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('text,code', [(LINE, 0), ('The model said: "' + LINE + '"', 1),
    ('Retry log: ' + LINE, 1), ('usage limit reached, retrying', 1),
    (LINE + ' Retrying now.', 1)])
def test_normal_output_is_not_a_limit(stream, text, code):
    assert harness_limit_line(code, **{stream: text, 'stderr' if stream == 'stdout' else 'stdout': ''}) is None


@pytest.mark.parametrize('reason', ['git_config_mutated', 'writer_no_changes', 'cancelled',
                                   'kill_incomplete', 'model_refusal'])
def test_terminal_verdict_stops_fallthrough(reason):
    record = dict(reason=reason, status='failed', exit_code=1)
    job = NS(exit_code=1, stdout=LINE, stderr='', events=[])
    assert fallthrough_reason(None, None, job, record) is None
    terminal_reason(record)
    assert record['reason'] == reason
