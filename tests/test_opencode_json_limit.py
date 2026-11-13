import json
import os

import pytest

from plangraph.spawner.env_redact import early_refusal, harness_limit_line
from plangraph.routing_support.leg_state import LegStateStore, RESET_CAP_S
from plangraph.planner.measured_down import measured_down


def event(status=429, delay=847661000):
    return json.dumps(dict(type='error', timestamp=1794072567354, sessionID='ses_x',
        error=dict(type='provider.go-limit', status=status, retryAfterMs=delay,
            message='OpenCode Go monthly usage limit reached. Switch to a free model, or turn on Extra Usage on the Go page in OpenCode Console to keep going.',
            response=dict(body=json.dumps(dict(type='error', error=dict(
                type='GoUsageLimitError', message='Go usage limit exceeded'),
                metadata=dict(limitName='monthly')))))))


@pytest.mark.parametrize('status', [429, 402])
@pytest.mark.parametrize('delay', [60000, 30500.5, 847661000, 1e20])
@pytest.mark.parametrize('exit_code', [None, 0, 1])
def test_json_limit(status, delay, exit_code, tmp_path):
    line = harness_limit_line(exit_code, event(status, delay), '')
    assert line and early_refusal(event(status, delay) + '\n') == line
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit('opencode-go/test', line, 'test', now=100)
    assert store.leg_status('opencode-go/test', now=101)['evidence']['reset_at'] == 100 + min(delay / 1000, RESET_CAP_S)


@pytest.mark.parametrize('exit_code', [0, 1])
def test_prose_is_healthy(exit_code, tmp_path):
    from types import SimpleNamespace as NS
    from plangraph.spawner.exit_outcome import fallthrough_reason
    store = LegStateStore(tmp_path / 'legs.sqlite')
    prose = 'We discussed 429 in prose.'
    assert harness_limit_line(exit_code, prose, prose) is None
    assert early_refusal('We discussed 429 in prose.\n') is None
    assert fallthrough_reason(NS(leg_reader=store), NS(model='opencode-go/test'),
        NS(exit_code=exit_code, stdout=prose, stderr=prose), {}) is None
    assert not store.path.exists()


def test_old_run_keeps_booked_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_HOST', 'test-host')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    store = LegStateStore(tmp_path / 'plangraph' / 'leg-state.sqlite')
    store.record_harness_limit('opencode-go/test', early_refusal(event(delay=8000000)), 'test', now=100)
    path = tmp_path / 'runs' / 'run-test' / 'record.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(host='test-host', ended=100, attempts=[dict(
        status='failed', down_reason='usage_limit', leg=dict(id='go-test', model='opencode-go/test', harness='opencode'))])))
    os.utime(path, (100, 100))
    assert 'go-test' in measured_down(tmp_path, now=7300)
    from types import SimpleNamespace as NS
    from plangraph.spawner.gateway_witness import witness_order
    legs = tuple(NS(id=name) for name in ('go-test', 'other', 'third'))
    assert [leg.id for leg in witness_order({}, 'review', legs,
        measured_down(tmp_path, now=7300))] == ['other', 'third', 'go-test']
    assert not measured_down(tmp_path, now=8100)
    assert not measured_down(tmp_path, now=100 + RESET_CAP_S)


@pytest.mark.parametrize('status,token', [(429, 'usage_limit'), (402, 'credit_limit')])
@pytest.mark.parametrize('path', ['streaming', 'exit', 'success'])
def test_outcome_paths(status, token, path, tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from plangraph.routing_support import leg_state
    from plangraph.spawner.exit_outcome import fallthrough_reason
    monkeypatch.setattr(leg_state, 'time', NS(time=lambda: 100))
    store = LegStateStore(tmp_path / 'legs.sqlite')
    job = NS(exit_code={'streaming': 7, 'exit': 1, 'success': 0}[path],
             stdout='' if path == 'streaming' else event(status, 60000), stderr='',
             refusal=early_refusal(event(status, 60000)))
    record = {}
    reason = fallthrough_reason(NS(leg_reader=store), NS(model='opencode-go/test'), job, record)
    # This is the token service.py persists as attempt.down_reason.
    assert reason.split(':', 1)[0] == token
    assert record['reason'] == token
    assert store.leg_status('opencode-go/test', now=101)['evidence']['reset_at'] == 160


@pytest.mark.parametrize('status,reason', [(429, 'usage_limit'), (402, 'credit_limit')])
@pytest.mark.parametrize('suffix,retained', [('N20', True), ('A04', False), ('A05', False)])
def test_a1_scrubbed_reader_boundary(status, reason, suffix, retained, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    from plangraph.spawner import env_redact as er, exit_outcome as eo
    rows = json.loads((Path(__file__).parent / 'fixtures/redact_name_scope.json').read_text())
    raw = ''.join(next(r for r in rows if r['id'] == 'a1-' + suffix)['input_parts']).replace('429', str(status))
    for quote in ('"', "'"):
        raw = raw.replace(quote + 'status' + quote + ':' + str(status),
                          quote + 'retryAfterMs' + quote + ':30500.5,' + quote + 'status' + quote + ':' + str(status))
    line = er.redact_output(raw)
    raw_expected = f'HTTP {status}: {reason} (reset after 30500.5ms)'
    assert er.opencode_limit(raw) == er.early_refusal(raw) == raw_expected
    expected = raw_expected if retained else None
    assert er.opencode_limit(line) == er.early_refusal(line) == expected
    book, notify = Mock(), Mock()
    monkeypatch.setattr(eo, '_book_failure', book); monkeypatch.setattr(eo, 'notify_leg_refused', notify)
    # Vary harness_limit_line's exit_code argument and fallthrough_reason's job.exit_code, not usage counters.
    for exit_code in (None, 0, 1):
        assert er.harness_limit_line(exit_code, raw, '') == raw_expected
        assert er.harness_limit_line(exit_code, line, '') == expected
        if exit_code is not None:
            record, job = {}, NS(exit_code=exit_code, stdout=line, stderr='')
            assert eo.fallthrough_reason(NS(), NS(), job, record) == (reason + ': ' + expected if retained else None)
            assert record == ({'reason': reason} if retained else {})
            assert not getattr(job, 'refusal', None)
    assert (book.call_count, notify.call_count) == ((2, 2) if retained else (0, 0))


def test_a1_scrubbed_usage_counters_survive():
    from pathlib import Path
    from plangraph.spawner.env_redact import redact_output
    rows = json.loads((Path(__file__).parent / 'fixtures/redact_name_scope.json').read_text())
    raw = ''.join(next(r for r in rows if r['id'] == 'a1-N21')['input_parts'])
    assert json.loads(redact_output(raw))['usage'] == {'input_'+'tokens': 12, 'output_'+'tokens': 34}
