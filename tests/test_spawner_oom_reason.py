"""The OOM wrapper's telemetry is an event, never stderr content or a failure reason."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.spawner.exit_outcome import failure_reason, terminal_reason
from plangraph.spawner.runner import JobInfo, SpawnerRunner

OOM = '{"plangraph_oom": {"event": "oom_score_adj", "value": 500}}\n'


def run_job(tmp_path, monkeypatch, name, stderr):
    runner = SpawnerRunner(default_cwd=str(tmp_path))
    job = JobInfo(name, 'running', 'writer', 'm', role='writer', command=['claude'], cwd=str(tmp_path))
    runner.jobs[name] = job

    async def collect(*args):
        return b'', stderr.encode()

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    monkeypatch.setattr(runner, '_reap_process', reap)
    monkeypatch.setattr(runner, '_check_git_config_guard', lambda *a: None)
    monkeypatch.setattr('plangraph.spawner.opencode_isolation.fail_unchanged_writer', lambda *a: None)
    asyncio.run(runner._monitor_process(name, SimpleNamespace(returncode=1), 'm'))
    return job


def test_oom_line_is_event_only_and_each_attempt_keeps_its_reason(tmp_path, monkeypatch):
    attempts = []
    for i, real in enumerate(('error: model not found', 'error: auth rejected')):
        job = run_job(tmp_path, monkeypatch, f'j{i}', OOM + real + '\n')
        assert {'event': 'oom_score_adj'} in job.events
        assert 'plangraph_oom' not in job.stderr
        assert job.stderr == real + '\n'
        attempts.append(dict(status='failed', skip_reason=failure_reason(job), stderr_tail=job.stderr))
    assert [a['skip_reason'] for a in attempts] == ['error: model not found', 'error: auth rejected']
    record = dict(status='failed', exit_code=1, attempts=attempts,
                  stderr_tail=''.join(a['stderr_tail'] for a in attempts))
    terminal_reason(record)
    assert record['reason'] == 'error: model not found'
    assert 'plangraph_oom' not in record['stderr_tail']


def test_oom_only_stderr_gives_exit_code_reason_not_telemetry(tmp_path, monkeypatch):
    job = run_job(tmp_path, monkeypatch, 'j', OOM)
    assert job.stderr == '' and {'event': 'oom_score_adj'} in job.events
    assert failure_reason(job) == 'harness exit code 1'
