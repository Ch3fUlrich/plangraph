"""Provider-state and quota management credentials stay on validated endpoints."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from plangraph.routing_support import quota_snapshots


@pytest.mark.parametrize('status', [401, 403])
def test_snapshot_auth_refused_gateway(monkeypatch, tmp_path, status):
    from plangraph.routing_support.leg_state import LegAdapter, LegStateStore

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'fixture-key must never appear')

        def log_message(self, *args):
            pass

    server = HTTPServer(('localhost', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = f'http://localhost:{server.server_port}'
    env = {'OMNIROUTE_BASE_URL': gateway + '/v1', 'PLANGRAPH_PRINCIPAL': 'reviewer'}
    try:
        provider = quota_snapshots.from_env(env, key_resolver=lambda _: 'fixture-key')
        with pytest.raises(RuntimeError) as error:
            provider.snapshot()
        assert not isinstance(error.value, quota_snapshots.OmniRouteUnreachable)
        assert 'auth_refused' in str(error.value)
        assert gateway in str(error.value) and 'reviewer' in str(error.value)
        assert 'fixture-key' not in str(error.value)
        reader = LegAdapter(LegStateStore(tmp_path / 'state'), provider,
                            ['opencode-go/sample'], enabled=True)
        state = reader('opencode-go/sample')
        assert state['state'] == 'limited'
        assert state['reason'].startswith('auth_refused:')
        assert state['evidence'] == dict(kind='auth_refused', gateway_url=gateway,
                                         principal='reviewer', http_status=status)
        import asyncio
        from types import SimpleNamespace
        from plangraph.spawner import leg_probe

        def forbidden(*args, **kwargs):
            pytest.fail('auth refusal must not retry or probe')

        monkeypatch.setattr(leg_probe, 'probe', forbidden)
        monkeypatch.setenv('PLANGRAPH_LEG_PROBE_TTL_S', '1800')
        for role in ('review', 'implement', 'fix'):
            skip = asyncio.run(leg_probe.before_launch(
                SimpleNamespace(leg_reader=reader),
                SimpleNamespace(model='opencode-go/sample', harness='opencode'), role, False))
            assert skip == state['reason']
        assert seen == ['/api/providers', '/api/providers']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize('reader', ['state', 'quota'])
@pytest.mark.parametrize('endpoint', ['http://gw.example:8080/v1',
                                     'ftp://gw.example/v1',
                                     'https://ops:secret@gw.example/v1',
                                     'https://[invalid'])
def test_invalid_endpoint_refused_before_key_read(monkeypatch, reader, endpoint):
    def forbidden(*args):
        pytest.fail('refused endpoint read key or fetched')

    env = {'OMNIROUTE_BASE_URL': endpoint}
    monkeypatch.setattr(quota_snapshots, 'load_token', forbidden)
    monkeypatch.setattr(quota_snapshots, 'load_token', forbidden)
    if reader == 'state':
        state = quota_snapshots.from_env(env, fetch=forbidden)
        with pytest.raises(quota_snapshots.OmniRouteUnreachable, match='^no_endpoint$'):
            state.snapshot()
    else:
        with pytest.raises(LookupError, match='^gateway not configured$'):
            quota_snapshots.fetch_snapshot_rows(env, fetch=forbidden)


@pytest.mark.parametrize('reader', ['state', 'quota'])
@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_management_redirect_never_forwards_key(monkeypatch, reader, status):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            if self.path == '/redirected':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"connections": [{"provider": "p", "isActive": true}], "data": []}')
            else:
                self.send_response(status)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/redirected')
                self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(('localhost', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {'OMNIROUTE_BASE_URL': f'http://localhost:{server.server_port}/v1'}
    monkeypatch.setattr(quota_snapshots, 'load_token', lambda _: 'fixture-key')
    try:
        if reader == 'state':
            state = quota_snapshots.from_env(env, key_resolver=lambda _: 'fixture-key')
            with pytest.raises(quota_snapshots.OmniRouteUnreachable, match='^transport_error$'):
                state.snapshot()
            path = '/api/providers'
        else:
            with pytest.raises(LookupError, match='^quota snapshots unreadable$'):
                quota_snapshots.fetch_snapshot_rows(env)
            path = quota_snapshots.LIMITS_PATH
        assert seen == [(path, 'Bearer fixture-key')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
