import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
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


def test_ps_real_records(tmp_path):
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


def stdio_env(tmp_path):
    src = Path(__file__).resolve().parents[1] / 'src'
    return {**os.environ, 'PYTHONPATH': str(src), 'HOME': str(tmp_path), 'USERPROFILE': str(tmp_path),
            'XDG_STATE_HOME': str(tmp_path)}


def stop(proc):
    proc.kill()
    proc.wait(timeout=30)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass


def test_real_stdio_tool_call_round_trip_while_stdin_stays_open(tmp_path):
    """Real clients keep stdin open and wait for each reply before the next request."""
    proc = subprocess.Popen([sys.executable, '-m', 'plangraph.spawner.mcp'], env=stdio_env(tmp_path), cwd=tmp_path,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    answers = queue.Queue()
    threading.Thread(target=lambda: [answers.put(line) for line in proc.stdout], daemon=True).start()

    def call(msg_id, method, params):
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': msg_id, 'method': method, 'params': params}).encode()
                         + b'\n')
        proc.stdin.flush()
        return json.loads(answers.get(timeout=60))

    try:
        assert call(1, 'initialize', {})['result']['serverInfo']['name'] == 'plangraph-spawner'
        reply = call(2, 'tools/call', {'name': 'plangraph_agent_ps', 'arguments': {}})
        assert reply['id'] == 2 and 'error' not in reply
        assert json.loads(reply['result']['content'][0]['text'])['total'] == 0
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        stop(proc)


def test_real_stdio_accepts_stdin_redirected_from_a_file(tmp_path):
    """Pipe-only transports refuse this on every platform, so it also guards POSIX-only CI."""
    requests = tmp_path / 'requests.jsonl'
    requests.write_text(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}) + '\n')
    with requests.open('rb') as stdin:
        done = subprocess.run([sys.executable, '-m', 'plangraph.spawner.mcp'], env=stdio_env(tmp_path), cwd=tmp_path,
                              stdin=stdin, capture_output=True, text=True, timeout=60)
    assert [json.loads(line)['id'] for line in done.stdout.splitlines()] == [1]
    assert done.returncode == 0


def test_server_failure_exits_without_waiting_for_stdin(tmp_path):
    """A read blocked on open stdin must not hold the process, or crash it, at shutdown."""
    proc = subprocess.Popen([sys.executable, '-m', 'plangraph.spawner.mcp'], env=stdio_env(tmp_path), cwd=tmp_path,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    errors = []
    threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True).start()
    try:
        proc.stdout.close()  # The next reply fails; stdin stays open with a read pending.
        proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}).encode()
                         + b'\n')
        proc.stdin.flush()
        assert proc.wait(timeout=60) != 0
        deadline = time.monotonic() + 30
        while not errors and time.monotonic() < deadline:
            time.sleep(0.05)
        assert errors and b'Fatal Python error' not in errors[0]
    finally:
        stop(proc)
