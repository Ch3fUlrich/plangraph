import asyncio
import os
import subprocess
from types import SimpleNamespace

from plangraph.spawner.env_redact import first_reason, harness_limit_line
from plangraph.spawner.git_guard import writer_launch
from plangraph.spawner.exit_outcome import fallthrough_reason, terminal_reason
from plangraph.routing_support.leg_state import LegStateStore


QUOTA = 'error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 10h40m26s.'


def test_quota_state_and_compound_reset(tmp_path, monkeypatch):
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    job = SimpleNamespace(exit_code=3, stdout='', stderr='\x1b[0m\n' + QUOTA)
    record = {}
    reason = fallthrough_reason(SimpleNamespace(leg_reader=store),
                                SimpleNamespace(model='agy/model'), job, record)
    assert reason == 'harness_limit: ' + QUOTA
    assert record['reason'] == 'harness_limit'
    state = store.leg_status('agy/model', now=1000)
    assert state['state'] == 'limited'
    assert state['evidence']['reset_at'] == 1000 + 38426
    store.record_harness_limit('agy/other', 'Resets in 2h 5m', 'test', now=1000)
    assert store.leg_status('agy/other', now=1000)['evidence']['reset_at'] == 8500
    assert first_reason('\x1b[0m\n\nactual error') == 'actual error'
    assert harness_limit_line(0, '', QUOTA) is None
    # Agent prose on stdout never marks a leg; an error-shaped stdout line does.
    assert harness_limit_line(1, 'I added the rate limit parser; quota reached handling', '') is None
    assert harness_limit_line(1, 'error: ' + QUOTA, '') == 'error: ' + QUOTA


def test_writer_identity_commits_with_writer_committer(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    prompt, extra = writer_launch('work', 'implement', 'agy/model', 'run-123')
    assert prompt.endswith('Writer: agy/model run-123')

    async def fake_writer():
        process = await asyncio.create_subprocess_exec(
            'git', 'commit', '--allow-empty', '-m', 'x', cwd=tmp_path,
            env={**os.environ, 'GIT_AUTHOR_NAME': 'Host', 'GIT_COMMITTER_NAME': 'Host', **extra},
            stdout=asyncio.subprocess.PIPE)
        await process.communicate()
        assert process.returncode == 0

    asyncio.run(fake_writer())
    identity = subprocess.check_output(
        ['git', 'log', '-1', '--format=%an|%ae|%cn|%ce'], cwd=tmp_path).decode().strip()
    assert identity == ('agy/model (plangraph writer)|run-123@writer.plangraph.invalid'
                        '|agy/model (plangraph writer)|run-123@writer.plangraph.invalid')
    assert writer_launch('review', 'review', 'agy/model', 'run-123') == ('review', {})


def test_quota_key_is_harness_aware(tmp_path):
    """Q16/M8: a harness limit is booked under leg_probe.key semantics --
    'agy/<model>' only for an agy leg's bare model."""
    from plangraph.spawner.leg_probe import key
    store = LegStateStore(tmp_path / 'legs.sqlite')
    record = {}
    reason = fallthrough_reason(SimpleNamespace(leg_reader=store),
                                SimpleNamespace(model='Qwen3.8-Flash', harness='qodercli'),
                                SimpleNamespace(exit_code=3, stdout='', stderr=QUOTA), record)
    assert reason.startswith('harness_limit:')
    assert record['reason'] == 'harness_limit'
    assert key(SimpleNamespace(model='Qwen3.8-Flash', harness='qodercli')) == 'Qwen3.8-Flash'
    assert store.leg_status('Qwen3.8-Flash')['state'] == 'limited'
    assert store.leg_status('agy/Qwen3.8-Flash')['state'] == 'ok'
    fallthrough_reason(SimpleNamespace(leg_reader=store),
                       SimpleNamespace(model='gemini-3.8-flash', harness='agy'),
                       SimpleNamespace(exit_code=3, stdout='', stderr=QUOTA), {})
    assert store.leg_status('agy/gemini-3.8-flash')['state'] == 'limited'


def test_terminal_reason_is_the_final_attempt_outcome():
    """Q8/M15: the run's reason describes how the run ENDED. A 'harness_limit'
    stamped by an earlier fallthrough leg never survives a later outcome."""
    record = dict(exit_code=1, reason='harness_limit', events=[], attempts=[
        dict(status='failed', skip_reason='harness_limit: ' + QUOTA),
        dict(status='failed', skip_reason='provider_stop')])
    terminal_reason(record)
    assert record['reason'] == 'provider_stop'
    record = dict(exit_code=1, reason='harness_limit', events=[], attempts=[
        dict(status='failed', skip_reason='harness_limit: ' + QUOTA),
        dict(status='timeout', skip_reason=None)])
    terminal_reason(record)
    assert record['reason'] == 'timeout'
    # A run that ended ON the quota leg keeps 'harness_limit'.
    record = dict(exit_code=4, reason='harness_limit', events=[],
                  attempts=[dict(status='failed', skip_reason='harness_limit: ' + QUOTA)])
    terminal_reason(record)
    assert record['reason'] == 'harness_limit'


def test_guard_reasons_are_never_stomped():
    """M9/M15: git_config_mutated and writer_no_changes become the run's reason,
    and no later fallthrough may stomp them with 'harness_limit'."""
    for reason in ('git_config_mutated', 'writer_no_changes'):
        record = {'reason': reason}
        fallthrough_reason(SimpleNamespace(leg_reader=None), SimpleNamespace(model='agy/model'),
                           SimpleNamespace(exit_code=3, stdout='', stderr=QUOTA), record)
        assert record['reason'] == reason
    record = dict(exit_code=5, reason='harness_limit',
                  events=[{'event': 'writer_no_changes'}], attempts=[])
    terminal_reason(record)
    assert record['reason'] == 'writer_no_changes'


def test_routing_reads_a_bare_non_agy_leg_under_its_own_key(tmp_path):
    """Q16 reader side: a qodercli harness limit booked under the bare model is
    seen by the dispatch reader; an agy limit under 'agy/<model>' still is."""
    from plangraph.spawner.leg_probe import read_status
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit('Qwen3.8-Flash', QUOTA, 'test')
    store.record_harness_limit('agy/gemini-3.8-flash', QUOTA, 'test')
    assert read_status(store.leg_status, 'Qwen3.8-Flash')['state'] == 'limited'
    assert read_status(store.leg_status, 'gemini-3.8-flash')['state'] == 'limited'
    assert read_status(store.leg_status, 'other-model')['state'] == 'ok'


def test_orphan_check_never_signals_on_windows(tmp_path, monkeypatch):
    """os.kill(pid, 0) is TerminateProcess on Windows: the orphan check must not call it."""
    from plangraph.spawner import exit_outcome
    log = tmp_path / 'supervisor.stderr.log'
    (tmp_path / 'supervisor.stderr.log.pid').write_text('12345')
    calls = []
    monkeypatch.setattr(os, 'kill', lambda *a: calls.append(a))
    monkeypatch.setattr(os, 'name', 'nt')
    record = {'status': 'running', 'ended': None, 'supervisor_stderr_path': str(log)}
    assert exit_outcome.orphaned(record, 1.0) is None
    assert calls == []
