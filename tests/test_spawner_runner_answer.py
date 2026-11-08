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
def test_oom_adjustment_never_lowers_and_records_denial(tmp_path, monkeypatch):
    from plangraph.spawner.proc_kill import raise_oom_score
    path = tmp_path / 'oom_score_adj'
    for current, expected in [(-900, 500), (750, 750)]:
        path.write_text(str(current))
        assert raise_oom_score(path)['event'] == 'oom_score_adj'
        assert int(path.read_text()) == expected
    def denied(*args, **kwargs):
        raise PermissionError(13, 'denied')
    monkeypatch.setattr(type(path), 'write_text', denied)
    monkeypatch.setattr(type(path), 'read_text', lambda self: '-900')
    assert raise_oom_score(path)['event'] == 'oom_score_adj_denied'


def test_linux_child_oom_inherits_before_exec(tmp_path):
    import asyncio
    import sys
    import pytest
    from plangraph.spawner.proc_kill import oom_process_factory
    if not sys.platform.startswith('linux'):
        pytest.skip('Linux proc policy')
    async def run():
        path = tmp_path / 'score'
        path.write_text('-900')
        proc = await oom_process_factory(
            sys.executable, '-c',
            "import subprocess,sys; print(open(sys.argv[1]).read().strip()); "
            "subprocess.run(['cat', sys.argv[1]], check=True)", str(path),
            oom_path=path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        assert proc.returncode == 0, err
        assert all(int(score) >= 500 for score in out.splitlines())
        assert b'oom_score_adj' in err
    asyncio.run(run())


def test_oom_deploy_contract():
    import configparser
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    unit = configparser.ConfigParser(interpolation=None)
    text = (root / 'deploy/systemd/claude-daemon.service').read_text()
    unit.read_string(text)
    assert int(unit['Service']['OOMScoreAdjust']) > -900
    assert unit['Service']['KillMode'] == 'process'
    assert not re.search(r'(?i)(api[_-]?key|token|password|secret)\s*=', text)
    docs = (root / 'deploy/oom-policy.md').read_text()
    assert 'systemd-run --user --scope' in docs
    assert 'claude --bg' in docs
    assert 'Not deployed' in docs
    assert 'second authenticated session' in docs
    assert 'min(4, CPUs, memory GiB)' in docs
    for name, matches in [('prefer', ['python3', 'pytest', 'opencode']),
                          ('avoid', ['herdr', 'claude', 'plangraph-keeper'])]:
        pattern = (root / f'deploy/earlyoom/{name}.regex').read_text().strip()
        assert all(re.search(pattern, value) for value in matches)


def test_pytest_memory_policy(tmp_path):
    import conftest
    mem = tmp_path / 'meminfo'
    mem.write_text('MemTotal:       15728640 kB\n')
    assert conftest._oom_worker_count(16, mem) == 4
    assert conftest._oom_worker_count(2, mem) == 2
    mem.write_text('MemTotal:       67108864 kB\n')
    assert conftest._oom_worker_count(16, mem) == 4
    mem.write_text('MemTotal:       2097152 kB\n')
    assert conftest._oom_worker_count(16, mem) == 2


def test_oom_factory_preserves_preexec_and_tolerates_bad_score(tmp_path, monkeypatch):
    from plangraph.spawner import proc_kill
    path = tmp_path / 'score'
    path.write_text('invalid')
    assert proc_kill.raise_oom_score(path)['event'] == 'oom_score_adj_failed'
    seen = {}
    async def spawn(*args, **kwargs):
        seen.update(kwargs)
    callback = lambda: None
    monkeypatch.setattr(proc_kill.asyncio, 'create_subprocess_exec', spawn)
    asyncio.run(proc_kill.oom_process_factory('true', preexec_fn=callback, oom_path=path))
    assert seen['preexec_fn'] is callback


def test_oom_worker_override(tmp_path, monkeypatch):
    import conftest
    monkeypatch.setenv('PYTEST_XDIST_AUTO_NUM_WORKERS', '2')
    assert conftest._oom_worker_count(16, tmp_path / 'missing') == 2


def test_oom_serial_configure(monkeypatch):
    import conftest
    from plangraph.spawner import proc_kill
    calls = []
    monkeypatch.setattr(proc_kill, 'raise_oom_score', lambda: calls.append(1) or {'event': 'oom_score_adj'})
    conftest.pytest_oom_configure(SimpleNamespace())
    assert calls == [1]


def test_rust_oom_envelope_is_recorded(tmp_path, monkeypatch):
    event = {'event': 'oom_score_adj_denied'}
    job = _monitor(tmp_path, monkeypatch, '', stderr=json.dumps({'plangraph_oom': event}))
    assert event in job.events
