"""Synthetic refusal boundary tests; no runtime discovery or credentials."""
import json
import inspect
import socket
import subprocess
from types import SimpleNamespace as NS

import pytest

from plangraph.routing_support.leg_state import LegAdapter, LegStateStore
from plangraph.spawner import exit_outcome
from plangraph.spawner.env_redact import busy_refusal, early_refusal, harness_limit_line, unavailable_reason


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('external access denied')
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr('plangraph.routing_support.leg_state._default_store', denied)
    monkeypatch.setattr(exit_outcome, '_booking_seconds', lambda: 60)
    monkeypatch.setattr('plangraph.routing_support.leg_state.time.time', lambda: 1000)


CASES = [
    ('opencode-go/muse-spark-1.3-contributor',
     'Error: Model unavailable: opencode-go/muse-spark-1.3-contributor'),
    ('opencode-go/glm-5.2', 'Error: Go usage limit exceeded'),
]


@pytest.mark.parametrize('model,line', CASES)
@pytest.mark.parametrize('enabled', [False, True])
def test_listed_refusal_and_refresh(tmp_path, monkeypatch, model, line, enabled):
    clock = [0]
    calls, notices = [], []
    provider = NS(snapshot=lambda: calls.append('probe') or {
        'opencode-go': NS(available=True, stale=False)})
    store = LegStateStore(tmp_path / 'state.sqlite')
    reader = LegAdapter(store, provider, models=lambda: [model],
                        enabled=enabled, clock=lambda: clock[0], ttl_s=10)
    assert reader(model)['state'] == 'ok'
    monkeypatch.setattr(exit_outcome, 'notify_leg_refused',
                        lambda *args: notices.append(args[-1]))
    job = NS(exit_code=1, stdout='', stderr=line)
    record = {}
    reason = exit_outcome.fallthrough_reason(NS(leg_reader=reader), NS(model=model), job, record)
    assert reason
    assert len(notices) == 1
    assert reader(model)['state'] == 'limited'
    if enabled:
        clock[0] = 11
        assert reader(model)['state'] == 'ok'
        assert calls == ['probe', 'probe']
    else:
        assert calls == []


@pytest.mark.parametrize('model,line', CASES)
@pytest.mark.parametrize('code,prose', [(0, False), (1, True)])
@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_no_booking_for_success_or_prose(tmp_path, model, line, code, prose, stream):
    reader = LegStateStore(tmp_path / 'state.sqlite')
    text = 'The documentation discusses ' + line if prose else line
    record = {}
    assert exit_outcome.fallthrough_reason(NS(leg_reader=reader), NS(model=model),
                                         NS(exit_code=code, **{stream: text, ('stderr' if stream == 'stdout' else 'stdout'): ''}), record) is None
    assert record == {}
    assert not reader.path.exists()


@pytest.mark.parametrize('line', ['Go usage limit exceeded', 'Free usage limit reached',
                                'Error: Go usage limit exceeded (reset after 5h)'])
def test_complete_usage_lines(line):
    assert harness_limit_line(1, '', line) == line
    assert harness_limit_line(0, '', line) is None
    error = line if line.startswith('Error:') else 'Error: ' + line
    assert early_refusal(error) == error
    assert harness_limit_line(1, '', line + ' is documented here') is None


def test_busy_envelope_evidence_is_flat(tmp_path, monkeypatch):
    body = '\x1b[31mbusy\x1b[0m\n\tplease retry ' + 'x' * 220
    text = json.dumps({'autoos_provider_response': {'status': 503, 'body': body}})
    expected = 'HTTP 503: ' + ('busy please retry ' + 'x' * 220)[:200]
    assert busy_refusal(text) == expected
    notices = []
    monkeypatch.setattr(exit_outcome, 'notify_leg_refused', lambda *args: notices.append(args[-1]))
    store = LegStateStore(tmp_path / 'state.sqlite')
    exit_outcome.fallthrough_reason(NS(leg_reader=store), NS(model='synthetic/model'),
                                   NS(exit_code=1, stdout='', stderr=text), {})
    assert notices == ['harness_limit: ' + expected]
    assert '\x1b' not in str(store.leg_status('synthetic/model'))


@pytest.mark.parametrize('enabled', [False, True])
def test_missing_exit_code_keeps_unavailable_booking(tmp_path, enabled):
    store = LegStateStore(tmp_path / 'state.sqlite')
    reader = LegAdapter(store, NS(snapshot=lambda: {}), models=lambda: [], enabled=enabled)
    line = 'Error: Model unavailable: synthetic/model'
    reason = exit_outcome.fallthrough_reason(
        NS(leg_reader=reader), NS(model='synthetic/model'),
        NS(exit_code=None, stdout='', stderr=line), {})
    assert reason == line
    assert store.leg_status('synthetic/model')['state'] == 'limited'


def test_booking_uses_adapter_quarantine_interface(tmp_path):
    observations = []

    class ObservedAdapter(LegAdapter):
        def quarantine(self, leg):
            observations.append(leg)
            super().quarantine(leg)

    reader = ObservedAdapter(
        LegStateStore(tmp_path / 'state.sqlite'),
        NS(snapshot=lambda: {'synthetic': NS(available=True, stale=False)}),
        models=lambda: ['synthetic/model'], enabled=True)
    exit_outcome._book_failure(NS(leg_reader=reader), NS(model='synthetic/model'),
                               'Error: quota reached')
    assert observations == ['synthetic/model']
    assert reader('synthetic/model')['state'] == 'limited'


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_usage_limit_keeps_independent_quota_evidence(stream):
    line = 'HTTP 429: usage limit resets in 5m'
    output = {stream: line, ('stderr' if stream == 'stdout' else 'stdout'): ''}
    assert harness_limit_line(1, **output) == line
    assert early_refusal(line) == line


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_shaped_unavailable_keeps_legacy_diagnostic(stream):
    line = 'HTTP 503: Model unavailable: synthetic/model (transient)'
    output = {stream: line, ('stderr' if stream == 'stdout' else 'stdout'): ''}
    assert unavailable_reason(1, **output) == line
    assert unavailable_reason(0, **output) is None


def test_legacy_unavailable_stderr():
    # Unshaped text is prose on either stream, not harness evidence.
    assert unavailable_reason(1, '', 'prefix model unavailable') is None
    assert unavailable_reason(0, '', 'prefix model unavailable') is None
    assert unavailable_reason(1, 'prefix model unavailable', '') is None


def test_adapter_keeps_original_documentation():
    assert inspect.cleandoc(LegAdapter.__doc__) == (
        'Explicit opt-in reader; one cached boundary shared by routing and probes.\n\n'
        'Discovery names actual standalone identities, never combo/request aliases.\n'
        'Failures retain the previous snapshot but never admit from it as healthy.')
