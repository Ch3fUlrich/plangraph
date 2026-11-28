import base64
import json
import shlex
import sys
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


@pytest.mark.parametrize('fault,error', [
    ('offline', remote_ps.HostQueryError),
    ('wrong-day', ValueError),
])
def test_coverage_tick_uses_host_inventories_and_retries_incomplete_day(tmp_path, monkeypatch, fault, error):
    from plangraph.spawner.notify import KeeperSink
    from plangraph.spawner.stall_watchdog import attempt_coverage_tick

    config(tmp_path, monkeypatch, [
        dict(name='node-a', target='coverage-a'),
        dict(name='node-b', target='coverage-b', wsl_distro='Fixture', src='/fixture/src'),
    ])
    day = '2023-11-14'
    local = dict(run_id='run-local', host='local')
    remote = dict(run_id='run-remote', host='node-a')
    fleet = dict(run_id='run-fleet', host='node-b')
    reported = []

    def lifecycle(state, verb, *args, **kwargs):
        assert state == tmp_path
        if verb == 'attempt-coverage-input':
            assert args == (day,)
            payload = dict(day=day, runs=[local], fleet=[])
        else:
            assert verb == 'attempt-coverage' and not args
            reported.append(json.loads(kwargs['input']))
            payload = dict(day=day, below_threshold=False)
        return subprocess.CompletedProcess([verb], 0, stdout=json.dumps(payload))

    popen = subprocess.Popen
    query = ['pg-spawnd', 'attempt-coverage-input', day]

    def ssh(argv, **kwargs):
        assert argv[:6] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2', '--']
        target, command = argv[6:]
        if target == 'coverage-a':
            assert command == ' '.join(query)
            assert kwargs['stdin'] == subprocess.DEVNULL
            payload = dict(day=day, runs=[local, remote], fleet=[])
            return popen([sys.executable, '-c', 'import sys; print(sys.argv[1])',
                          json.dumps(payload)], **kwargs)
        assert target == 'coverage-b'
        assert command == 'wsl.exe --distribution Fixture --exec pg-spawnd steps'
        assert kwargs['stdin'] == subprocess.PIPE
        payload = dict(day='2023-11-13' if fault == 'wrong-day' else day,
                       runs=[], fleet=[remote, fleet])
        peer = ('import json, sys; '
                'assert json.load(sys.stdin) == {"steps": [{"argv": json.loads(sys.argv[1])}]}; '
                'print(sys.argv[2]); sys.exit(int(sys.argv[3]))')
        return popen([sys.executable, '-c', peer, json.dumps(query), json.dumps(payload),
                      '255' if fault == 'offline' else '0'], **kwargs)

    monkeypatch.setattr(remote_ps, 'lifecycle_command', lifecycle)
    monkeypatch.setattr(KeeperSink, 'attempt_coverage', lambda self, day: [local, remote, fleet])
    monkeypatch.setattr(subprocess, 'Popen', ssh)
    now = 1700006400 + 3600
    with pytest.raises(error):
        attempt_coverage_tick(tmp_path, now=now)
    reports = tmp_path / 'watchdog/attempt-coverage'
    assert (reports / 'next-day').read_text() == day + '\n'
    assert not reported and not list(reports.glob('*.json'))

    fault = None
    assert attempt_coverage_tick(tmp_path, now=now + 86400) == []
    assert reported == [dict(day=day, runs=[local, local, remote],
                             fleet=[remote, fleet], keeper=[local, remote, fleet])]
    assert (reports / (day + '.json')).exists()
    assert (reports / 'next-day').read_text() == '2023-11-15\n'


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
    assert rows[0] == dict(run_id='run-abc', host='node-0', status='running')
    assert [r['host'] for r in rows[1:]] == ['node-1', 'node-2']
    assert all(r['status'] == 'unknown' for r in rows[1:])
    assert 'SECRET' not in json.dumps(rows)
    assert 'private-' not in json.dumps(rows)
    compact = ps_reply([dict(status='completed') for _ in range(25)] + rows)
    assert len([r for r in compact['runs'] if r['status'] == 'unknown']) == 2
    assert all(r['reason'] == 'host query failed' for r in compact['runs']
               if r['status'] == 'unknown')
    assert compact['runs'][1]['error'] == 'TimeoutExpired'


@pytest.mark.parametrize('src,distro', [(s, None) for s in [None, '/opt/plangraph/src',
    '~/code/plangraph/src']] +
    [(s, d) for s in ['/opt/plangraph/src', '~/code/plangraph/src']
     for d in ['Ubuntu-24.04', 'a_B.9-', '.distro', '_distro', '1']])
def test_transport_is_bounded_and_noninteractive(monkeypatch, tmp_path, src, distro):
    original = subprocess.Popen
    host = dict(target='private-target')
    command = 'python3 -m plangraph.spawner.remote_ps'
    steps = [{'argv': ['python3', '-m', 'plangraph.spawner.remote_ps']}]
    if src is not None:
        host['src'] = src
        quoted = '"$HOME"/code/plangraph/src' if src.startswith('~/') else src
        command = f'test -d {quoted} || exit 44; PYTHONPATH={quoted} {command}'
        steps = [{'argv': ['test', '-d', src], 'fail_code': 44},
                 {'argv': ['python3', '-m', 'plangraph.spawner.remote_ps'], 'env': {'PYTHONPATH': src}}]
    if distro is not None:
        host['wsl_distro'] = distro
        command = f'wsl.exe --distribution {distro} --exec pg-spawnd steps'
    received = tmp_path / 'plan.json'
    def run(argv, **kwargs):
        assert argv == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2',
                        '--', 'private-target', command]
        assert kwargs['stdin'] == (subprocess.DEVNULL if distro is None else subprocess.PIPE)
        assert kwargs['stdout'] == subprocess.PIPE
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs.get('shell', False) is False
        child = ('import sys,pathlib;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read());'
                 'print(\'{"runs": []}\', end="")')
        return original([sys.executable, '-c', child, str(received)], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', run)
    assert remote_ps.query_host(host, 2) == '{"runs": []}'
    if distro is not None:
        assert json.loads(received.read_text()) == {'steps': steps}

def test_wsl_hop_sets_registry_path(tmp_path, monkeypatch):
    guest = dict(name='node-a', target='target', src='/srv/repo/src', wsl_distro='Ubuntu')
    config(tmp_path, monkeypatch, [dict(guest, env={'PATH': '/stale/bin'})])
    (tmp_path / 'plangraph' / 'hosts.json').write_text(json.dumps({'hosts': [dict(
        id='node-a', transport='ssh', os='linux', ssh_target='target', wsl_distro='Ubuntu',
        repos={'plangraph': {'root': '/srv/repo'}}, caps={'mem_floor_mb': 1},
        env={'PATH': '/srv/worker/.local/bin:/usr/bin:/bin'})]}))
    host, = remote_ps.configured_hosts()
    command, body = remote_ps.host_invocation(host, query=True)
    assert command == ('wsl.exe --distribution Ubuntu --exec /usr/bin/env '
                       'PATH=/srv/worker/.local/bin:/usr/bin:/bin pg-spawnd steps')
    assert json.loads(body)['steps'][-1]['argv'] == ['python3', '-m', 'plangraph.spawner.remote_ps']


@pytest.mark.skipif(sys.platform == 'win32', reason='fixture executes the Linux guest argv')
def test_wsl_hop_without_login_path_finds_tools(tmp_path):
    (tmp_path / 'pg-spawnd').symlink_to(sys.executable)
    (tmp_path / 'steps').write_text('import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n')
    host = {'wsl_distro': 'Ubuntu', 'env': {'PATH': f'{tmp_path}:/usr/bin:/bin'}}
    command, body = remote_ps.invocation(host, 'true')
    argv = shlex.split(command)
    assert argv[:4] == ['wsl.exe', '--distribution', 'Ubuntu', '--exec']
    output = subprocess.run(argv[4:], cwd=tmp_path, input=body, capture_output=True, check=True,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)})
    assert json.loads(output.stdout) == {'host_env_defaults': True, 'steps': [{'argv': ['true']}]}


@pytest.mark.parametrize('wsl', [False, True])
@pytest.mark.parametrize('src', ['/opt/src;id', '$(id)',
                                   '-src', 'relative/src', '~user/src', '', None,
                                    1, [], '/src\n', '/src$(id)'])
def test_invalid_src_is_invalid_config(tmp_path, monkeypatch, src, wsl):
    host = dict(name='node-a', target='target', src=src)
    if wsl:
        host['wsl_distro'] = 'Ubuntu'
    config(tmp_path, monkeypatch, [host])
    def transport(*args):
        pytest.fail('invalid src must not reach SSH')
    assert remote_ps.list_runs([], transport=transport) == [
        dict(host='invalid-config', status='unknown', reason='host query failed')]


@pytest.mark.parametrize('src,wsl', [(s, False) for s in ['/opt/plangraph/src',
    '~/code/plangraph/src']] +
    [('/opt/plangraph/src', True), ('/', True)])
def test_config_accepts_src(tmp_path, monkeypatch, src, wsl):
    from unittest.mock import Mock, ANY
    host = dict(name='node-a', target='target', src=src)
    if wsl:
        host['wsl_distro'] = 'Ubuntu'
    config(tmp_path, monkeypatch, [host])
    transport = Mock(return_value='{"runs": []}')
    assert remote_ps.list_runs([], transport=transport) == []
    transport.assert_called_once_with(host, ANY)
@pytest.mark.parametrize('distro,src', [(d, '/src') for d in
    ['', '-Ubuntu', None, 1, True, [], {}, 'é', 'Ubuntu\n', 'a\r', 'a\t',
     *['a' + c + 'b' for c in ' /\\\'";&|$%^<>`()=:!']]] +
    [('Ubuntu', s) for s in [None, '~/src', 'C:/src', r'C:\src', '/src\\bad']])
def test_invalid_wsl_is_invalid_config(tmp_path, monkeypatch, distro, src):
    host = dict(name='node-a', target='target', wsl_distro=distro)
    if src is not None:
        host['src'] = src
    config(tmp_path, monkeypatch, [host])
    def transport(*args):
        pytest.fail('invalid WSL config must not reach SSH')
    assert remote_ps.list_runs([], transport=transport) == [
        dict(host='invalid-config', status='unknown', reason='host query failed')]
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
def test_overflow_counts_only_local_with_hosts(tmp_path, monkeypatch, admission_memory):
    admission_memory()
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
    assert remote_ps._fetch(dict(name='node-a'), lambda *a: json.dumps({'runs': [row]})) == [dict(row, host='node-a')]
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
                        lambda argv, **kw: original([sys.executable, '-c', script], **kw))
    with pytest.raises(error):
        remote_ps.query_host(dict(target='target'), budget)
def test_transport_overflow_names_host(monkeypatch):
    """Error-path contract guard, not fix coverage: base already names the host."""
    original = subprocess.Popen
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: original(
        [sys.executable, '-c', 'import json; print(json.dumps({"runs": ['
         '{"run_id": "x" * 2097152, "host": "node-a", "status": "running"}]}))'], **kw))
    rows = remote_ps._fetch(dict(name='node-a', target='target'),
                           lambda host, _: remote_ps.query_host(host, 10))
    assert rows == [dict(host='node-a', status='unknown', reason='host query failed', error='ValueError')]
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
        dict(host='node-a', status='unknown', reason='host query failed', error='OSError')]


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


def test_all_hosts_cli_matches_mcp(tmp_path, monkeypatch, capsys):
    import asyncio
    from plangraph.spawner import cli
    from plangraph.spawner.mcp import MCPSpawnerServer
    from plangraph.spawner.service import SpawnService
    local = dict(run_id='local', host='dell', status='running')
    merged = [local, dict(run_id='remote', host='ws-build', status='queued')]
    calls = []
    monkeypatch.setattr(SpawnService, 'ps', lambda self: {'runs': [local]})
    def fanout(rows):
        calls.append(rows)
        return merged
    monkeypatch.setattr(remote_ps, 'list_runs', fanout)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SpawnService(state=tmp_path))
    assert cli.main(['ps', '--all-hosts']) == 0
    cli_rows = json.loads(capsys.readouterr().out)['runs']
    reply = asyncio.run(MCPSpawnerServer(state=tmp_path).call_tool('plangraph_agent_ps', {}))
    assert cli_rows == reply['runs'] == merged
    assert calls == [[local], [local]]


def test_failed_query_reason_carries_safe_stderr(monkeypatch):
    original = subprocess.Popen
    script = ('import sys; sys.stderr.write("ModuleNotFoundError: token=private-value\\nSECOND\\n"); '
              'raise SystemExit(1)')
    monkeypatch.setattr(subprocess, 'Popen', lambda argv, **kw: original(
        [sys.executable, '-c', script], **kw))
    rows = remote_ps._fetch(dict(name='ws-build', target='private-target'), remote_ps.query_host)
    reason = rows[0]['reason']
    assert reason == 'host query failed'
    assert rows[0]['error'] == 'HostQueryError (SSH exit 1)'
    assert 'ModuleNotFoundError' not in json.dumps(rows)
    assert 'private-value' not in reason and 'SECOND' not in reason
    assert ps_reply(rows)['runs'][0]['reason'] == reason
    assert len(reason) <= 200


@pytest.mark.parametrize('code,stderr,attempts', [
    (255, 'ssh: connect timeout', 1), (1, 'remote command failed', 1),
    (44, 'source missing', 1), (0, '', 1),
])
def test_ssh_failure_is_not_retried(monkeypatch, code, stderr, attempts):
    original = subprocess.Popen
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        actual = code if len(calls) == 1 else 0
        script = f'import sys; print(\'{{"runs": []}}\'); sys.stderr.write({stderr!r}); sys.exit({actual})'
        return original([sys.executable, '-c', script], **kw)
    monkeypatch.setattr(subprocess, 'Popen', run)
    rows = remote_ps._fetch(dict(name='dell', target='target'), remote_ps.query_host)
    assert len(calls) == attempts
    if code == 0:
        assert rows == []
    else:
        assert rows[0]['status'] == 'unknown'


def test_remote_hosts_use_configured_name():
    payload = json.dumps({'runs': [dict(run_id=str(i), host=h, status='running')
                                  for i, h in enumerate(['host-a.example.invalid', 'dell'])]})
    rows = remote_ps._fetch(dict(name='dell'), lambda *a: payload)
    assert [row['host'] for row in rows] == ['dell', 'dell']


def test_failed_query_diagnostic_is_bounded(monkeypatch):
    monkeypatch.setenv('PS_TEST_TOKEN', 'known-private-value')
    exc = remote_ps.HostQueryError('host query failed', code=1,
        stderr='ModuleNotFoundError: known-private-value private-target ' + 'x' * 300 + '\nHIDDEN')
    rows = remote_ps._fetch(dict(name='node-a', target='private-target'),
        lambda *a: (_ for _ in ()).throw(exc))
    reason = rows[0]['reason']
    assert len(reason) == len('host query failed')
    assert rows[0]['error'] == 'HostQueryError (SSH exit 1)'
    assert 'known-private-value' not in reason and 'private-target' not in reason
    assert 'HIDDEN' not in reason and 'ModuleNotFoundError' not in json.dumps(rows)
    assert ps_reply(rows)['runs'][0]['reason'] == reason


def test_retry_exhaustion_is_bounded(monkeypatch):
    original = subprocess.Popen
    calls = []
    def ssh(argv, **kwargs):
        calls.append(argv)
        return original([sys.executable, '-c',
            'import sys; sys.stderr.write("ssh: connect timeout\\n"); sys.exit(255)'], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', ssh)
    rows = remote_ps._fetch(dict(name='dell', target='target'), remote_ps.query_host)
    assert len(calls) == 1
    assert rows[0]['reason'] == 'host unreachable'
    assert rows[0]['error'] == 'HostQueryError (SSH exit 255)'


def test_remote_command_timeout_is_not_retried(monkeypatch):
    calls = []
    def query(host, timeout):
        calls.append(host)
        raise TimeoutError('remote command timeout')
    monkeypatch.setattr(remote_ps, 'query_host', query)
    rows = remote_ps._fetch(dict(name='dell', target='target'), remote_ps.query_host)
    assert len(calls) == 1
    assert rows[0]['reason'] == 'host query failed'
    assert rows[0]['error'] == 'TimeoutError'


def test_transport_error_metadata_keeps_reason_private(monkeypatch):
    from plangraph.routing_support import host_keys
    monkeypatch.setattr(host_keys, 'redaction_env', lambda env: dict(env))
    exc = remote_ps.HostQueryError('PRIVATE-DIAGNOSTIC')
    exc.code, exc.stderr = 1, 'PRIVATE-DIAGNOSTIC' * 100
    rows = remote_ps._fetch(dict(name='ws-build', target='private-target'),
                           lambda *a: (_ for _ in ()).throw(exc))
    assert rows == [dict(host='ws-build', status='unknown', reason='host query failed',
                         error='HostQueryError (SSH exit 1)')]
    assert ps_reply(rows)['runs'] == rows
    assert 'PRIVATE-DIAGNOSTIC' not in json.dumps(rows)


def test_native_result_over_a_wsl_guest_sends_a_receipt_plan_not_a_script(monkeypatch):
    from types import SimpleNamespace
    from plangraph.spawner import overflow
    host = dict(ssh='alias', repo='/r', spawner_src='/s', env_file='/e', wsl_distro='Ubuntu', state_dir='/st')
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'placement_config', lambda registry, role=None: {'hosts': {'ash': host}})
    seen = []
    def run(argv, **kwargs):
        plan = json.loads(kwargs['input'])
        seen.append((argv, plan))
        receipt = plan['steps'][0]['receipt']
        body = json.dumps(dict(run_id='run-a', host='ash', run_kind='native-codex', role='implement',
                               status='completed'))
        return SimpleNamespace(returncode=0, stdout=(
            receipt['begin'] + '\n' + body + '\n\n' + receipt['end'] + '\n').encode())
    monkeypatch.setattr(remote_ps.subprocess, 'run', run)
    record = dict(host='ash', run_id='run-a', run_kind='native-codex', role='implement')
    result = remote_ps.native_result(record)
    assert result.get('remote_query_error') is None
    argv, plan = seen[0]
    assert argv[-1] == 'wsl.exe --distribution Ubuntu --exec pg-spawnd steps'
    assert plan['cwd'] == '/r' and plan['env_file'] == '/e'
    step = plan['steps'][0]
    assert step['argv'][:3] == ['python3', '-m', 'plangraph.spawner.cli']
    assert step['argv'][-2:] == ['result', 'run-a'] and step['env'] == {'PYTHONPATH': '/s'}


def test_wsl_query_deadline_holds_when_the_reader_never_drains_the_plan(monkeypatch):
    import time
    original = subprocess.Popen
    host = dict(target='private-target', src='/opt/plangraph/src', wsl_distro='Ubuntu')
    big = {'padding': 'x' * (4 * 1024 * 1024)}
    monkeypatch.setattr(remote_ps, 'host_invocation',
                        lambda host, query=False: ('wsl.exe', json.dumps(big).encode()))
    def run(argv, **kwargs):
        # A descendant inherits stdin and outlives the killed child, so the pipe stays full.
        child = ('import subprocess, sys, time\n'
                 'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], stdin=0)\n'
                 'time.sleep(30)')
        return original([sys.executable, '-c', child], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', run)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        remote_ps.query_host(host, 0.15)
    # The full pipe would hold the call for the descendant's 20 s; a loaded host stays far below.
    assert time.monotonic() - started < 10


def test_wsl_query_delivers_a_plan_larger_than_a_pipe_buffer(monkeypatch, tmp_path):
    original = subprocess.Popen
    host = dict(target='private-target', src='/opt/plangraph/src', wsl_distro='Ubuntu')
    body = json.dumps({'padding': 'x' * (512 * 1024)}).encode()
    monkeypatch.setattr(remote_ps, 'host_invocation', lambda host, query=False: ('wsl.exe', body))
    received = tmp_path / 'plan.json'
    def run(argv, **kwargs):
        child = ('import sys,pathlib;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read());'
                 'print(\'{"runs": []}\', end="")')
        return original([sys.executable, '-c', child, str(received)], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', run)
    assert remote_ps.query_host(host, 10) == '{"runs": []}'
    assert received.read_bytes() == body


def test_remote_rows_carry_batch_member_keys_and_refuse_malformed_ones(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, [dict(name='node-a', target='private-a')])
    members = ['plangraph:ALPHA-1', 'plangraph:BETA-1']

    def reply(item_keys):
        return lambda host, timeout: json.dumps({'runs': [dict(
            run_id='run-b', host='true-host', status='running', item_key=members[0],
            item_keys=item_keys)]})

    rows = remote_ps.list_runs([], transport=reply(members))
    assert rows[0]['item_keys'] == members
    assert ps_reply(rows)['runs'][0]['item_keys'] == members
    for bad in ('plangraph:ALPHA-1', [], [7], ['no-colon'], ['plangraph:A'] * 51):
        rows = remote_ps.list_runs([], transport=reply(bad))
        assert rows[0]['status'] == 'unknown' and 'item_keys' not in rows[0], bad


def test_fleet_session_rows_keep_run_kind_for_the_feeder_card_split(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch, [dict(name='node-a', target='private-a')])
    row = dict(run_id='run-a', host='node-a', status='running', role='implement',
               run_kind='fleet-session', item_key='plangraph:A plangraph:B')
    assert remote_ps.list_runs([], transport=lambda *args: json.dumps({'runs': [row]})) == [row]


def test_batch_card_keys_land_on_the_run_record():
    from plangraph.spawner.item_key import annotate_item
    record = {}
    annotate_item(record, {'item_key': 'plangraph:ALPHA-1',
                           'item_keys': ['plangraph:ALPHA-1', 'plangraph:BETA-1', 'plangraph:ALPHA-1']})
    assert record['item_keys'] == ['plangraph:ALPHA-1', 'plangraph:BETA-1']
    for card in ({'item_key': 'plangraph:ALPHA-1'},
                 {'item_key': 'plangraph:ALPHA-1', 'item_keys': ['bad key']},
                 {'item_key': 'plangraph:ALPHA-1', 'item_keys': 'plangraph:ALPHA-1'}):
        record = {}
        annotate_item(record, card)
        assert 'item_keys' not in record, card


@pytest.mark.parametrize('prompt_text', [
    pytest.param(r'fix the literal \0 string', id='literal-backslash-zero'),
    pytest.param('reject an embedded \0 byte', id='nul-byte'),
])
def test_remote_resume_ack_clears_previous_outcome(tmp_path, monkeypatch, capsys, prompt_text):
    from types import SimpleNamespace
    from plangraph.spawner import cli, overflow
    from plangraph.spawner.service import SpawnService

    host = dict(ssh='alias', repo='/r', spawner_src='/s',
                wsl_distro='Ubuntu', state_dir='/target-state')
    monkeypatch.setattr(overflow, 'registry_caps', lambda: {})
    monkeypatch.setattr(overflow, 'placement_config', lambda registry: {'hosts': {'ash': host}})
    service = SpawnService(state=tmp_path)
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: service)
    prior = dict(run_id='run-a', host='ash', run_kind='native-codex', role='implement',
                 cwd='/target-state/runs/run-a/worktree', session_id='existing-session',
                 overflow_remote=True, status='failed', final_head='a' * 40,
                 started=1, ended=2, exit_code=7, reason='old failure',
                 status_line='BLOCKED', result='old result', review={'head': 'a' * 40},
                 fast_checks={'head': 'a' * 40})
    record_path = service._path('run-a')
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(prior))
    prompt = tmp_path / 'fix.md'
    prompt.write_text(prompt_text, encoding='utf-8')
    calls = []

    def fake_ssh(argv, **kwargs):
        assert argv[-1] == 'wsl.exe --distribution Ubuntu --exec pg-spawnd steps'
        step = json.loads(kwargs['input'])['steps'][0]
        calls.append(step['argv'])
        if len(calls) == 1:
            assert step['argv'] == ['pg-spawnd', 'resume-local', 'run-a', '--state', '/target-state']
            assert json.loads(step['stdin_text']) == dict(
                host='ash', session_id=prior['session_id'], cwd=prior['cwd'],
                final_head=prior['final_head'], resume_number=0, prompt=prompt_text)
            body = dict(ok=True, run_id='run-a', resume_number=1, started=3)
        else:
            assert step['argv'][-2:] == ['result', 'run-a']
            if len(calls) == 2:
                raise subprocess.TimeoutExpired(argv, 10)
            body = dict(prior, **({'status': 'completed', 'final_head': 'b' * 40,
                                   'exit_code': 0, 'ended': 5, 'started': 3, 'resume_number': 1}
                                  if len(calls) == 4 else {}))
        receipt = step['receipt']
        return SimpleNamespace(returncode=0, stdout=(
            receipt['begin'] + '\n' + json.dumps(body) + '\n' + receipt['end'] + '\n').encode())

    monkeypatch.setattr(remote_ps.subprocess, 'run', fake_ssh)
    rc = cli.main(['resume', 'run-a', '--prompt-file', str(prompt)])
    if '\0' in prompt_text:
        assert calls == []
        assert rc == 1
        assert capsys.readouterr().err.strip() == 'resume prompt must contain between 1 byte and 1 MiB of text'
        assert service.result('run-a') == prior
        return

    assert rc == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['resume_number'] == 1
    assert resumed['resume_from']['head'] == 'a' * 40
    assert resumed['resume_from']['exit_code'] == 7
    for _ in range(2):
        assert cli.main(['result', 'run-a']) == 0
        running = json.loads(capsys.readouterr().out)
        assert running['status'] == service.result('run-a')['status'] == 'running'
        assert running['remote_query_error'] == 'host query failed'
        for field in ('final_head', 'ended', 'exit_code', 'reason', 'status_line', 'result',
                      'review', 'fast_checks'):
            assert field not in running
    assert cli.main(['result', 'run-a']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'completed' and result['exit_code'] == 0
    assert result['final_head'] == 'b' * 40 and result['resume_number'] == 1
