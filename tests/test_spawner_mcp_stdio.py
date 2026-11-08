import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from plangraph.spawner import mcp_limits
from plangraph.spawner.mcp import MCPSpawnerServer, stdio_reply
from plangraph.spawner.mcp_limits import encode_reply, ps_reply
from plangraph.spawner.runner import spawn_service


def request(server, method='tools/list', **extra):
    return asyncio.run(stdio_reply(server, json.dumps({
        'jsonrpc': '2.0', 'id': 7, 'method': method, **extra}).encode()))


@pytest.mark.parametrize('method', [
    'notifications/initialized', 'notifications/cancelled', 'tools/call', 'unknown',
])
def test_notifications(tmp_path, monkeypatch, method):
    server = MCPSpawnerServer(state=tmp_path)
    original = server.handle_rpc_request

    async def forbidden(req):
        raise AssertionError('notification dispatched')

    monkeypatch.setattr(server, 'handle_rpc_request', forbidden)
    assert asyncio.run(stdio_reply(server, json.dumps({
        'jsonrpc': '2.0', 'method': method}).encode())) is None
    monkeypatch.setattr(server, 'handle_rpc_request', original)
    reply = json.loads(request(server))
    assert reply['id'] == 7 and reply['result']['tools']


@pytest.mark.parametrize('line,code', [
    (b'{', -32700), (b'\xff', -32700),
    (b'{"jsonrpc":"1.0","id":7,"method":"tools/list"}', -32600),
    (b'[]', -32600),
])
def test_invalid_lines(tmp_path, line, code):
    reply = json.loads(asyncio.run(stdio_reply(MCPSpawnerServer(state=tmp_path), line)))
    assert reply['error']['code'] == code


def test_unexpected_exception(tmp_path, monkeypatch):
    server = MCPSpawnerServer(state=tmp_path)

    async def broken(req):
        raise RuntimeError('test failure')

    monkeypatch.setattr(server, 'handle_rpc_request', broken)
    reply = json.loads(request(server))
    assert reply['id'] == 7 and reply['error']['code'] == -32603


def test_ps_large_view():
    payload = 'x' * 40000
    runs = [{'run_id': str(i), 'status': 'completed', 'started': 4000 - i,
             'attempts': [{'output': payload}], 'stderr_tail': payload}
            for i in range(3030)]
    for i in range(0, 3030, 101):
        runs[i]['status'] = 'running' if i % 2 else 'queued'
    live = [r['run_id'] for r in runs if r['status'] in ('running', 'queued')]
    ended = [r['run_id'] for r in runs if r['status'] == 'completed'][:20]
    reply = ps_reply(runs)
    assert len(json.dumps(reply, indent=2).encode()) < 1024 * 1024
    assert [r['run_id'] for r in reply['runs']] == live + ended
    assert len(live) == 30
    assert reply['total'] == 3030 and reply['omitted'] == 2980
    assert all('attempts' not in r and 'stderr_tail' not in r for r in reply['runs'])
    assert 'attempts' in runs[0]


def test_ps_leg():
    assert ps_reply([{'chosen_leg': {'id': 'seat', 'extra': 'omit'}}])['runs'] == [{'leg': 'seat'}]


def test_ps_real_records(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))  # no operator SSH fan-out
    service = spawn_service(state=tmp_path)
    directory = tmp_path / 'runs'
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(25):
        record_dir = directory / f'run-{i}'
        record_dir.mkdir()
        (record_dir / 'record.json').write_text(json.dumps({
            'run_id': f'run-{i}', 'started': i, 'status': 'completed',
            'attempts': [{'stdout_tail': 'x' * 40000}]}))
    full = service.ps()['runs']
    assert len(full) == 25 and all(r['attempts'] for r in full)
    reply = json.loads(request(MCPSpawnerServer(state=tmp_path), 'tools/call',
                               params={'name': 'plangraph_agent_ps', 'arguments': {}}))
    compact = json.loads(reply['result']['content'][0]['text'])
    assert compact == ps_reply(full)
    assert compact['omitted'] == 5 and len(compact['runs']) == 20
    assert all('attempts' not in r for r in compact['runs'])
    assert service.ps()['runs'] == full


def test_encode_cap(monkeypatch):
    reply = {'jsonrpc': '2.0', 'id': 'request', 'result': 'é' * 500}
    size = len(json.dumps(reply).encode())
    monkeypatch.setattr(mcp_limits, 'MAX_REPLY_BYTES', size)
    assert json.loads(encode_reply(reply)) == reply
    monkeypatch.setattr(mcp_limits, 'MAX_REPLY_BYTES', size - 1)
    error = json.loads(encode_reply(reply))
    assert error['id'] == 'request' and 'result' not in error
    assert error['error']['code'] == -32000
    assert error['error']['data'] == {'type': 'ReplyTooLarge', 'exit_code': 1}
    assert str(size) in error['error']['message']
    assert str(size - 1) in error['error']['message']


def test_stdio_cap_recovers(tmp_path, monkeypatch):
    server = MCPSpawnerServer(state=tmp_path)
    cap = mcp_limits.MAX_REPLY_BYTES
    monkeypatch.setattr(mcp_limits, 'MAX_REPLY_BYTES', 512)
    reply = json.loads(request(server))
    assert reply['id'] == 7 and 'result' not in reply
    assert reply['error']['data']['type'] == 'ReplyTooLarge'
    monkeypatch.setattr(mcp_limits, 'MAX_REPLY_BYTES', cap)
    assert json.loads(request(server))['result']['tools']


def test_real_stdio_handshake_writes_only_replies_with_ids(tmp_path):
    src = Path(__file__).resolve().parents[1] / 'src'
    env = {**os.environ, 'PYTHONPATH': str(src), 'HOME': str(tmp_path), 'XDG_STATE_HOME': str(tmp_path)}
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
    done = subprocess.run([sys.executable, '-m', 'plangraph.spawner.mcp'], env=env, cwd=tmp_path, capture_output=True,
                          text=True, timeout=60, input=''.join(json.dumps(m) + '\n' for m in messages))
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [reply['id'] for reply in replies] == [1, 2]
    assert replies[1]['result']['tools']


def stdio_process(tmp_path, *, stdin=subprocess.PIPE, script=None):
    src = Path(__file__).resolve().parents[1] / 'src'
    env = {**os.environ, 'PYTHONPATH': str(src), 'HOME': str(tmp_path),
           'USERPROFILE': str(tmp_path), 'XDG_STATE_HOME': str(tmp_path)}
    args = ['-c', script] if script else ['-m', 'plangraph.spawner.mcp']
    return subprocess.Popen([sys.executable, *args], env=env, cwd=tmp_path,
                            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def reap_stdio(proc):
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)


def test_stdio_open_pipe_tool_round_trip(tmp_path):
    proc = stdio_process(tmp_path)
    answers = queue.Queue()
    reader = threading.Thread(target=lambda: answers.put(proc.stdout.readline()), daemon=True)
    try:
        reader.start()
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'plangraph_agent_ps', 'arguments': {}}}
        proc.stdin.write(json.dumps(msg).encode() + b'\n')
        proc.stdin.flush()
        reply = json.loads(answers.get(timeout=10))
        assert reply['id'] == 1 and 'error' not in reply
        assert json.loads(reply['result']['content'][0]['text'])['total'] == 0
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=10)
        reap_stdio(proc)


def test_stdio_redirected_file(tmp_path):
    requests = tmp_path / 'requests.jsonl'
    requests.write_text('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    with requests.open('rb') as stdin:
        proc = stdio_process(tmp_path, stdin=stdin)
        try:
            out, err = proc.communicate(timeout=10)
            assert proc.returncode == 0, err
            assert [json.loads(line)['id'] for line in out.splitlines()] == [1]
        finally:
            reap_stdio(proc)


def test_stdio_failure_with_pending_read(tmp_path):
    script = '''
import asyncio
from plangraph.spawner import mcp
async def main():
    task = asyncio.create_task(mcp.run_stdio_server())
    await asyncio.sleep(0.2)
    assert not task.done()
    raise RuntimeError('injected server failure')
asyncio.run(main())
'''
    proc = stdio_process(tmp_path, script=script)
    try:
        assert proc.wait(timeout=10) != 0
        err = proc.stderr.read()
        assert b'injected server failure' in err
        assert b'Fatal Python error' not in err
    finally:
        reap_stdio(proc)


def test_stdio_eof_exits_zero(tmp_path):
    proc = stdio_process(tmp_path)
    try:
        out, err = proc.communicate(input=b'', timeout=10)
        assert proc.returncode == 0, err
        assert out == b''
    finally:
        reap_stdio(proc)
