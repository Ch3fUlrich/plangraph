"""Hermetic host doctor checks: subprocesses and HTTP stay local."""
import asyncio
import contextlib
import http.server
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from plangraph.spawner import doctor as d
from plangraph.spawner.service import SpawnService as RealService


@pytest.fixture
def host(tmp_path, monkeypatch):
    from plangraph.planner import legs
    from plangraph.spawner import service, leg_probe
    monkeypatch.setenv('PATH', str(tmp_path))
    token = tmp_path / 'credential'
    token.write_text('planted-credential-value')
    token.chmod(0o600)
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    monkeypatch.setenv('PG_URL', 'http://unused.invalid')
    monkeypatch.setenv('PLANTED_SECRET', 'planted-private-environment')
    monkeypatch.setattr(legs, 'HARNESSES', ('fake', 'missing'))
    binary = tmp_path / 'fake'
    binary.write_text(f'#!{sys.executable}\nprint("fake version 1")\n')
    binary.chmod(0o700)
    class Service:
        runner = object()
        leg_reader = None

        def route(self, card, role, **kwargs):
            if role == 'review':
                from plangraph.review.family import writer_key
                assert writer_key(kwargs['writer_families'][0])
            assert card and role in ('implement', 'review')
            return SimpleNamespace(legs=[SimpleNamespace(id=role + '-one'),
                                         SimpleNamespace(id=role + '-two')])
    monkeypatch.setattr(service, 'SpawnService', Service)
    async def no_probe(*args, **kwargs):
        pytest.fail('default must not call probe')
    monkeypatch.setattr(leg_probe, 'probe_many', no_probe)
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def fake_mcp(tmp_path, mode):
    script = tmp_path / 'peer.py'
    script.write_text('''import json, sys, time
mode = sys.argv[1]
if mode == 'exit': sys.exit(0)
if mode == 'hang': time.sleep(60)
for line in sys.stdin:
    msg = json.loads(line)
    if msg['method'] == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {}, 'serverInfo': {}}
    elif msg['method'] == 'notifications/initialized':
        continue
    elif msg['method'] == 'tools/list':
        result = {'tools': [] if mode == 'empty' else [{'name': 'ready'}]}
    else: sys.exit(2)
    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)
''')
    return [sys.executable, str(script), mode]


@pytest.mark.parametrize('mode', ['good', 'exit', 'hang', 'empty'])
@pytest.mark.parametrize('component', ['spawner', 'keeper'])
def test_mcp_real_exchange_and_reap(host, monkeypatch, mode, component):
    children = []
    original = asyncio.create_subprocess_exec
    async def record(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', record)
    monkeypatch.setattr(d, 'TIMEOUT', 0.2)
    coro = d.mcp('plangraph.' + component + '.mcp', fake_mcp(host, mode))
    if mode == 'good':
        assert run(coro) == ('pass', '1 tools')
    else:
        with pytest.raises(Exception):
            run(coro)
    assert len(children) == 1 and children[0].returncode is not None


@contextlib.contextmanager
def server(status):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/v1/whoami'
            assert self.headers['Authorization'].split() == ['Bearer', 'planted-credential-value']
            self.send_response(status)
            if status == 302:
                self.send_header('Location', '/must-not-follow')
            self.end_headers()
            self.wfile.write(json.dumps(dict(name='doctor', role='spawner')).encode())

        def log_message(self, *args):
            pass
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}'
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


@pytest.mark.parametrize('status', [200, 401, 302])
def test_whoami_http(host, monkeypatch, status):
    operation = d.whoami
    with server(status) as url:
        monkeypatch.setenv('PG_URL', url)
        if status == 200:
            assert d.whoami() == ('pass', 'principal=doctor role=spawner')
        else:
            with pytest.raises(Exception):
                operation()


@pytest.mark.parametrize('broken', ['PG_URL', 'PG_TOKEN_FILE', 'mode', 'unreadable', 'empty', 'unreachable'])
def test_whoami_broken(host, monkeypatch, broken):
    operation = d.whoami
    if broken.startswith('PG_'):
        monkeypatch.delenv(broken)
    elif broken == 'mode':
        Path(os.environ['PG_TOKEN_FILE']).chmod(0o644)
    elif broken == 'unreadable':
        Path(os.environ['PG_TOKEN_FILE']).unlink()
    elif broken == 'empty':
        Path(os.environ['PG_TOKEN_FILE']).write_text('')
    else:
        monkeypatch.setenv('PG_URL', 'http://127.0.0.1:1')
    with pytest.raises(Exception):
        operation()


@pytest.mark.parametrize('missing', [None, 'PG_URL', 'PG_TOKEN_FILE'])
def test_completion_configuration(host, monkeypatch, missing):
    from plangraph.spawner.notify import KeeperNotConfigured, KeeperSink
    monkeypatch.setattr(KeeperSink, 'deliver', lambda *a: pytest.fail('no delivery'))
    if missing:
        monkeypatch.delenv(missing)
        with pytest.raises(KeeperNotConfigured, match='PG_URL and PG_TOKEN_FILE'):
            d.completion()
    else:
        assert d.completion()[0] == 'pass'


@pytest.mark.parametrize('mode', ['good', 'empty', 'exit', 'hang', 'missing'])
def test_harness(host, monkeypatch, mode):
    monkeypatch.setattr(d, 'TIMEOUT', 0.2)
    binary = host / 'fake'
    if mode == 'exit':
        binary.write_text(f'#!{sys.executable}\nraise SystemExit(1)\n')
    elif mode == 'hang':
        binary.write_text(f'#!{sys.executable}\nimport time; time.sleep(60)\n')
    elif mode == 'empty':
        binary.write_text(f'#!{sys.executable}\n')
    if mode in ('exit', 'hang'):
        with pytest.raises(Exception):
            run(d.harness('fake'))
    else:
        assert run(d.harness('missing' if mode == 'missing' else 'fake'))[0] == (
            'skip' if mode == 'missing' else 'pass')


@pytest.fixture
def isolated(host, monkeypatch):
    async def mcp(module):
        assert module in ('plangraph.spawner.mcp', 'plangraph.keeper.mcp')
        return 'pass', '1 tools'
    monkeypatch.setattr(d, 'mcp', mcp)
    monkeypatch.setattr(d, 'whoami', lambda: ('pass', 'principal=doctor role=spawner'))
    return host


@pytest.mark.parametrize('mode', ['good', 'no-harness', 'no-leg', 'crash', 'completion'])
def test_aggregate_exit_and_independence(isolated, monkeypatch, capsys, mode):
    from plangraph.spawner import service
    if mode == 'no-harness':
        (isolated / 'fake').unlink()
    elif mode == 'no-leg':
        monkeypatch.setattr(service.SpawnService, 'route', lambda *a, **k: SimpleNamespace(legs=[]))
    elif mode == 'crash':
        def crash():
            raise RuntimeError('planted-credential-value planted-private-environment')
        monkeypatch.setattr(d, 'whoami', crash)
    elif mode == 'completion':
        monkeypatch.delenv('PG_URL')
    assert d.main(['--json']) == (0 if mode == 'good' else 1)
    output = capsys.readouterr().out
    assert 'planted-credential-value' not in output
    assert 'planted-private-environment' not in output
    result = json.loads(output)
    rows = {row['name']: row for row in result['checks']}
    assert result['ok'] == (mode == 'good')
    assert rows['probe_review']['status'] == 'skip'
    assert rows['harness_missing']['status'] == 'skip'
    assert rows['route_review']['status'] == ('fail' if mode == 'no-leg' else 'pass')
    if mode == 'no-harness':
        assert rows['harness_any']['status'] == 'fail'
    if mode == 'crash':
        assert rows['keeper_whoami']['detail'].startswith('RuntimeError:')


@pytest.mark.parametrize('answer', [False, True])
def test_opt_in_probe(isolated, monkeypatch, capsys, answer):
    from plangraph.spawner import leg_probe
    calls = []
    async def probe(runner, legs):
        calls.append(legs)
        return dict(probes=[dict(id=leg, ok=answer and i == 1,
            reason='Free usage limit reached' if i == 0 else 'Model unavailable')
            for i, leg in enumerate(legs)])
    monkeypatch.setattr(leg_probe, 'probe_many', probe)
    assert d.main(['--json', '--probe']) == (0 if answer else 1)
    result = json.loads(capsys.readouterr().out)
    rows = [row for row in result['checks'] if row['name'].startswith('probe_')]
    assert len(calls) == len(rows) == 2
    for row in rows:
        assert row['status'] == ('pass' if answer else 'fail')
        assert 'Free usage limit reached' in row['detail']
        assert ('answered' if answer else 'Model unavailable') in row['detail']


def test_text_output(isolated, capsys):
    assert d.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 10
    assert all(': pass ' in line or ': skip ' in line for line in lines)


def test_malformed_token_does_not_abort(isolated, capsys):
    Path(os.environ['PG_TOKEN_FILE']).write_bytes(b'\xff')
    assert d.main(['--json']) == 0
    assert json.loads(capsys.readouterr().out)['checks'][-1]['name'] == 'probe_review'


@pytest.mark.parametrize('target', ['mcp', 'whoami', 'completion', 'harness'])
def test_each_check_crash_is_failure(isolated, monkeypatch, capsys, target):
    def crash(*args):
        raise RuntimeError('planted-credential-value planted-private-environment')
    monkeypatch.setattr(d, target, crash)
    assert d.main(['--json']) == 1
    output = capsys.readouterr().out
    assert 'planted-credential-value' not in output
    assert 'planted-private-environment' not in output
    rows = json.loads(output)['checks']
    assert any(row['status'] == 'fail' and row['detail'].startswith('RuntimeError:') for row in rows)
    assert rows[-1]['name'] == 'probe_review'


def test_real_service_review_family(isolated, monkeypatch):
    from plangraph.spawner import service
    from plangraph.planner import routing
    from plangraph.review.family import writer_key
    # Use the real service method, but replace planning's host catalog/state I/O.
    seen = []
    def plan(card, role, writer_families, **kwargs):
        if role == 'review':
            seen.extend(writer_key(family) for family in writer_families)
        return SimpleNamespace(error=None, legs=[SimpleNamespace(id='local-leg')])
    monkeypatch.setattr(routing, 'plan_spawn', plan)
    monkeypatch.setattr(service.SpawnService, 'route', RealService.route)
    monkeypatch.setattr(service.SpawnService, '_route', RealService._route, raising=False)
    result = run(d.diagnose())
    assert result['ok'], result
    assert seen
