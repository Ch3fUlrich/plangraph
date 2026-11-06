import json
import subprocess
import time
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
    def transport(host, timeout):
        assert 0 < timeout <= 3
        time.sleep(.1)
        if host['name'] == 'node-1':
            raise subprocess.TimeoutExpired('SECRET', timeout)
        if host['name'] == 'node-2':
            return 'SECRET malformed'
        return json.dumps({'runs': [dict(run_id='run-abc', host='true-host',
                                        status='running', stderr='SECRET')]})
    start = time.monotonic()
    rows = remote_ps.list_runs([], transport=transport)
    assert time.monotonic() - start < .25
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


def test_service_uses_fanout(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    monkeypatch.setattr(remote_ps, 'list_runs', lambda local: local + [
        dict(host='node-a', status='unknown', reason='host query failed')])
    assert SpawnService(state=tmp_path).ps()['runs'][0]['host'] == 'node-a'
