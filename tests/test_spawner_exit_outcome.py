import asyncio
import json
import os
import sqlite3
import subprocess
from pathlib import Path
import pytest
from types import SimpleNamespace

from plangraph.spawner.env_redact import first_reason, harness_limit_line
from plangraph.spawner.git_guard import writer_launch
from plangraph.spawner.exit_outcome import fallthrough_reason, terminal_reason
from plangraph.routing_support.leg_state import LegStateStore
from test_spawner_seat_fallthrough import CAPTURE, attempts_by_leg, recorded_plan, replay


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


def test_omniroute_reset_after_is_booked(tmp_path, monkeypatch):
    """D-3685: OmniRoute's '(reset after 3h 12m 5s)' books the parsed reset,
    which wins over the 1800 s down_s default."""
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    store = LegStateStore(tmp_path / 'legs.sqlite')
    line = 'Error: [codex/zz-model] quota threshold (reset after 3h 12m 5s)'
    job = SimpleNamespace(exit_code=1, stdout='', stderr='\x1b[0m\n' + line)
    reason = fallthrough_reason(SimpleNamespace(leg_reader=store),
                                SimpleNamespace(model='agy/zz-model'), job, {})
    assert reason == 'harness_limit: ' + line
    assert sqlite3.connect(str(store.path)).execute(
        'SELECT source, reset_at FROM leg_events').fetchall() == [('harness-stderr', 1000 + 11525)]
    assert store.leg_status('agy/zz-model', now=1000 + 3600)['state'] == 'limited'
    assert store.leg_status('agy/zz-model', now=1000 + 11526)['state'] == 'ok'


def test_explicit_zero_reset_books_nothing_through_fallthrough(tmp_path, monkeypatch):
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)
    monkeypatch.setenv('PLANGRAPH_LEG_DOWN_S', '1800')
    store = LegStateStore(tmp_path / 'legs.sqlite')
    line = 'Error: [codex/zz-model] quota threshold (reset after 0s)'
    job = SimpleNamespace(exit_code=1, stdout='', stderr=line)
    assert fallthrough_reason(SimpleNamespace(leg_reader=store),
                              SimpleNamespace(model='agy/zz-model'), job, {}) == 'harness_limit: ' + line
    assert store.leg_status('agy/zz-model', now=1000)['state'] == 'ok'
    assert not store.path.exists()  # No reset-less row or down_s fallback booking.


def test_harness_fallback_reset_is_capped(tmp_path):
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit('agy/model', 'error: quota threshold', 'test',
                               now=1000, down_s=172800)
    assert store.leg_status('agy/model', now=1000)['evidence']['reset_at'] == 87400


def test_writer_identity_commits_with_writer_committer(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    for key, value in [('user.name', 'Host'), ('user.email', 'host@example.com')]:
        subprocess.run(['git', '-C', str(tmp_path), 'config', key, value], check=True)
    prompt, extra = writer_launch('work', 'implement', 'agy/model', 'run-123')
    assert prompt.endswith('Writer: agy/model run-123')

    async def fake_writer():
        process = await asyncio.create_subprocess_exec(
            'git', 'commit', '--allow-empty', '-m', 'x', cwd=tmp_path,
            env={**os.environ, **extra}, stdout=asyncio.subprocess.PIPE)
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


@pytest.mark.parametrize('role', ['review', 'implement', 'fix'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'model_refusal'])
@pytest.mark.parametrize('reason', [None, 'git_config_mutated', 'cancelled', 'kill_incomplete', 'other_diagnostic'])
def test_refusal_classification_does_not_rewrite_run_reason(role, status, reason):
    record = dict(role=role, status=status, reason=reason)
    job = SimpleNamespace(status=status, role=role, exit_code=1, stdout='', stderr=QUOTA,
                          events=[dict(event='model_refusal')])
    assert fallthrough_reason(SimpleNamespace(leg_reader=None), None, job, record) is None
    assert record == dict(role=role, status=status, reason=reason)


@pytest.mark.parametrize('prefix,evidence', [
    ('usage_limit', 'error: usage limit reached'),
    ('credit_limit', 'error: credits exhausted')])
def test_review_quota_diagnostic_books_only_recognized_payload(replay, monkeypatch, prefix, evidence):
    from plangraph.spawner import exit_outcome
    from plangraph.spawner.leg_probe import key
    notifications = []
    monkeypatch.setattr(exit_outcome, 'notify_leg_refused',
                        lambda *args: notifications.append(args[-1]))
    assert harness_limit_line(1, '', evidence) == evidence
    record = replay.run(succeed='agy-opus-5.5',
                        failures={'agy-claude-opus': prefix + ': ' + evidence}, include_skips=False)
    reason = 'harness_limit: ' + evidence
    assert attempts_by_leg(record)['agy-claude-opus']['skip_reason'] == reason
    assert replay.service.leg_reader.leg_status(key(replay.plan.legs[0]))['state'] == 'limited'
    assert notifications == [reason]
    assert replay.calls == ['agy-claude-opus', 'agy-opus-5.5']
    assert record['status'] == 'completed' and 'reason' not in record


def test_replay_capture_does_not_claim_an_unavailable_source_hash():
    assert 'source_sha256' not in CAPTURE
    fragment = Path(__file__).parents[1] / 'changelog.d/SPAWNER-SEAT-FALLTHROUGH.md'
    assert 'hash-identified' not in fragment.read_text()


@pytest.mark.parametrize('refusal_event', [False, True])
@pytest.mark.parametrize('stop_status', [402, 429])
def test_provider_stop_is_redacted_before_recording(replay, monkeypatch, refusal_event, stop_status):
    from plangraph.spawner.env_redact import redact
    secret = 'sk-' + 'fixture' * 6
    body = 'rate limited for ' + secret
    seen = []
    save = replay.service._save

    def check_before_save(record):
        for attempt in record.get('attempts', ()):
            stop = attempt.get('provider_response')
            if stop:
                seen.append(stop)
                assert stop['body'] == redact(body)
        assert secret not in json.dumps(record)
        save(record)

    monkeypatch.setattr(replay.service, '_save', check_before_save)
    record = replay.run(succeed='agy-opus-5.5', provider_stop='failed', stop_status=stop_status,
                        refusal_event=refusal_event, stop_body=body)
    assert seen and all(stop['status'] == stop_status for stop in seen)
    assert attempts_by_leg(record)['agy-claude-opus']['provider_response']['body'] == redact(body)
    assert secret not in replay.service._path(record['run_id']).read_text()
    assert replay.calls == (['agy-claude-opus'] if refusal_event else ['agy-claude-opus', 'agy-opus-5.5'])
    assert record['status'] == ('failed' if refusal_event else 'completed')
