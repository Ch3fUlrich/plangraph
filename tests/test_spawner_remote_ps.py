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


def test_transport_is_bounded_and_noninteractive(monkeypatch):
    def run(argv, **kwargs):
        assert argv[:3] == ['ssh', '-o', 'BatchMode=yes']
        assert kwargs['timeout'] == 2
        assert kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs['encoding'] == 'utf-8'
        assert kwargs['errors'] == 'replace'
        assert kwargs.get('shell', False) is False
        return subprocess.CompletedProcess(argv, 0, '{"runs": []}')
    monkeypatch.setattr(subprocess, 'run', run)
    assert remote_ps.query_host(dict(target='private-target'), 2) == '{"runs": []}'


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
        return rows + [remote]
    monkeypatch.setattr(remote_ps, 'list_runs', fanout)
    if surface == 'mcp':
        result = asyncio.run(MCPSpawnerServer(state=tmp_path).call_tool('plangraph_agent_ps', {}))
    else:
        monkeypatch.setattr(cli, 'SpawnService', lambda **kwargs: SpawnService(state=tmp_path))
        cli.main(['ps'])
        result = json.loads(capsys.readouterr().out)
    assert calls == [[local]]
    assert [row['run_id'] for row in result['runs']] == ['local', 'remote']


@pytest.mark.parametrize('contents', ['not json', '{}', 'null', '[]',
    '{"hosts": null}', '{"hosts": [null]}',
    '{"hosts": [{"name": 1, "target": "SECRET"}]}',
    '{"hosts": [{"name": "node-a", "target": []}]}',
    '{"hosts": [{"name": "node-a", "target": "-SECRET"}]}', '\udcff'])
def test_invalid_config_preserves_local_listing(tmp_path, monkeypatch, caplog, contents):
    config(tmp_path, monkeypatch, [])
    path = tmp_path / 'plangraph' / 'ps-hosts.json'
    path.write_bytes(contents.encode('utf-8', errors='surrogateescape'))
    local = [dict(run_id='local', status='running', started=1)]
    assert remote_ps.list_runs(local) == local
    assert caplog.text == ''


def test_unreadable_config_preserves_local_listing(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    def denied(*args, **kwargs):
        raise PermissionError('SECRET')
    monkeypatch.setattr(Path, 'read_text', denied)
    local = [dict(run_id='local', status='running')]
    assert remote_ps.list_runs(local) == local
    assert caplog.text == ''


@pytest.mark.parametrize('field', ['host', 'run_id'])
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
