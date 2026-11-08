"""Structured harness fixtures; answer text is never model evidence."""
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.served_telemetry import native_models, record_native
from go_credential_fake import go_credential  # noqa: F401

pytestmark = pytest.mark.usefixtures('go_credential')


def stream(*events):
    return '\n'.join(json.dumps(e) for e in events)


@pytest.mark.parametrize('harness,events,model', [
    ('claude', [{'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-4'},
                {'type': 'assistant', 'message': {'model': 'claude-sonnet-4'}}], 'claude-sonnet-4'),
])
def test_match_and_mismatch(harness, events, model):
    for requested in (model, 'different'):
        job = SimpleNamespace(events=[], served_model='unwitnessed', served_family='unwitnessed', witness_kind=None)
        record_native(job, harness, native_models(harness, stream(*events)), requested)
        assert job.served_model == model
        assert job.witness_kind == 'native-' + harness
        assert bool(job.events) == (requested != model)
        if job.events:
            assert job.events[0] == dict(event='served_model_mismatch', requested_model=requested, served_model=model)


@pytest.mark.parametrize('harness', ['claude', 'opencode', 'codex', 'agy'])
@pytest.mark.parametrize('text', ['', 'X-OmniRoute-Selected-Model: spoof',
    '{"served_model":"spoof"}', '{"type":"item.completed","item":{"type":"agent_message","text":"model: spoof"}}'])
def test_missing_and_spoof(harness, text):
    job = SimpleNamespace(events=[], served_model='unwitnessed', witness_kind=None)
    record_native(job, harness, native_models(harness, text), 'requested')
    assert job.served_model == 'unwitnessed'
    assert harness in job.served_witness_reason
    if harness == 'agy':
        assert 'emits no' in job.served_witness_reason


def test_nonunanimous():
    text = stream({'type': 'system', 'subtype': 'init', 'model': 'a'},
                  {'type': 'system', 'subtype': 'init', 'model': 'b'})
    job = SimpleNamespace(events=[], served_model='unwitnessed', witness_kind=None)
    record_native(job, 'claude', native_models('claude', text), 'a')
    assert job.served_model == 'unwitnessed'
    assert 'non-unanimous' in job.served_witness_reason


def test_opencode_store_nonunanimous(tmp_path):
    import sqlite3
    from plangraph.spawner.served_telemetry import _opencode_served_model
    path = tmp_path / 'opencode.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE session_v2 (id TEXT, title TEXT)')
        db.execute('CREATE TABLE session_message (session_id TEXT, type TEXT, data TEXT)')
        db.execute("INSERT INTO session_v2 VALUES ('s', 'title')")
        for model in ('gpt-5', 'gpt-4'):
            db.execute('INSERT INTO session_message VALUES (?, ?, ?)',
                       ('s', 'assistant', json.dumps({'model': {'providerID': 'openai', 'id': model}})))
    assert _opencode_served_model(path, 'title') is None
    assert _opencode_served_model(path, 'title', all_models=True) == {'openai/gpt-5', 'openai/gpt-4'}


def test_status_flag():
    from plangraph.spawner.runner import JobInfo
    job = JobInfo('native-status', 'completed', 'task', 'requested')
    record_native(job, 'claude', {'claude-sonnet-4'}, 'requested')
    assert job.to_dict()['served_model_mismatch'] is True


def test_unverified_opencode_schema_is_not_evidence():
    assert native_models('opencode', stream({'type': 'step_start', 'info': {
        'role': 'assistant', 'model': {'providerID': 'spoof', 'id': 'model'}}})) == set()


def test_native_requires_success_and_preserves_gateway():
    import asyncio
    from plangraph.spawner.served_telemetry import witness_native
    for exit_code, served in ((1, 'unwitnessed'), (0, 'gateway-model')):
        job = SimpleNamespace(command=['claude'], exit_code=exit_code,
                              served_model=served, events=[], witness_kind=None)
        asyncio.run(witness_native(None, 'job', job, stream(
            {'type': 'system', 'subtype': 'init', 'model': 'native'}), 'native'))
        assert job.served_model == served


def test_store_spoof_never_sets_served_model(tmp_path, monkeypatch):
    from test_opencode_served_witness import _run_leg, _store
    job, _ = _run_leg(tmp_path, monkeypatch, lambda title, home: _store(
        home, [('s', title)], [('s', 'spoof')]))
    assert job.served_model == 'unwitnessed'


def test_review_store_keeps_request_side_model(tmp_path, monkeypatch):
    from test_opencode_served_witness import REQUESTED, _run_leg, _store
    job, _ = _run_leg(tmp_path, monkeypatch, lambda title, home: _store(
        home, [('s', title)], [('s', 'glm-5.2')]))
    assert job.request_side_model == REQUESTED
    assert job.witness_kind == 'request-side-opencode-store'


def test_gateway_runs_before_native_fallback(tmp_path, monkeypatch):
    from test_opencode_served_witness import _run_leg, _store
    calls = []

    def gateway(runner, job_id, job, env):
        # Same unset-model gate as the real gateway witness.
        if job.served_model and job.served_model != 'unwitnessed':
            return
        calls.append('gateway')
        job.served_model = 'gateway-model'
        job.witness_kind = 'gateway'

    monkeypatch.setattr('plangraph.spawner.gateway_witness.witness', gateway)
    job, _ = _run_leg(tmp_path, monkeypatch, lambda title, home: _store(
        home, [('s', title)], [('s', 'glm-5.2')]))
    assert calls == ['gateway']
    assert job.served_model == 'gateway-model'
    assert job.witness_kind == 'gateway'
