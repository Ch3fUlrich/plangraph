from types import SimpleNamespace
import subprocess

import pytest

from plangraph.spawner.service import SpawnService
from plangraph.spawner.exit_outcome import terminal_reason
from plangraph.spawner.opencode_isolation import record_baseline, fail_unchanged_writer


@pytest.mark.parametrize('size', [12000, 1100000])
def test_spawner_full_stderr_retained(tmp_path, size):
    service = SpawnService(state=tmp_path)
    text = 'diagnostic start\n' + 'x' * size + '\nend diagnostic'
    record = dict(run_id='run-capture', status='failed', exit_code=5,
                  stderr_tail=text, attempts=[], ended=1)
    service._save(record)
    path = service._path(record['run_id']).with_name('stderr.log')
    assert path.exists()
    data = path.read_text()
    assert data.startswith('diagnostic start\n')
    assert len(path.read_bytes()) <= 1024 * 1024
    assert data == text if size < 1024 * 1024 else '[stderr truncated]' in data
    service._save(record)
    assert path.read_text() == data
    assert len(service.result(record['run_id'])['stderr_tail'].encode()) <= 4096


def test_spawner_exit1_after_plain_stdout():
    record = dict(status='failed', exit_code=1, stdout_tail='Finished the requested work.\n',
                  stderr_tail='', attempts=[], ended=1)
    terminal_reason(record)
    assert record['reason'] == 'exit1_after_stdout'


def test_spawner_skip_reason_no_ansi(tmp_path):
    service = SpawnService(state=tmp_path)
    record = dict(run_id='run-ansi', status='failed', exit_code=1,
                  attempts=[dict(status='failed', skip_reason='\x1b[31mError: failed\x1b[0m')])
    service._save(record)
    assert service.result('run-ansi')['attempts'][0]['skip_reason'] == 'Error: failed'


def test_spawner_no_changes_dirty_at_exit_keeps_stderr(tmp_path):
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    git('config', 'user.email', 'writer@example.test')
    git('config', 'user.name', 'Writer')
    (tmp_path / 'file').write_text('base')
    git('add', 'file')
    git('commit', '-m', 'base')
    (tmp_path / 'file').write_text('dirty')
    job = SimpleNamespace(role='implement', cwd=str(tmp_path), status='running', job_id='job',
                          events=[], writer_email='writer@example.test', stderr='original diagnostic\n' * 1000,
                          exit_code=0)
    record_baseline(job, tmp_path)
    job.status = 'completed'
    fail_unchanged_writer(job, tmp_path)
    assert job.status == 'completed'
    assert not any(e['event'] == 'writer_no_changes' for e in job.events)
    service = SpawnService(state=tmp_path / 'state')
    record = dict(run_id='run-dirty', status=job.status, exit_code=job.exit_code,
                  stderr_tail=job.stderr, events=job.events, ended=1)
    service._save(record)
    assert service._path('run-dirty').with_name('stderr.log').read_text() == job.stderr
