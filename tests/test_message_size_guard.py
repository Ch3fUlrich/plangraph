"""Standalone hook subprocess contract."""
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('message,tool,override,code', [
    ('small', 'SendMessage', None, 0),
    ('x' * 6000, 'SendMessage', None, 0),
    ('x' * 6001, 'SendMessage', None, 2),
    ('é' * 3001, 'SendMessage', None, 2),
    ('x' * 6001, 'Other', None, 0),
    ('abc', 'SendMessage', '2', 2),
    ('x' * 6001, 'SendMessage', 'invalid', 2),
])
def test_guard(message, tool, override, code):
    env = dict(os.environ)
    env.pop('PLANGRAPH_MESSAGE_MAX_BYTES', None)
    if override is not None:
        env['PLANGRAPH_MESSAGE_MAX_BYTES'] = override
    p = subprocess.run([sys.executable, 'tools/hooks/message_size_guard.py'],
                       input=json.dumps({'tool_name': tool, 'tool_input': {'message': message}}),
                       capture_output=True, text=True, env=env)
    assert p.returncode == code
    assert p.stdout == ''
    if code == 0:
        assert p.stderr == ''
    else:
        limit = 2 if override == '2' else 6000
        assert p.stderr == (f'message is {len(message.encode())} bytes, limit {limit}: '
                            'a document goes by pointer: write it to a file, run '
                            'fleet-artifact put <file>, send the ARTIFACT line\n')
        assert message not in p.stderr


@pytest.mark.parametrize('payload', ['{', '[]', '{"tool_name":"SendMessage"}'])
def test_malformed(payload):
    p = subprocess.run([sys.executable, 'tools/hooks/message_size_guard.py'],
                       input=payload, text=True, capture_output=True)
    assert p.returncode == 0 and p.stdout == ''
    assert len(p.stderr.splitlines()) == 1
