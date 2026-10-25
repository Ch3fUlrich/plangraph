import json

from plangraph.spawner.cli import _drain_chunks
from plangraph.spawner.env_redact import redact_env_values


def test_utf8_chunk_boundary_preserves_characters():
    output = []
    data = ('x' * 65535 + '€' + 'y' * 5000).encode()
    _drain_chunks([data[:65536], data[65536:]], output.append)
    text = b''.join(output).decode()
    assert '�' not in text
    assert '€' in text


def test_provider_port_is_not_a_secret():
    env = {'PORT': '8080', 'OPENCODE_CONFIG_CONTENT': json.dumps(
        {'providers': {'local': {'env': ['PORT']}}})}
    assert redact_env_values('listening on 8080', env) == 'listening on 8080'


def test_signed_numeric_env_values_are_not_secrets():
    """The numeric carve-out takes optionally signed decimals, nothing else.

    "-1" and "+8080" are configuration (sentinel, port), so a configured
    provider env name must not be redacted; "1_000" is not a decimal digit
    string and stays redacted along with every other literal form.
    """
    env = {'OPENCODE_CONFIG_CONTENT': json.dumps(
               {'providers': {'local': {'env': ['OFFSET', 'PORT', 'TICKS', 'BASE']}}}),
           'OFFSET': '-1', 'PORT': '+8080', 'TICKS': '1_000', 'BASE': '0x1F'}
    text = 'offset=-1 port=+8080 ticks=1_000 base=0x1F'
    assert redact_env_values(text, env) == 'offset=-1 port=+8080 ticks=[autoos:redacted] base=[autoos:redacted]'
    # An unsigned decimal keeps the carve-out it always had.
    plain = dict(env, PORT='8080')
    assert redact_env_values('port=8080', plain) == 'port=8080'


def test_keeper_sink_requires_explicit_identity():
    from plangraph.spawner.notify import KeeperSink
    try:
        KeeperSink(url='', token_file='').deliver('keeper', {})
    except ValueError as exc:
        assert 'PG_URL' in str(exc) and 'PG_TOKEN_FILE' in str(exc)
    else:
        raise AssertionError('missing keeper identity accepted')


def test_keeper_sink_http_finished_payload():
    import tempfile
    import threading
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from plangraph.spawner.notify import KeeperSink
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == '/v1/record_run'
            assert self.headers['Authorization'] == 'Bearer fake-token'
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(dir='.') as directory:
            token = Path(directory) / 'token'
            token.write_text('fake-token')
            sink = KeeperSink('http://127.0.0.1:' + str(server.server_port), str(token))
            for rc in (0, 1, -9, 124):
                sink.deliver('keeper', {'item': 'p:item', 'run_id': 'run-' + str(rc), 'rc': rc})
        assert [body['run']['rc'] for body in received] == [0, 1, -9, 124]
        assert all(body['run']['phase'] == 'finished' for body in received)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_keeper_redirect_is_never_an_acknowledgement(tmp_path, monkeypatch):
    """A keeper answering 302 -> 200 has acknowledged nothing: no marker, still retryable."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from plangraph.spawner.runner import SpawnerRunner, spawn_service

    mode = {'redirect': True}
    posted, followed = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            if mode['redirect']:
                self.send_response(302)
                self.send_header('Location', '/v1/record_run?ack=1')
                self.end_headers()
                return
            posted.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def do_GET(self):
            # urlopen would follow the302 here and see a 200.
            followed.append(self.path)
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
    monkeypatch.setenv('PG_URL', 'http://127.0.0.1:' + str(server.server_port))
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    monkeypatch.setenv('PLANGRAPH_SPAWN_ITEM', 'p:item')
    try:
        service = spawn_service(runner=SpawnerRunner(), state=tmp_path)
        record = dict(run_id='run-redirect', status='completed', exit_code=0,
                      role='implement', attempts=[], started=time.time(),
                      ended=time.time(), stdout_tail='', stderr_tail='')
        service._save(record)
        run_dir = tmp_path / 'runs' / 'run-redirect'
        assert not (run_dir / 'keeper.delivered').exists()
        assert (run_dir / 'parent.delivered').exists()   # the inbox sink did deliver
        # The redirect is never followed and no 2xx answer ever acknowledged it.
        assert not followed
        assert not posted
        # Still retryable: one clean 2xx later the marker lands exactly once.
        mode['redirect'] = False
        service.retry_completions()
        assert (run_dir / 'keeper.delivered').read_text() == 'delivered\n'
        assert len(posted) == 1
        service.retry_completions()
        assert len(posted) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
