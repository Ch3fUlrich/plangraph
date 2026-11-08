"""Structured harness fixtures; answer text is never model evidence."""
import json
from types import SimpleNamespace

import pytest

from plangraph.spawner.served_telemetry import native_models, record_native


def stream(*events):
    return '\n'.join(json.dumps(e) for e in events)


@pytest.mark.parametrize('harness,events,model', [
    ('claude', [{'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-4'},
                {'type': 'assistant', 'message': {'model': 'claude-sonnet-4'}}], 'claude-sonnet-4'),
    ('opencode', [{'type': 'step_start', 'part': {'type': 'step-start'},
                  'info': {'role': 'assistant', 'model': {'providerID': 'openai', 'modelID': 'gpt-5'}}}], 'openai/gpt-5'),
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
                  {'type': 'assistant', 'message': {'model': 'b'}})
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
