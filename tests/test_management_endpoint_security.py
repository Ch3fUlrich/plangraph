"""Provider-state and quota management credentials stay on validated endpoints."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from plangraph.routing_support import omniroute_state, quota_snapshots


@pytest.mark.parametrize('reader', ['state', 'quota'])
@pytest.mark.parametrize('endpoint', ['http://gw.example:8080/v1',
                                     'ftp://gw.example/v1',
                                     'https://ops:secret@gw.example/v1',
                                     'https://[invalid'])
def test_invalid_endpoint_refused_before_key_read(monkeypatch, reader, endpoint):
    def forbidden(*args):
        pytest.fail('refused endpoint read key or fetched')

    env = {'OMNIROUTE_BASE_URL': endpoint}
    monkeypatch.setattr(omniroute_state, '_gateway_key', forbidden)
    monkeypatch.setattr(quota_snapshots, '_gateway_key', forbidden)
    if reader == 'state':
        state = omniroute_state.from_env(env, fetch=forbidden)
        with pytest.raises(omniroute_state.OmniRouteUnreachable, match='^no_endpoint$'):
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
    monkeypatch.setattr(quota_snapshots, '_gateway_key', lambda _: 'fixture-key')
    try:
        if reader == 'state':
            state = omniroute_state.from_env(env, key_resolver=lambda _: 'fixture-key')
            with pytest.raises(omniroute_state.OmniRouteUnreachable, match='^transport_error$'):
                state.snapshot()
            path = '/api/providers'
        else:
            with pytest.raises(LookupError, match='^quota snapshots unreadable$'):
                quota_snapshots.fetch_snapshot_rows(env)
            path = quota_snapshots.URL_PATH
        assert seen == [(path, 'Bearer fixture-key')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
