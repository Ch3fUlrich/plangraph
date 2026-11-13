"""OpenCode completion evidence, not seat verdict syntax, owns exit outcome."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import importlib.util
from pathlib import Path

from plangraph.spawner.runner import JobInfo, SpawnerRunner


def _oom_config():
    spec = importlib.util.spec_from_file_location('oom_test_config', Path(__file__).with_name('conftest.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        path = Path('/proc/self/oom_score_adj')
        before = path.read_text()
        proc = await oom_process_factory(
            sys.executable, '-c',
            "import subprocess,sys; print(open(sys.argv[1]).read().strip()); "
            "subprocess.run(['cat', sys.argv[1]], check=True)", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        assert proc.returncode == 0, err
        assert len(out.splitlines()) == 2
        assert all(int(score) >= 500 for score in out.splitlines())
        assert path.read_text() == before
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
    assert int(unit['Service']['OOMScoreAdjust']) == 200
    assert unit['Service']['KillMode'] == 'process'
    assert not re.search(r'(?i)(api[_-]?key|token|password|secret)\s*=', text)
    docs = (root / 'deploy/oom-policy.md').read_text()
    assert 'systemd-run --user --scope' in docs
    assert 'claude --bg' in docs
    assert 'Not deployed' in docs
    assert 'second authenticated session' in docs
    assert 'min(4, CPUs, memory GiB)' in docs
    for name, matches in [('prefer', ['python3', 'pytest', 'opencode']),
                          ('avoid', ['herdr', 'claude', 'plangraph-keeper'[:15]])]:
        pattern = (root / f'deploy/earlyoom/{name}.regex').read_text().strip()
        assert all(re.search(pattern, value) for value in matches)
        assert not re.search(pattern, 'x' + matches[0])


def test_pytest_memory_policy(tmp_path):
    conftest = _oom_config()
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
    conftest = _oom_config()
    monkeypatch.setenv('PYTEST_XDIST_AUTO_NUM_WORKERS', '2')
    assert conftest._oom_worker_count(16, tmp_path / 'missing') == 2


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32'])
def test_oom_serial_configure(monkeypatch, platform):
    conftest = _oom_config()
    from plangraph.spawner import proc_kill
    calls = []
    monkeypatch.setattr(conftest.sys, 'platform', platform)
    monkeypatch.setattr(proc_kill, 'raise_oom_score', lambda: calls.append(1) or {'event': 'oom_score_adj'})
    conftest.pytest_oom_configure(SimpleNamespace())
    assert calls == ([1] if platform == 'linux' else [])


def test_rust_oom_envelope_is_recorded(tmp_path, monkeypatch):
    event = {'event': 'oom_score_adj_denied'}
    job = _monitor(tmp_path, monkeypatch, '', stderr=json.dumps({'plangraph_oom': event}))
    assert event in job.events


def test_oom_envelopes_are_bounded_and_validated(tmp_path, monkeypatch):
    valid = json.dumps({'plangraph_oom': {'event': 'oom_score_adj_failed'}})
    invalid = [json.dumps({'plangraph_oom': value}) for value in [None, [], 'bad', {'event': 'unknown'}]]
    job = _monitor(tmp_path, monkeypatch, '', stderr='\n'.join([valid] * 1000 + invalid))
    assert [e for e in job.events if e.get('event', '').startswith('oom_score_adj')] == [
        {'event': 'oom_score_adj_failed'}]


@pytest.mark.parametrize('override, expected', [('bad', 2), ('99', 2), ('0', 1)])
def test_oom_override_is_safe(tmp_path, monkeypatch, override, expected):
    monkeypatch.setenv('PYTEST_XDIST_AUTO_NUM_WORKERS', override)
    mem = tmp_path / 'meminfo'
    mem.write_text('MemTotal: 2097152 kB\n')
    assert _oom_config()._oom_worker_count(16, mem) == expected


@pytest.mark.parametrize('args', [[], ['score'], ['score', '/missing/oom-command']])
def test_oom_wrapper_diagnoses_failure(tmp_path, args):
    import subprocess
    import sys
    from plangraph.spawner import proc_kill
    (tmp_path / 'score').write_text('500')
    result = subprocess.run([sys.executable, proc_kill.__file__, *args],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode in (2, 127)
    assert 'oom wrapper:' in result.stderr
    assert 'Traceback' not in result.stderr


def test_linux_oom_call_is_guarded():
    from plangraph.spawner import proc_kill
    from unittest.mock import patch, AsyncMock
    with patch.object(proc_kill.sys, 'platform', 'darwin'), patch.object(
            proc_kill.asyncio, 'create_subprocess_exec', new_callable=AsyncMock) as spawn:
        asyncio.run(proc_kill.oom_process_factory('true'))
        assert spawn.call_args.args == ('true',)
    source = (Path(__file__).resolve().parents[1] / 'rust/worker-core/src/run/group_unix.rs').read_text()
    assert '#[cfg(target_os = "linux")]\n    unsafe {\n        cmd.pre_exec' in source


def test_inheritance_tests_use_proc():
    import inspect
    root = Path(__file__).resolve().parents[1]
    rust = (root / 'rust/worker-core/src/run/group_tests.rs').read_text()
    assert 'cat /proc/self/oom_score_adj' in rust
    assert '/proc/self/oom_score_adj' in inspect.getsource(test_linux_child_oom_inherits_before_exec)


@pytest.mark.parametrize('signal', ['length', 'stdout-error', 'stderr-error'])
@pytest.mark.parametrize('padding', [None, '', '\n', ' \t'])
def test_reasoning_limit_telemetry_survives_monitor(tmp_path, monkeypatch, signal, padding):
    output = stream(('step_finish', {'reason': 'length',
                                    'tokens': {'output': 0, 'reasoning': 32768}}))
    error = 'Response still truncated after two output token limit continuations'
    stderr = ''
    if signal == 'stdout-error':
        output += '\n' + json.dumps({'type': 'error', 'error': {'type': 'output-limit', 'message': error}})
    elif signal == 'stderr-error':
        output, stderr = '', 'Error: output-limit: ' + error
    if padding is not None:
        output = padding + '\n' + output + '\n' + padding
    job = _monitor(tmp_path, monkeypatch, output, stderr=stderr)
    assert any(e['event'] == 'opencode_reasoning_exhausted' for e in job.events)
    if signal != 'stderr-error':
        step = next(e for e in job.events if e['event'] == 'opencode_step_finish')
        assert (step['finish_reason'], step['output_tokens'], step['reasoning_tokens']) == ('length', 0, 32768)


@pytest.mark.parametrize('part', [
    {'reason': 'length'}, {'reason': 'length', 'tokens': {'output': False}},
    {'reason': 'length', 'tokens': {'output': 1}},
    {'reason': 'stop', 'tokens': {'output': 0}},
])
def test_reasoning_limit_requires_explicit_zero_length(tmp_path, monkeypatch, part):
    job = _monitor(tmp_path, monkeypatch, stream(('step_finish', part)), stderr='')
    assert not any(e['event'] == 'opencode_reasoning_exhausted' for e in job.events)


def test_later_answer_recovers_reasoning_exhaustion(tmp_path, monkeypatch):
    output = stream(('step_finish', {'reason': 'length', 'tokens': {'output': 0}}),
                    ('text', {'text': 'Complete answer'}),
                    ('step_finish', {'reason': 'stop', 'tokens': {'output': 3}}))
    job = _monitor(tmp_path, monkeypatch, output, stderr='', rc=0)
    assert job.status == 'completed' and job.stdout == 'Complete answer'
    assert not any(e['event'] == 'opencode_reasoning_exhausted' for e in job.events)


@pytest.mark.parametrize('framing', ['plain', 'mixed', 'text'])
def test_display_answer_prevents_reasoning_retry(tmp_path, monkeypatch, framing):
    from plangraph.spawner.opencode_answer import answer_text
    verdict = 'Review complete\nTEST-GAMING: no - searched tests\nVERDICT: ACCEPT'
    diagnostic = stream(('step_finish', {'reason': 'length', 'tokens': {'output': 0}}))
    output = (stream(('text', {'text': verdict})) if framing == 'text' else verdict)
    if framing != 'plain':
        output = diagnostic + '\n' + output
    assert answer_text(output, raw_fallback=False).strip()
    job = _monitor(tmp_path, monkeypatch, output, stderr='Error: output-limit', denied=True)
    assert job.stdout == answer_text(output) and verdict in job.stdout
    assert not any(e['event'] == 'opencode_reasoning_exhausted' for e in job.events)


@pytest.mark.parametrize('config', [None, [], {'providers': []}, {'providers': {'mimo': None}},
    {'providers': {'mimo': {'package': '@opencode/ai/providers/openai-compatible',
                          'models': {'mimo-v2.6-flash': {'body': []}}}}}])
def test_reasoning_cap_malformed_config_unavailable(config):
    from plangraph.spawner.opencode_answer import cap_reasoning
    assert not cap_reasoning({'OPENCODE_CONFIG_CONTENT': json.dumps(config)}, 'mimo/mimo-v2.6-flash')


@pytest.mark.parametrize('status', ['timeout', 'stalled', 'cancelled'])
def test_terminal_record_never_appends_attempt(status):
    from plangraph.spawner.run_watchdog import terminal_record
    attempt = dict(status='failed', skip_reason='reasoning_output_limit')
    record = dict(role='review', attempts=[attempt], events=[])
    job = SimpleNamespace(status=status, exit_code=124, events=[dict(event='watchdog_' + status)])
    assert terminal_record(record, job) == (status != 'cancelled')
    assert record['attempts'] == [attempt]


@pytest.mark.parametrize('count', [0, 12])
def test_assignment_text_keeps_raw_integer_usage(tmp_path, monkeypatch, count):
    text = 'MY_' + 'SECRET=V1'
    output = stream(('text', {'text': text}), ('step_finish', {'reason': 'length', 'tokens': {'output': count, 'reasoning': count}}))
    job = _monitor(tmp_path, monkeypatch, output, stderr='')
    step = next(e for e in job.events if e['event'] == 'opencode_step_finish')
    assert (step['output_tokens'], step['reasoning_tokens']) == (count, count)
    assert job.stdout == 'MY_' + 'SECRET=[REDACTED]'
    assert not any(e['event'] == 'opencode_reasoning_exhausted' for e in job.events)
