"""OpenCode completion evidence, not seat verdict syntax, owns exit outcome."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import JobInfo, SpawnerRunner


def _monitor(tmp_path, monkeypatch, output, stderr='Error: terminated', rc=1,
             role='review', denied=False):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo('answer-job', 'running', 'task', 'test-model', role=role,
                  command=['opencode'], cwd=str(tmp_path))
    runner.jobs[job.job_id] = job
    if denied:
        job.events.append({'event': 'opencode_tools_denied'})
    monkeypatch.setattr('plangraph.spawner.runner._opencode_tools_denied', lambda *args: False)

    async def collect(*args):
        return output.encode(), stderr.encode()

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *args: None)
    asyncio.run(runner._monitor_process(job.job_id, SimpleNamespace(returncode=rc), 'test-model'))
    assert job.exit_code == rc
    return job


def stream(*events):
    return '\n'.join(json.dumps(dict(type=kind, part=part)) for kind, part in events)


@pytest.mark.parametrize('stderr', ['Error: terminated',
                                  '\x1b[91mError: \x1b[0mterminated\n> build · model'])
def test_complete_answer_then_termination_is_distinct(tmp_path, monkeypatch, stderr):
    output = stream(('text', {'text': 'A complete generic answer.'}),
                    ('step_finish', {'reason': 'stop'}), ('step_start', {}))
    job = _monitor(tmp_path, monkeypatch, output, stderr=stderr)
    assert job.status == 'terminated_after_answer'
    assert {'event': 'terminated_after_answer'} in job.events
    assert not any(e['event'] == 'harness_failed' for e in job.events)
    assert job.stdout == 'A complete generic answer.'


def test_truncated_verdict_is_not_promoted(tmp_path, monkeypatch):
    job = _monitor(tmp_path, monkeypatch, 'VERDICT: ACCEPT')
    assert job.status == 'failed'
    assert not any(e['event'] == 'terminated_after_answer' for e in job.events)


@pytest.mark.parametrize('reason', ['length', 'tool-calls', None])
def test_unfinished_stream_stays_failed(tmp_path, monkeypatch, reason):
    output = stream(('text', {'text': 'VERDICT: ACC'}), ('step_finish', {'reason': reason}))
    assert _monitor(tmp_path, monkeypatch, output).status == 'failed'


def test_other_error_after_answer_stays_failed(tmp_path, monkeypatch):
    output = stream(('text', {'text': 'Answer'}), ('step_finish', {'reason': 'stop'}))
    assert _monitor(tmp_path, monkeypatch, output, stderr='Error: authentication').status == 'failed'


def test_command_requests_json_completion_frames(tmp_path):
    job = asyncio.run(SpawnerRunner(default_cwd=str(tmp_path)).spawn(
        'Answer this task', model_or_combo='mimo/mimo-v2.6-flash', dry_run=True))
    assert job.command[job.command.index('--format') + 1] == 'json'


@pytest.mark.parametrize('reason, expected', [('stop', 'terminated_after_answer'),
                                             ('length', 'failed')])
def test_writer_completion_evidence(tmp_path, monkeypatch, reason, expected):
    output = stream(('text', {'text': 'Implemented the task.'}),
                    ('step_finish', {'reason': reason}))
    job = _monitor(tmp_path, monkeypatch, output, role='implement')
    assert job.status == expected


@pytest.mark.parametrize('verdict', ['VERDICT: ACCEPT', 'VERDICT: REQUEST_CHANGES'])
@pytest.mark.parametrize('framing', ['plain', 'stop', 'length'])
def test_denied_review_verdict_completion(tmp_path, monkeypatch, verdict, framing):
    output = verdict if framing == 'plain' else stream(
        ('text', {'text': verdict}), ('step_finish', {'reason': framing}))
    job = _monitor(tmp_path, monkeypatch, output, denied=True, stderr='')
    assert job.status == ('failed' if framing == 'length' else 'completed')


def test_monitor_writer_hold(tmp_path, monkeypatch):
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools/preflight.py').touch()
    from plangraph.spawner import git_guard, notify
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo('hold-job', 'running', 'task', 'test-model', role='fix',
                  command=['opencode'], cwd=str(tmp_path))
    runner.jobs[job.job_id] = job
    async def collect(*args):
        return stream(('text', {'text': 'Done'}), ('step_finish', {'reason': 'stop'})).encode(), b''
    async def reap(*args):
        pass
    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *a: None)
    monkeypatch.setattr(git_guard.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout='HOLD trailers\n'))
    asyncio.run(runner._monitor_process(job.job_id, SimpleNamespace(returncode=0), 'test-model'))
    assert job.status == 'hold' and job.exit_code == 1
    assert job.stderr == 'HOLD trailers'
    assert dict(event='writer_hold', reason='HOLD trailers') in job.events
    monkeypatch.setattr(notify.subprocess, 'check_output', lambda *a, **k: 'abc')
    assert notify.completion_event(dict(run_id='run-test', status=job.status,
                                        exit_code=job.exit_code), tmp_path)['event'] == 'HOLD'
def test_brief_collection_refuses_before_writer(monkeypatch, tmp_path):
    from plangraph.spawner.git_guard import collect_brief_tests
    from plangraph.routing_errors import SpawnRefused
    from types import SimpleNamespace
    import pytest
    calls = []

    def collect(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=2, stdout='collection failed: missing PG table', stderr='fcntl unavailable')

    monkeypatch.setattr('plangraph.spawner.git_guard.subprocess.run', collect)
    with pytest.raises(SpawnRefused, match='test_env_unready:.*missing PG table.*fcntl'):
        collect_brief_tests('Run tests/test_host.py::test_schema', 'implement', tmp_path)
    assert len(calls) == 1
    assert calls[0][0][1:] == ['-m', 'pytest', '--collect-only', '-q', 'tests/test_host.py::test_schema']
    assert calls[0][1]['cwd'] == tmp_path
