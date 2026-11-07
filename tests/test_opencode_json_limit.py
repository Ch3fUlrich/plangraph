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
