"""Repeated polling is not a loop until completed calls repeat their results too."""
import json

from plangraph.spawner.stall_probe import ToolLoop


def frame(kind='item.completed', identity=None, output='unchanged', command='touch marker'):
    return json.dumps({'type': kind, 'item': {'type': 'command_execution',
        'id': identity, 'command': command, 'aggregated_output': output, 'exit_code': 0}})


def test_three_equal_completed_calls_are_not_enough():
    gate = ToolLoop()
    assert not any(gate.feed(frame(identity=str(i))) for i in range(3))


def test_equal_calls_with_changing_results_do_not_trigger():
    gate = ToolLoop()
    assert not any(gate.feed(frame(identity=str(i), output=f'progress {i}')) for i in range(12))


def test_idless_started_completed_pairs_count_once():
    gate = ToolLoop()
    for _ in range(3):
        assert not gate.feed(frame('item.started'))
        assert not gate.feed(frame())
    assert gate.count == 3
    for _ in range(2):
        assert not gate.feed(frame('item.started'))
        assert not gate.feed(frame())
    assert not gate.feed(frame('item.started'))
    assert gate.feed(frame())


def test_call_without_a_result_is_insufficient_evidence():
    gate = ToolLoop()
    assert not any(gate.feed(json.dumps(dict(type='tool_call', id=str(i), name='read',
                                            arguments={'path': 'same'}))) for i in range(12))


def test_duplicate_completions_count_once():
    gate = ToolLoop()
    for _ in range(10):
        assert not gate.feed(frame(identity='same-id'))
    assert gate.count == 1


def test_claude_results_pair_with_calls_before_loop():
    gate = ToolLoop()
    for i in range(6):
        assert not gate.feed(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': str(i), 'name': 'read', 'input': {'path': 'same'}}]}}))
        found = gate.feed(json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': str(i), 'content': 'same result'}]}}))
        assert found == (i == 5)


def test_read_only_status_polling_is_not_a_tool_loop():
    gate = ToolLoop()
    assert not any(gate.feed(frame(identity=str(i), command='git status')) for i in range(12))
