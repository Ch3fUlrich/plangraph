import json
import os

import pytest

from plangraph.spawner.env_redact import early_refusal, harness_limit_line
from plangraph.routing_support.leg_state import LegStateStore, RESET_CAP_S
from plangraph.planner.measured_down import measured_down


def event(status=429):
    return json.dumps(dict(type='error', timestamp=1794072567354, sessionID='ses_x',
        error=dict(type='provider.go-limit', status=status, retryAfterMs=847661000,
            message='OpenCode Go monthly usage limit reached. Switch to a free model, or turn on Extra Usage on the Go page in OpenCode Console to keep going.',
            response=dict(body=json.dumps(dict(type='error', error=dict(
                type='GoUsageLimitError', message='Go usage limit exceeded'),
                metadata=dict(limitName='monthly')))))))


@pytest.mark.parametrize('status', [429, 402])
def test_json_limit(status, tmp_path):
    line = harness_limit_line(1, event(status), '')
    assert line and early_refusal(event(status) + '\n') == line
    store = LegStateStore(tmp_path / 'legs.sqlite')
    store.record_harness_limit('opencode-go/test', line, 'test', now=100)
    assert store.leg_status('opencode-go/test', now=101)['evidence']['reset_at'] == 100 + RESET_CAP_S


def test_prose_is_healthy():
    assert harness_limit_line(0, 'We discussed 429 in prose.', '') is None
    assert early_refusal('We discussed 429 in prose.\n') is None


def test_old_run_keeps_booked_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_HOST', 'test-host')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    store = LegStateStore(tmp_path / 'plangraph' / 'leg-state.sqlite')
    store.record_harness_limit('opencode-go/test', 'reset after 24h', 'test', now=100)
    path = tmp_path / 'runs' / 'run-test' / 'record.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(host='test-host', ended=100, attempts=[dict(
        status='failed', down_reason='usage_limit', leg=dict(id='opencode-go/test'))])))
    os.utime(path, (100, 100))
    assert 'opencode-go/test' in measured_down(tmp_path, now=7300)
    from types import SimpleNamespace as NS
    from plangraph.spawner.gateway_witness import witness_order
    legs = tuple(NS(id=name) for name in ('opencode-go/test', 'other', 'third'))
    assert [leg.id for leg in witness_order({}, 'review', legs,
        measured_down(tmp_path, now=7300))] == ['other', 'third', 'opencode-go/test']
    assert not measured_down(tmp_path, now=100 + RESET_CAP_S)
