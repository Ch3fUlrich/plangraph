import asyncio
from types import SimpleNamespace

import pytest

from plangraph.spawner.env_redact import first_reason
from plangraph.spawner.exit_outcome import failure_reason, fallthrough_reason, terminal_reason
from plangraph.spawner.runner import SpawnerRunner


PROGRESS = ['✱ Grep "not_served_on_host|served_model" in src/plangraph · 63 matches',
            '→ Read src/plangraph', '> build · cx-gpt-6.1-sol', '$ git status']
FINAL = 'Final report: implemented the change; tests passed.'


def monitor(monkeypatch, stderr, stdout=FINAL, code=5, status='running',
            refusal=None, harness='opencode'):
    job = SimpleNamespace(command=[harness], status=status, exit_code=code,
                          survivors=[], refusal=refusal, events=[], role='implement',
                          cwd=None, served_model=None, served_family=None)

    async def collect(*args):
        return stdout.encode(), stderr.encode()

    async def reap(*args):
        pass

    monkeypatch.setattr('plangraph.spawner.run_watchdog.collect', collect)
    runner = SimpleNamespace(jobs={'j': job}, _nonces={}, _child_envs={'j': {}},
                             _git_guards={}, _stdin_payloads={}, _adapter_tokens={},
                             _processes={}, _ocdata_dirs={}, _reap_process=reap,
                             _check_git_config_guard=lambda _: None)
    asyncio.run(SpawnerRunner._monitor_process(runner, 'j', SimpleNamespace(returncode=code), 'm'))
    return job


@pytest.mark.parametrize('line', PROGRESS)
@pytest.mark.parametrize('ansi', [False, True])
def test_progress_never_becomes_error(monkeypatch, line, ansi):
    text = '\x1b[36m' + line + '\x1b[0m' if ansi else line
    job = monitor(monkeypatch, text)
    assert job.status == 'failed' and job.exit_code == 5
    assert first_reason(job.stderr) == ''
    assert fallthrough_reason(None, None, job, {}) is None
    assert (first_reason(job.stderr) or f'harness exit code {job.exit_code}') == 'harness exit code 5'


def test_real_error_wins(monkeypatch):
    job = monitor(monkeypatch, '\n'.join(PROGRESS + ['Error: transport failed']))
    assert job.status == 'failed'
    assert first_reason(job.stderr) == 'Error: transport failed'


@pytest.mark.parametrize('code', [1, 5])
@pytest.mark.parametrize('stdout', [FINAL, ''])
def test_nonzero_remains_failed_without_proven_final_protocol(monkeypatch, code, stdout):
    job = monitor(monkeypatch, '\n'.join(PROGRESS), stdout, code)
    assert job.status == 'failed' and job.exit_code == code
    assert job.stdout == stdout
    assert first_reason(job.stderr) == ''


@pytest.mark.parametrize('status,refusal', [('timeout', None), ('stalled', None),
                                          ('running', 'Error: quota exceeded')])
def test_watchdog_and_refusal_unchanged(monkeypatch, status, refusal):
    job = monitor(monkeypatch, PROGRESS[0] + '\nError: quota exceeded',
                  status=status, refusal=refusal)
    assert job.status == ('failed' if refusal else status)
    assert job.exit_code == 5 and job.refusal == refusal
    assert first_reason(job.stderr) == 'Error: quota exceeded'


@pytest.mark.parametrize('harness', ['claude', 'agy'])
def test_other_harnesses_byte_identical(monkeypatch, harness):
    text = '\x1b[36m' + '\n'.join(PROGRESS) + '\x1b[0m\n'
    job = monitor(monkeypatch, text, harness=harness)
    assert job.stderr == text and job.stdout == FINAL
    assert job.status == 'failed' and job.exit_code == 5


@pytest.mark.parametrize('code', [0, 5])
def test_no_verdict_preserves_captured_output(monkeypatch, code):
    text = '\x1b[36m' + '\n'.join(PROGRESS) + '\x1b[0m\n'
    job = monitor(monkeypatch, text, stdout=text, code=code)
    assert job.stdout == '' and job.stderr == ''
    assert job.status == ('completed' if code == 0 else 'failed')


def test_absolute_opencode_failure_evidence(monkeypatch):
    job = monitor(monkeypatch, PROGRESS[0], harness='/opt/bin/opencode')
    assert job.events == [{'event': 'harness_failed', 'error': 'harness exit code 5'}]
    assert failure_reason(job) == 'harness exit code 5'


@pytest.mark.parametrize('field', ['stderr_tail', 'error', 'reason'])
@pytest.mark.parametrize('chosen', [{}, {'harness': 'opencode'}])
def test_terminal_progress_is_not_failure_evidence(field, chosen):
    job = SimpleNamespace(command=('/opt/bin/opencode',))
    record = dict(status='failed', exit_code=5, chosen_leg=chosen)
    record[field] = PROGRESS[0] + '\nError: transport failed'
    terminal_reason(record, job)
    assert record['reason'] == 'Error: transport failed'
    if field == 'error':
        assert record['error'] == 'Error: transport failed'


def test_final_fallback_keeps_nonzero_failure(monkeypatch):
    job = monitor(monkeypatch, PROGRESS[0], stdout=FINAL)
    record = dict(status=job.status, exit_code=job.exit_code, stderr_tail=job.stderr)
    terminal_reason(record, job)
    assert record['status'] == 'failed' and record['exit_code'] == 5
    assert record['reason'] == 'harness exit 5'
