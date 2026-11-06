"""OpenCode completion evidence, not seat verdict syntax, owns exit outcome."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.runner import JobInfo, SpawnerRunner


def _monitor(tmp_path, monkeypatch, output, stderr='Error: terminated', rc=1):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo('answer-job', 'running', 'task', 'test-model', role='review',
                  command=['opencode'], cwd=str(tmp_path))
    runner.jobs[job.job_id] = job
    monkeypatch.setattr('plangraph.spawner.runner._opencode_tools_denied', lambda *args: False)

    async def collect(*args):
        return output.encode(), stderr.encode()

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *args: None)
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.fail_unchanged_writer', lambda *args: None)
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
