"""Synthetic classifier stops through the real monitor and dispatch, without a harness."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_claude_stream_render import fake_claude, run_review
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.spawner.claude_stream import last_stop_reason
from plangraph.spawner.exit_outcome import fallthrough_reason
from plangraph.spawner.runner import SpawnerRunner, spawn_service


def stream(shape, stop='refusal'):
    text = 'partial answer\nVERDICT: PASS'
    message = {'type': 'assistant', 'message': {
        'content': [{'type': 'text', 'text': text}], 'stop_reason': None}}
    ending = ({'type': 'result', 'result': text, 'stop_reason': stop} if shape == 'result'
              else {'type': 'assistant', 'message': {'content': [], 'stop_reason': stop}})
    if shape == 'assistant':
        message['message']['stop_reason'] = stop
        return [json.dumps(message)], text
    return [json.dumps(message), json.dumps(ending)], text


@pytest.mark.parametrize('shape', ['result', 'assistant', 'partial'])
def test_refusal_record_and_output(tmp_path, monkeypatch, shape):
    lines, text = stream(shape)
    record, directory = run_review(tmp_path, monkeypatch, lines)
    assert (record['status'], record.get('reason')) == ('failed', 'model_refusal')
    assert record['stop_reason'] == 'refusal'
    assert record['refusal_text_chars'] == len(text)
    assert record['stdout_tail'] == text
    assert (directory / 'claude-stream.jsonl').read_text().splitlines() == lines
    assert record['completion']['run_id'] == record['run_id']
    assert 'model_refusal' in json.dumps(record['completion'])


@pytest.mark.parametrize('stop', ['end_turn', 'max_tokens'])
def test_other_stops_unchanged(tmp_path, monkeypatch, stop):
    lines, text = stream('result', stop)
    record, _ = run_review(tmp_path, monkeypatch, lines)
    assert record['status'] == 'completed' and 'reason' not in record
    assert record['stdout_tail'] == text and 'refusal_text_chars' not in record


def test_last_explicit_stop():
    lines, _ = stream('partial')
    assert last_stop_reason('\n'.join(lines)) == 'refusal'
    assert last_stop_reason('\n'.join(lines + [json.dumps({'type': 'result', 'stop_reason': 'end_turn'})])) == 'end_turn'


@pytest.fixture
def captured_safety_stop():
    return {'type': 'result', 'subtype': 'success', 'is_error': False,
            'result': 'I cannot help with that request.', 'stop_reason': 'end_turn',
            'safety_stops': 1, 'num_turns': 1}


def test_result_safety_stops_is_refusal(tmp_path, monkeypatch, captured_safety_stop):
    lines = [json.dumps(captured_safety_stop)]
    assert last_stop_reason(lines[0]) == 'refusal'
    record, directory = run_review(tmp_path, monkeypatch, lines)
    assert (record['status'], record['reason'], record['stop_reason']) == ('failed', 'model_refusal', 'refusal')
    assert record['stdout_tail'] == captured_safety_stop['result']
    assert (directory / 'claude-stream.jsonl').read_text().splitlines() == lines
    assert record['completion']['run_id'] == record['run_id']
    assert 'model_refusal' in json.dumps(record['completion'])
    captured_safety_stop['safety_stops'] = 0
    assert last_stop_reason(json.dumps(captured_safety_stop)) == 'end_turn'


def test_no_booking_even_for_limit_text():
    job = SimpleNamespace(events=[{'event': 'model_refusal'}], exit_code=7,
                          stdout='usage limit reached', stderr='', refusal='quota')
    service = SimpleNamespace(leg_reader=SimpleNamespace(
        record_harness_limit=lambda *a, **k: pytest.fail('booked a safety refusal')))
    assert fallthrough_reason(service, DEFAULT_LEGS[0], job, {}) is None


@pytest.mark.parametrize('harness, tool_mode', [
    pytest.param('claude', 'prompt-only', id='claude'),
    pytest.param('opencode', 'tools', id='opencode'),
])
def test_route_is_terminal_only_for_claude(tmp_path, monkeypatch, harness, tool_mode):
    lines, _ = stream('partial')
    calls, notifications = [], []
    async def run():
        factory = fake_claude(tmp_path, lines)
        async def process(*a, **k):
            calls.append(a)
            return await factory(*a, **k)
        runner = SpawnerRunner(process_factory=process, default_cwd=str(tmp_path))
        sink = SimpleNamespace(deliver=lambda recipient, event: notifications.append(event))
        service = spawn_service(runner, tmp_path / 'state', completion_sink=sink, keeper_sink=sink)
        leg = next(l for l in DEFAULT_LEGS if l.harness == harness and not l.disabled
                   and 'review' in l.roles and l.tool_mode == tool_mode)
        legs = (leg, replace(leg, id='second-leg'))
        monkeypatch.setattr(service, '_route', lambda *a, **k: SpawnPlan(legs, (), 'test', (), ()))
        async def continuation(*a):
            assert harness != 'claude', 'continued refused writer'
        monkeypatch.setattr(service, '_continue_writer', continuation)
        return await service.spawn(card={'estimate_tokens': 1000, 'item': 'demo:REVIEW'}, prompt='review', role='review',
                                   writer_families=('mimo',), wait_seconds=None)
    monkeypatch.setattr('shutil.which', lambda name, **k: name)
    if harness == 'opencode':
        async def served(*args, **kwargs):
            return None
        monkeypatch.setattr('plangraph.spawner.resolver.host_skip_reason_async', served)
        monkeypatch.setattr('plangraph.spawner.runner._opencode_tools_denied', lambda *a: False)
        monkeypatch.setattr('plangraph.spawner.opencode_isolation.install_go_credential', lambda job_dir: True)   # CI holds no Go key
    record = asyncio.run(run())
    assert len(calls) == 1, record
    if harness == 'claude':
        assert calls[0][calls[0].index('--tools') + 1] == ''
        assert record['reason'] == 'model_refusal'
        assert any(e.get('run_id') == record['run_id'] and 'model_refusal' in json.dumps(e) for e in notifications)
    else:
        assert record.get('reason') != 'model_refusal' and 'refusal_text_chars' not in record
