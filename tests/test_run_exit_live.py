"""Real children and HTTP delivery, including a lost local acknowledgement."""
import asyncio
import json
from pathlib import Path
import sys
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from plangraph.spawner.runner import SpawnerRunner, spawn_service


def test_live_smoke_contract():
    script = Path('tools/smoke-run-exit-notify.sh').read_text()
    assert '/v1/show?' in script
    assert 'keeper.delivered' in script
    assert 'run_ids' in script
    assert 'SpawnerRunner' in script


@pytest.mark.parametrize('visible', [True, False])
def test_smoke_requires_live_readback(tmp_path, monkeypatch, visible):
    """Execute the shipped smoke offline; a marker alone must never pass."""
    runs = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            runs.append(body['run']['run_id'])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def do_GET(self):
            assert self.path.startswith('/v1/show?key=')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'run_ids': runs if visible else []}).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    token = tmp_path / 'token'
    token.write_text('fake-token')
    monkeypatch.setenv('PG_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', 'p:item')
    monkeypatch.setenv('PLANGRAPH_NOTIFY_SMOKE_TIMEOUT', '.2')
    root = Path.cwd()
    monkeypatch.setenv('PYTHONPATH', str(root / 'src'))
    try:
        result = subprocess.run(['bash', str(root / 'tools/smoke-run-exit-notify.sh')],
                                cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert (result.returncode == 0) == visible, result.stderr
        assert len(runs) == 1
        assert len(list(tmp_path.glob('.notify-smoke-*/runs/*/keeper.delivered'))) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize('script,rc', [
    ('raise SystemExit(0)', 0), ('raise SystemExit(7)', 7),
    ('import os,signal; os.kill(os.getpid(),signal.SIGKILL)', -9),
    ('import time; time.sleep(60)', 124)])
def test_real_exit_http_marker_retry(tmp_path, monkeypatch, script, rc):
    events = {}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == '/v1/record_run'
            assert self.headers['Authorization'] == 'Bearer fake-token'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            run = body['run']
            key = (body['key'], run['run_id'], run['phase'])
            requests.append(key)
            events.setdefault(key, run)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    token = tmp_path / 'token'
    token.write_text('fake-token')
    monkeypatch.setenv('PG_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', 'p:item')
    monkeypatch.setattr('plangraph.spawner.runner.build_command',
                        lambda **kw: [sys.executable, '-c', script])
    if rc == 124:
        monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '.03')
    from plangraph.spawner import private_io
    original = private_io.atomic_write_private
    fail = [True]

    def write(path, data):
        if Path(path).name == 'keeper.delivered' and fail[0]:
            raise OSError('injected marker failure')
        return original(path, data)

    monkeypatch.setattr(private_io, 'atomic_write_private', write)
    service = spawn_service(runner=SpawnerRunner(), state=tmp_path)

    async def run():
        record = await service.spawn(card={'estimate_tokens': 1000}, prompt='tiny', model='omniroute/deepseek-direct-flash',
                                     override_reason='HTTP completion regression', wait_seconds=5)
        assert record['exit_code'] == rc
        marker = service._path(record['run_id']).parent / 'keeper.delivered'
        assert not marker.exists()
        assert len(events) == 1
        fail[0] = False
        service.retry_completions()
        service.retry_completions()
        assert marker.read_text() == 'delivered\n'
        assert len(events) == 1
        assert len(requests) == 2
        assert next(iter(events.values())) == {
            'run_id': record['run_id'], 'phase': 'finished', 'rc': rc}

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
