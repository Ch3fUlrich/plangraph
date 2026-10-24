import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from plangraph.spawner import cli
from plangraph.spawner.runner import JobInfo, spawn_service


def test_supervisor_imports_without_install(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('PYTHONPATH', raising=False)
    # An invalid explicit leg reaches the real supervisor and returns its JSON refusal.
    assert cli.main(['--state', str(tmp_path / 'state'), 'spawn', 'x', '--card', '{"estimate_tokens":100}',
                     '--leg', 'missing', '--override-reason', 'test']) == 2
    assert json.loads(capsys.readouterr().out)['error'] == 'unknown leg'


def test_supervisor_import_error_is_visible(tmp_path, monkeypatch, capsys):
    def start(command, **kwargs):
        return subprocess.Popen([sys.executable, '-c',
            'import sys; sys.stdin.readline(); raise ImportError("broken sk-testsecret")'], **kwargs)
    monkeypatch.setattr(cli, '_start_supervisor', start)
    assert cli.main(['--state', str(tmp_path), 'spawn', 'x', '--card', '{"estimate_tokens":100}',
                     '--leg', 'missing', '--override-reason', 'test']) == 1
    error = capsys.readouterr().err
    assert 'ImportError: broken' in error
    assert 'sk-testsecret' not in error


@pytest.mark.parametrize('padding', ['', 'é' * 5000])
def test_failed_harness_record_tails(tmp_path, monkeypatch, padding):
    from plangraph.planner.legs import DEFAULT_LEGS
    leg = DEFAULT_LEGS[0]
    job = JobInfo('job-test', 'failed', 'x', leg.model, exit_code=1,
                  stdout='x' * 5000 + ' output',
                  stderr='Error: Model unavailable: x sk-testsecret\n' + padding)
    async def spawn(**kwargs):
        return job
    runner = SimpleNamespace(spawn=spawn, _processes={}, _tasks={}, provider_responses={})
    service = spawn_service(state=tmp_path, runner=runner)
    plan = SimpleNamespace(legs=[leg], error=None, to_dict=lambda: {'skipped': []})
    monkeypatch.setattr(service, '_route', lambda *a, **kw: plan)
    record = asyncio.run(service.spawn(card={'estimate_tokens': 100}, prompt='x', wait_seconds=None))
    stored = json.loads(service._path(record['run_id']).read_text())
    assert stored['stdout_tail'].endswith(' output')
    assert stored['stderr_tail']
    if not padding:
        assert 'Error: Model unavailable: x [REDACTED]' in stored['stderr_tail']
    assert len(stored['stdout_tail'].encode()) <= 4096
    assert len(stored['stderr_tail'].encode()) <= 4096
    assert stored['attempts'][0]['skip_reason'] == 'Error: Model unavailable: x [REDACTED]'
    assert 'sk-testsecret' not in json.dumps(stored)


def test_override_record_and_watchdog_compatibility(tmp_path):
    from plangraph.spawner import stall_watchdog
    from plangraph.spawner.notify import completion_event
    job = JobInfo('override', 'failed', 'x', 'test/model', exit_code=1,
                  stdout='harness output', stderr='Error: Model unavailable: x')
    service = spawn_service(state=tmp_path, runner=SimpleNamespace(jobs={job.job_id: job}))
    record = dict(run_id='run-override', job_id=job.job_id, status='failed', exit_code=1,
                  started=1, ended=2, attempts=[dict(status='failed', skip_reason=None)])
    service._save(record)
    stored = service.result(record['run_id'])
    assert stored['stderr_tail'] == job.stderr
    assert stored['stdout_tail'] == job.stdout
    assert stored['attempts'][0]['skip_reason'] == job.stderr
    assert stall_watchdog.read_runs(tmp_path)[0]['exit_code'] == 1
    assert completion_event(stored, str(tmp_path))['event'] == 'FAILED'
    assert completion_event(stored, str(tmp_path))['rc'] == 1
