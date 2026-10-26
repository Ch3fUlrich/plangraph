import asyncio
import json
import os
import time

import pytest

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
    from plangraph.spawner.notify import KeeperSink, KeeperNotConfigured
    try:
        KeeperSink(url='', token_file='').deliver('keeper', {})
    except ValueError as exc:
        assert isinstance(exc, KeeperNotConfigured)
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


def test_unconfigured_keeper_delivery_quiet_and_retry(tmp_path, monkeypatch, caplog):
    """Three terminal records without keeper token log exactly one warning without traceback."""
    import logging
    import time
    import urllib.request
    from plangraph.spawner import runner
    from plangraph.spawner.runner import SpawnerRunner, spawn_service
    from plangraph.spawner import stall_watchdog as w

    monkeypatch.delenv('PG_URL', raising=False)
    monkeypatch.delenv('PG_TOKEN_FILE', raising=False)
    clock = [1000.0]
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(runner, '_keeper_unconfigured_last_warned', None)
    monkeypatch.setenv('PLANGRAPH_SPAWN_STATE', str(tmp_path))
    caplog.set_level(logging.WARNING)

    service = spawn_service(runner=SpawnerRunner(), state=tmp_path)
    run_ids = ['run-t1', 'run-t2', 'run-t3']
    for run_id in run_ids:
        rec = dict(run_id=run_id, status='completed', exit_code=0,
                   role='implement', attempts=[], started=time.time(),
                   ended=time.time(), stdout_tail='', stderr_tail='',
                   item='plangraph:STALL-WD-DELIVERY-QUIET')
        service._save(rec)

    # a. three terminal records, KeeperSink without token -> exactly one warning record logged (caplog),
    #    no record with exc_info, no keeper.delivered marker.
    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1
    assert 'completion delivery skipped: keeper not configured (queued)' in warnings[0].message
    assert not any(r.exc_info for r in caplog.records)
    for run_id in run_ids:
        run_dir = tmp_path / 'runs' / run_id
        assert (run_dir / 'parent.delivered').exists()
        assert not (run_dir / 'keeper.delivered').exists()

    # Retry or watchdog tick re-checks pending records without logging new warnings or tracebacks
    # Second pass 10 s later -> 0 new warnings
    clock[0] += 10.0
    monkeypatch.setattr(w, 'read_claims', lambda: [])
    monkeypatch.setattr(w, 'read_activity', lambda state, s: None)
    w.main(['tick'])
    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1
    assert not any(r.exc_info for r in caplog.records)
    for run_id in run_ids:
        assert not (tmp_path / 'runs' / run_id / 'keeper.delivered').exists()

    # Pass 601 s later -> 1 new warning
    clock[0] += 601.0
    w.main(['tick'])
    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 2
    assert 'completion delivery skipped: keeper not configured (queued)' in warnings[1].message
    assert not any(r.exc_info for r in caplog.records)
    for run_id in run_ids:
        assert not (tmp_path / 'runs' / run_id / 'keeper.delivered').exists()

    # b. after setting a token file + url with a fake/stub delivery (monkeypatch the HTTP call the sink makes)
    #    the next _save/tick delivers and writes the marker.
    token_file = tmp_path / 'token'
    token_file.write_text('test-token', encoding='utf-8')
    monkeypatch.setenv('PG_URL', 'http://127.0.0.1:9090')
    monkeypatch.setenv('PG_TOKEN_FILE', str(token_file))

    delivered = []
    class StubResponse:
        status = 200
        def read(self):
            return b'{}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def stub_open(self, request, *args, **kwargs):
        delivered.append(request)
        return StubResponse()

    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', stub_open)

    # Next watchdog tick delivers completions and writes keeper.delivered
    w.main(['tick'])

    for run_id in run_ids:
        assert (tmp_path / 'runs' / run_id / 'keeper.delivered').read_text() == 'delivered\n'
    assert len(delivered) == 3

    # Subsequent tick does not re-deliver
    w.main(['tick'])
    assert len(delivered) == 3

    # Direct _save also delivers immediately when configured
    rec4 = dict(run_id='run-t4', status='completed', exit_code=0,
                role='implement', attempts=[], started=time.time(),
                ended=time.time(), stdout_tail='', stderr_tail='',
                item='plangraph:STALL-WD-DELIVERY-QUIET')
    spawn_service(runner=SpawnerRunner(), state=tmp_path)._save(rec4)
    assert (tmp_path / 'runs' / 'run-t4' / 'keeper.delivered').read_text() == 'delivered\n'
    assert len(delivered) == 4


@pytest.mark.parametrize('exit_code', [0, 1])
def test_tree_killer_invoked_on_exit(exit_code):
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner

    killed_pids = []

    class FakeProcess:
        pid = 9876
        returncode = exit_code

        async def communicate(self):
            return b'stdout', b'stderr'

    async def fake_factory(*args, **kwargs):
        return FakeProcess()

    async def fake_tree_killer(pid):
        killed_pids.append(pid)

    async def run():
        runner = SpawnerRunner(process_factory=fake_factory, tree_killer=fake_tree_killer)
        job = await runner.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.exit_code == exit_code
        assert killed_pids == [9876]

    asyncio.run(run())


def test_tree_killer_invoked_once_on_timeout_no_double_kill(monkeypatch):
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner

    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '0.05')
    killed_pids = []

    class HangingProcess:
        pid = 9876
        returncode = 124

        def __init__(self):
            self._unblock = asyncio.Event()

        async def communicate(self):
            await self._unblock.wait()
            return b'stdout', b'stderr'

    proc = HangingProcess()

    async def fake_factory(*args, **kwargs):
        return proc

    async def fake_tree_killer(pid):
        killed_pids.append(pid)
        proc._unblock.set()

    async def run():
        runner = SpawnerRunner(process_factory=fake_factory, tree_killer=fake_tree_killer)
        job = await runner.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.exit_code == 124
        assert killed_pids == [9876]

    asyncio.run(run())


def test_tree_killer_process_lookup_error_tolerated():
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner

    killed_pids = []

    class FakeProcess:
        pid = 9876
        returncode = 0

        async def communicate(self):
            return b'stdout', b'stderr'

    async def fake_factory(*args, **kwargs):
        return FakeProcess()

    async def fake_tree_killer(pid):
        killed_pids.append(pid)
        raise ProcessLookupError("process lookup error")

    async def run():
        runner = SpawnerRunner(process_factory=fake_factory, tree_killer=fake_tree_killer)
        job = await runner.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.status == 'completed'
        assert job.exit_code == 0
        assert killed_pids == [9876]

    asyncio.run(run())


def test_tree_killer_permission_error_tolerated():
    import asyncio
    from plangraph.spawner.runner import SpawnerRunner

    killed_pids = []

    class FakeProcess:
        pid = 9876
        returncode = 0

        async def communicate(self):
            return b'stdout', b'stderr'

    async def fake_factory(*args, **kwargs):
        return FakeProcess()

    async def fake_tree_killer(pid):
        killed_pids.append(pid)
        raise PermissionError("operation not permitted")

    async def run():
        runner = SpawnerRunner(process_factory=fake_factory, tree_killer=fake_tree_killer)
        job = await runner.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.status == 'completed'
        assert job.exit_code == 0
        assert killed_pids == [9876]

    asyncio.run(run())


def test_reap_failure_recorded_not_swallowed():
    """PermissionError from the tree killer gives exactly one reap_failed event.

    The bare except in _reap_process silently swallowed mis-kills and
    Windows/unrelated failures (F2): keep ProcessLookupError silent (the group
    is already gone) but surface anything else as a type-name-only event.
    """
    from plangraph.spawner.runner import SpawnerRunner

    class FakeProcess:
        pid = 9876
        returncode = 0

        async def communicate(self):
            return b'stdout', b'stderr'

    async def fake_factory(*args, **kwargs):
        return FakeProcess()

    def fake_tree_killer(pid):
        raise PermissionError('operation not permitted')

    async def run():
        runner = SpawnerRunner(process_factory=fake_factory, tree_killer=fake_tree_killer)
        job = await runner.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.status == 'completed'
        assert [e for e in job.events if e.get('event') == 'reap_failed'] == [
            {'event': 'reap_failed', 'error': 'PermissionError'}]

    asyncio.run(run())


def _pid_gone(pid):
    """True once pid is dead or a zombie awaiting a non-reaping init."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        state = open(f'/proc/{pid}/stat').read().rsplit(') ', 1)[1][0]
    except OSError:
        return False
    return state == 'Z'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process groups')
def test_run_exit_reaps_surviving_grandchild(tmp_path):
    """A child that outlives its run must not survive the run's scope (SPAWN-ORPHAN-REAP).

    The incident (`opencode.exe serve` outliving its run by 3 h) is a surviving
    grandchild holding ports: prove by real pids, with the default tree killer,
    that the reaper reaches it — the stubbed tree_killer tests cannot.
    """
    import sys
    from plangraph.spawner.runner import SpawnerRunner

    pid_file = tmp_path / 'grandchild.pid'
    script = tmp_path / 'run_that_orphans.py'
    script.write_text(
        "import subprocess, sys\n"
        # stdio to DEVNULL: the run's pipes must close at exit so the job completes
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n")

    async def script_factory(*args, **kwargs):
        return await asyncio.create_subprocess_exec(sys.executable, str(script), **kwargs)

    async def run():
        runner = SpawnerRunner(process_factory=script_factory)  # default tree killer
        job = await runner.spawn('prompt', model_or_combo='mimo/mimo-v2.6-flash')
        await runner._tasks[job.job_id]
        assert job.exit_code == 0
        grandchild = int(pid_file.read_text())
        deadline = time.time() + 5
        while not _pid_gone(grandchild):
            assert time.time() < deadline, f'grandchild {grandchild} survived the run'
            time.sleep(0.05)

    asyncio.run(run())


