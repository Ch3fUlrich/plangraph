import json
import subprocess
from threading import Barrier
import pytest
from plangraph.spawner import remote_ps
from plangraph.spawner.mcp_limits import ps_reply
def config(tmp_path, monkeypatch, hosts):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    path = tmp_path / 'plangraph' / 'ps-hosts.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'hosts': hosts}))

def test_no_config_is_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    local = [{'run_id': 'run-local', 'host': 'original'}]
    assert remote_ps.list_runs(local) == local

def test_empty_config_is_local_only(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, [])
    local = [dict(run_id='run-local', host='original')]
    assert remote_ps.list_runs(local) == local

def test_parallel_partial_and_private(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, [dict(name=f'node-{i}', target=f'private-{i}')
                                 for i in range(3)])
    in_flight = Barrier(3)
    def transport(host, timeout):
        assert 0 < timeout <= 3
        # The timeout only prevents a deadlock; no elapsed-time performance bound.
        in_flight.wait(timeout=10)
        if host['name'] == 'node-1':
            raise subprocess.TimeoutExpired('SECRET', timeout)
        if host['name'] == 'node-2':
            return 'SECRET malformed'
        return json.dumps({'runs': [dict(run_id='run-abc', host='true-host',
                                        status='running', stderr='SECRET')]})
    rows = remote_ps.list_runs([], transport=transport)
    assert rows[0] == dict(run_id='run-abc', host='true-host', status='running')
    assert [r['host'] for r in rows[1:]] == ['node-1', 'node-2']
    assert all(r['status'] == 'unknown' for r in rows[1:])
    assert 'SECRET' not in json.dumps(rows)
    assert 'private-' not in json.dumps(rows)
    compact = ps_reply([dict(status='completed') for _ in range(25)] + rows)
    assert len([r for r in compact['runs'] if r['status'] == 'unknown']) == 2
    assert all(r['reason'] == 'host query failed' for r in compact['runs']
               if r['status'] == 'unknown')


@pytest.mark.parametrize('src', [None, '/opt/plangraph/src', '~/code/plangraph/src',
                                 r'C:\code\plangraph\src', 'D:/code/src', 'C:\\src\\'])
def test_transport_is_bounded_and_noninteractive(monkeypatch, src):
    original = subprocess.Popen
    host = dict(target='private-target')
    command = 'python3 -m plangraph.spawner.remote_ps'
    if src is not None:
        host['src'] = src
        command = ('python3 -c "import os,sys,runpy;'
                   f"sys.path.insert(0,os.path.expanduser(r'{src.replace(chr(92), '/')}'));"
                   "runpy.run_module('plangraph.spawner.remote_ps',run_name='__main__')\"")
    def run(argv, **kwargs):
        assert argv == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                        '--', 'private-target', command]
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['stdout'] == subprocess.PIPE
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs.get('shell', False) is False
        return original(['python3', '-c', 'print(\'{"runs": []}\', end="")'], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', run)
    assert remote_ps.query_host(host, 2) == '{"runs": []}'

@pytest.mark.parametrize('src', ['/opt/source tree', '/opt/src;id', '$(id)',
                                   '-src', 'relative/src', '~user/src', '', None,
                                    1, [], '/src\n', '/src$(id)', 'C:relative',
                                    *['C:\\src' + c + 'bad' for c in
                                      [' ', "'", '"', ';', '&', '|', '$', '%', '^', '<', '>', '`']],
                                    '-C:\\src'])
def test_invalid_src_is_invalid_config(tmp_path, monkeypatch, src):
    config(tmp_path, monkeypatch, [dict(name='node-a', target='target', src=src)])
    def transport(*args):
        pytest.fail('invalid src must not reach SSH')
    assert remote_ps.list_runs([], transport=transport) == [
        dict(host='invalid-config', status='unknown', reason='host query failed')]


@pytest.mark.parametrize('src', ['/opt/plangraph/src', '~/code/plangraph/src',
                                 r'C:\code\plangraph\src', 'D:/code/src', 'C:\\src\\'])
def test_config_accepts_src(tmp_path, monkeypatch, src):
    from unittest.mock import Mock, ANY
    host = dict(name='node-a', target='target', src=src)
    config(tmp_path, monkeypatch, [host])
    transport = Mock(return_value='{"runs": []}')
    assert remote_ps.list_runs([], transport=transport) == []
    transport.assert_called_once_with(host, ANY)
@pytest.mark.parametrize('payload', ['{}', '{"runs": {}}', '{"runs": [null]}',
                                     '{"runs": [{"run_id": "run-a"}]}'])
def test_malformed_is_named_unknown(payload):
    assert remote_ps._fetch(dict(name='node-a'), lambda *args: payload) == [
        dict(host='node-a', status='unknown', reason='host query failed')]
def test_unreachable_does_not_log_secrets(caplog):
    def fail(*args):
        raise OSError('private-address secret-token')
    assert remote_ps._fetch(dict(name='node-a'), fail)[0]['status'] == 'unknown'
    assert caplog.text == ''


def test_overflow_counts_only_local_with_hosts(tmp_path, monkeypatch):
    from plangraph.spawner import cli, overflow, stall_watchdog
    from plangraph.spawner.service import SpawnService
    config(tmp_path, monkeypatch, [dict(name='node-a', target='private-target')])
    local = [dict(run_id='local', status='running', started=1),
             dict(run_id='ended', status='completed', started=0)]
    monkeypatch.setattr(stall_watchdog, 'read_runs', lambda state: local)
    real_list = remote_ps.list_runs
    def transport(host, timeout):
        return json.dumps({'runs': [dict(run_id='remote', host='node-a', status='running')]})
    monkeypatch.setattr(remote_ps, 'list_runs',
                        lambda rows: real_list(rows, transport=transport))
    monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: SpawnService(state=tmp_path))
    assert len(remote_ps.list_runs(local)) == 3
    assert overflow.local_resources()[0] == 1
    assert SpawnService(state=tmp_path).ps() == {'runs': local}


@pytest.mark.parametrize('surface', ['cli', 'mcp'])
def test_ps_surfaces_use_fanout(tmp_path, monkeypatch, capsys, surface):
    import asyncio
    from plangraph.spawner import cli
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.spawner.service import SpawnService
    local = dict(run_id='local', host='local', status='running')
    remote = dict(run_id='remote', host='node-a', status='running')
    monkeypatch.setattr(SpawnService, 'ps', lambda self: {'runs': [local]})
    calls = []
    def fanout(rows):
        calls.append(rows)
        return rows + [remote, dict(host='node-b', status='unknown', reason='host query failed')]
    monkeypatch.setattr(remote_ps, 'list_runs', fanout)
    if surface == 'mcp':
        result = asyncio.run(MCPSpawnerServer(state=tmp_path).call_tool('plangraph_agent_ps', {}))
    else:
        monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: SpawnService(state=tmp_path))
        cli.main(['ps'])
        result = json.loads(capsys.readouterr().out)
    assert calls == [[local]]
    assert [row.get('run_id') for row in result['runs']] == ['local', 'remote', None]
    assert result['runs'][-1] == dict(host='node-b', status='unknown', reason='host query failed')


@pytest.mark.parametrize('contents', ['not json', '{}', 'null', '[]',
    '{"hosts": null}', '\udcff',
    '{"hosts": [{"name": "node-a", "target": "SECRET"}]}!'])
def test_invalid_config_file_preserves_local_listing(tmp_path, monkeypatch, caplog, contents):
    config(tmp_path, monkeypatch, [])
    path = tmp_path / 'plangraph' / 'ps-hosts.json'
    path.write_bytes(contents.encode('utf-8', errors='surrogateescape'))
    local = [dict(run_id='local', status='running', started=1)]
    assert remote_ps.list_runs(local) == local + [
        dict(host='invalid-config', status='unknown', reason='host query failed')]
    assert caplog.text == ''


@pytest.mark.parametrize('contents', ['{"hosts": [null]}',
    '{"hosts": [{"name": 1, "target": "SECRET"}]}',
    '{"hosts": [{"name": "node-a", "target": []}]}',
    '{"hosts": [{"name": "node-a", "target": "-SECRET"}]}'])
def test_invalid_config_preserves_local_listing(tmp_path, monkeypatch, caplog, contents):
    config(tmp_path, monkeypatch, [])
    path = tmp_path / 'plangraph' / 'ps-hosts.json'
    path.write_bytes(contents.encode('utf-8', errors='surrogateescape'))
    local = [dict(run_id='local', status='running', started=1)]
    rows = remote_ps.list_runs(local)
    assert rows == local + [
        dict(host='invalid-config', status='unknown', reason='host query failed')]
    assert caplog.text == ''


def test_unreadable_config_preserves_local_listing(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    def denied(*args, **kwargs):
        raise PermissionError('SECRET')
    monkeypatch.setattr(Path, 'read_text', denied)
    local = [dict(run_id='local', status='running')]
    assert remote_ps.list_runs(local) == local + [
        dict(host='invalid-config', status='unknown', reason='host query failed')]
    assert caplog.text == ''


@pytest.mark.parametrize('field', ['host', 'run_id', 'role', 'status'])
@pytest.mark.parametrize('value', ['a\nFAKE', 'a\rFAKE', 'a\tFAKE',
                                   'a\x1b[31mFAKE', 'a\x00FAKE', 'a\x7fFAKE',
                                   'a\x9b31mFAKE'])
def test_remote_identifiers_reject_controls(field, value, caplog):
    row = dict(run_id='run-a', host='node-a', status='running')
    row[field] = value
    rows = remote_ps._fetch(dict(name='node-a'), lambda *args: json.dumps({'runs': [row]}))
    assert rows == [dict(host='node-a', status='unknown', reason='host query failed')]
    assert ps_reply(rows)['runs'] == rows
    assert caplog.text == ''


@pytest.mark.parametrize('field', remote_ps.FIELDS)
@pytest.mark.parametrize('value', [{ 'SECRET': 1 }, [], True])
def test_every_remote_field_is_typed(field, value, caplog):
    row = dict(run_id='run-a', host='node-a', status='running', role='implement',
               started=1.5, ended=None, pid=123, exit_code=None)
    row[field] = value
    payload = json.dumps({'runs': [dict(run_id='valid', host='node-a',
                                         status='queued'), row]})
    assert remote_ps._fetch(dict(name='node-a'), lambda *args: payload) == [
        dict(host='node-a', status='unknown', reason='host query failed')]
    assert caplog.text == ''


@pytest.mark.parametrize('ended,exit_code,pid', [(None, None, None), (2.5, 0, 123)])
def test_valid_remote_field_types(ended, exit_code, pid):
    row = dict(run_id='run-a', host='node-a', status='running', role='implement',
               started=1, ended=ended, pid=pid, exit_code=exit_code)
    assert remote_ps._fetch(dict(name='node-a'), lambda *args: json.dumps(
        {'runs': [row]})) == [row]


def test_long_identifiers():
    row = dict(run_id='r' * 256, host='h' * 253, status='running')
    assert remote_ps._fetch(dict(name='node-a'), lambda *a: json.dumps({'runs': [row]})) == [row]


def test_local_reason_is_private():
    assert 'reason' not in ps_reply([dict(status='failed', reason='private')])['runs'][0]


def test_mixed_config(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, [dict(name='node-a', target='target'),
                                   dict(name='node-b', target='-invalid')])
    calls = []
    def transport(host, timeout):
        calls.append(host['name'])
        return '{"runs": []}'
    rows = remote_ps.list_runs([], transport=transport)
    assert calls == ['node-a']
    assert rows == [dict(host='invalid-config', status='unknown', reason='host query failed')]


@pytest.mark.parametrize('script, budget, error', [
    ('import sys; sys.stdout.write("x" * 2097152)', 10, ValueError),
    ('raise SystemExit(1)', 10, ValueError),
    ('import time; time.sleep(60)', 1, TimeoutError),
])
def test_transport_rejects_overflow_exit_and_timeout(monkeypatch, script, budget, error):
    original = subprocess.Popen
    monkeypatch.setattr(subprocess, 'Popen',
                        lambda argv, **kw: original(['python3', '-c', script], **kw))
    with pytest.raises(error):
        remote_ps.query_host(dict(target='target'), budget)


def test_transport_overflow_names_host(monkeypatch):
    """Error-path contract guard, not fix coverage: base already names the host."""
    original = subprocess.Popen
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: original(
        ['python3', '-c', 'import json; print(json.dumps({"runs": ['
         '{"run_id": "x" * 2097152, "host": "node-a", "status": "running"}]}))'], **kw))
    rows = remote_ps._fetch(dict(name='node-a', target='target'),
                           lambda host, _: remote_ps.query_host(host, 10))
    assert rows == [dict(host='node-a', status='unknown', reason='host query failed')]


def test_worker_count_is_capped(monkeypatch):
    hosts = [dict(name='node-a') for _ in range(100)]
    monkeypatch.setattr(remote_ps, 'configured_hosts', lambda: hosts)
    original = remote_ps.ThreadPoolExecutor
    def pool(**kwargs):
        assert kwargs['max_workers'] <= 16
        return original(**kwargs)
    monkeypatch.setattr(remote_ps, 'ThreadPoolExecutor', pool)
    assert remote_ps.list_runs([], transport=lambda *a: '{"runs": []}') == []


@pytest.mark.parametrize('xdg', ['', None])
def test_default_config_location(tmp_path, monkeypatch, xdg):
    from pathlib import Path
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    hosts = [dict(name='node-a', target='target')]
    default = tmp_path / '.config'
    default.mkdir()
    config(default, monkeypatch, hosts)
    if xdg is None:
        monkeypatch.delenv('XDG_CONFIG_HOME')
    else:
        monkeypatch.setenv('XDG_CONFIG_HOME', xdg)
    assert remote_ps.configured_hosts() == hosts


def test_collect_local_protocol(monkeypatch):
    from plangraph.spawner import stall_watchdog
    state = object()
    def read_runs(actual):
        assert actual is state
        return [dict(run_id='r', host='h', status='running', private='omitted')]
    monkeypatch.setattr(stall_watchdog, 'read_runs', read_runs)
    assert remote_ps.collect_local(state) == {'runs': [dict(run_id='r', host='h', status='running')]}


@pytest.mark.parametrize('status,terminal', [
    ('running', False), ('queued', False), ('needs_input', False),
    ('stalled', False), ('future_status', False), ('unknown', False),
    ('completed', True), ('failed', True), ('cancelled', True),
    ('killed', True), ('dry_run', True), ('succeeded', True),
    ('timeout', True), ('refused', True), ('skipped', True),
])
@pytest.mark.parametrize('path', ['local', 'remote'])
def test_status_visibility(monkeypatch, status, terminal, path):
    from plangraph.spawner import stall_watchdog
    row = dict(run_id='run-a', host='node-a', status=status)
    if path == 'local':
        monkeypatch.setattr(stall_watchdog, 'read_runs', lambda state: [row])
        rows = remote_ps.collect_local(object())['runs']
    else:
        rows = remote_ps._fetch(dict(name='node-a'),
                                lambda *a: json.dumps({'runs': [row]}))
    assert rows == ([] if terminal else [row])


@pytest.mark.parametrize('terminal', [
    dict(status='completed', run_id=['malformed'], host=None),
    dict(status='failed', run_id='x' * remote_ps.MAX_OUTPUT),
])
def test_terminal_history_never_hides_live_runs(monkeypatch, terminal):
    from plangraph.spawner import stall_watchdog
    live = dict(run_id='live', host='node-a', status='running')
    monkeypatch.setattr(stall_watchdog, 'read_runs', lambda state: [terminal, live])
    payload = json.dumps(remote_ps.collect_local(object()))
    assert len(payload.encode()) < remote_ps.MAX_OUTPUT
    assert json.loads(payload) == {'runs': [live]}
    assert remote_ps._fetch(dict(name='node-a'), lambda *a: payload) == [live]


def test_malformed_terminal_reply_does_not_hide_live_run():
    live = dict(run_id='live', host='node-a', status='queued')
    payload = json.dumps({'runs': [dict(status='completed', host=None, pid=[]), live]})
    assert remote_ps._fetch(dict(name='node-a'), lambda *a: payload) == [live]


def test_protocol_main(monkeypatch, capsys):
    import runpy
    from plangraph.spawner import service, stall_watchdog
    state = object()
    monkeypatch.setattr(service, 'SpawnService', lambda: type('Service', (), {'state': state})())
    def read_runs(actual):
        assert actual is state
        return [dict(run_id='r', status='queued', private='omitted')]
    monkeypatch.setattr(stall_watchdog, 'read_runs', read_runs)
    with pytest.warns(RuntimeWarning):
        runpy.run_module(remote_ps.__name__, run_name='__main__')
    assert json.loads(capsys.readouterr().out) == {'runs': [dict(run_id='r', status='queued')]}


def test_cli_transport_failure(tmp_path, monkeypatch, capsys):
    from plangraph.spawner import cli
    from plangraph.spawner.service import SpawnService
    config(tmp_path, monkeypatch, [dict(name='node-a', target='target')])
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SpawnService(state=tmp_path))
    real_list = remote_ps.list_runs
    def fail(*a):
        raise OSError('unavailable')
    monkeypatch.setattr(remote_ps, 'list_runs', lambda rows: real_list(rows, transport=fail))
    cli.main(['ps'])
    assert json.loads(capsys.readouterr().out)['runs'] == [
        dict(host='node-a', status='unknown', reason='host query failed')]


def test_executor_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(remote_ps, 'configured_hosts', lambda: [dict(name='node-a')])
    def broken(**kwargs):
        assert kwargs['max_workers'] <= 16
        raise RuntimeError('unavailable')
    monkeypatch.setattr(remote_ps, 'ThreadPoolExecutor', broken)
    local = [dict(status='running')]
    assert remote_ps.list_runs(local) == local + [dict(host='node-a', status='unknown', reason='host query failed')]


def test_mcp_ps_does_not_block(tmp_path, monkeypatch):
    import asyncio
    from threading import Event
    from plangraph.spawner.mcp import MCPSpawnerServer
    entered, release = Event(), Event()
    def fanout(rows):
        entered.set()
        assert release.wait(1)
        return rows
    monkeypatch.setattr(remote_ps, 'list_runs', fanout)
    async def check():
        task = asyncio.create_task(MCPSpawnerServer(state=tmp_path).call_tool('plangraph_agent_ps', {}))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.sleep(0)
        finally:
            release.set()
        await task
    asyncio.run(check())
