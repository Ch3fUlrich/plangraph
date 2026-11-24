"""plangraph_agent_ps keeps listing the healthy hosts when one host's query faults."""
import json
import time

import pytest

from plangraph.spawner import remote_ps


def registry(root):
    return {'version': 1, 'self': 'coding.vm', 'hosts': [
        {'id': 'dell', 'transport': 'ssh', 'os': 'linux', 'aliases': ['dell-build'],
         'caps': {'mem_floor_mb': 1}, 'repos': {'plangraph': {'root': root}}},
        {'id': 'ws', 'transport': 'ssh', 'os': 'linux', 'wsl_distro': 'Ubuntu',
         'caps': {'mem_floor_mb': 1}, 'repos': {'plangraph': {'root': '/home/w/pg'}}}]}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    directory = tmp_path / 'plangraph'
    directory.mkdir()
    return directory


def write(config, hosts, root=None):
    (config / 'ps-hosts.json').write_text(json.dumps({'hosts': hosts}))
    if root:
        (config / 'hosts.json').write_text(json.dumps(registry(root)))


def test_ps_source_follows_registry_checkout_not_stale_config(config):
    write(config, [dict(name='dell-build', target='dell', src='/old/place/src')],
          root='/home/u/code/plangraph')
    (host,) = remote_ps.configured_hosts()
    assert host['src'] == '/home/u/code/plangraph/src' and host['name'] == 'dell-build'


def test_ps_source_registry_alias_and_wsl_hop(config):
    write(config, [dict(name='ws-build', target='ws', src='/stale/src')], root='/x')
    (host,) = remote_ps.configured_hosts()
    assert (host['src'], host['wsl_distro']) == ('/home/w/pg/src', 'Ubuntu')


def test_ps_without_registry_keeps_configured_source(config):
    write(config, [dict(name='dell-build', target='dell', src='/old/place/src')])
    assert remote_ps.configured_hosts()[0]['src'] == '/old/place/src'


def test_ps_registry_invalid_keeps_configured_source(config):
    write(config, [dict(name='dell-build', target='dell', src='/old/place/src')])
    (config / 'hosts.json').write_text('{"hosts": [{"id": 3}]}')
    assert remote_ps.configured_hosts()[0]['src'] == '/old/place/src'


def fake(config_hosts, fault):
    def transport(host, timeout):
        if host['name'] == 'bad':
            return fault(host, timeout)
        return json.dumps({'runs': [dict(run_id='r1', host='x', status='running')]})
    return transport


@pytest.mark.parametrize('fault,reason', [
    (lambda h, t: (_ for _ in ()).throw(remote_ps.HostQueryError('x', code=44)),
     'host src missing'),
    (lambda h, t: (_ for _ in ()).throw(TimeoutError('slow')), 'host query failed')])
def test_failing_host_is_listed_and_others_still_print(config, fault, reason):
    write(config, [dict(name='bad', target='bad', src='/s/src'),
                   dict(name='good', target='good', src='/s/src')])
    rows = remote_ps.list_runs([], transport=fake(None, fault))
    assert [(r['host'], r['status']) for r in rows] == [('bad', 'unknown'), ('good', 'running')]
    assert rows[0]['reason'] == reason


def test_hung_transport_cannot_hold_ps(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'TIMEOUT', 0.1)
    write(config, [dict(name='bad', target='bad', src='/s/src'),
                   dict(name='good', target='good', src='/s/src')])
    started = time.monotonic()
    rows = remote_ps.list_runs([], transport=fake(None, lambda h, t: time.sleep(5)))
    assert time.monotonic() - started < 2
    assert [(r['host'], r['status']) for r in rows] == [('bad', 'unknown'), ('good', 'running')]
    assert rows[0]['reason'] == 'host query failed'


def test_wsl_and_windows_hosts_get_a_longer_query_budget(config):
    plain = dict(name='a', target='a', src='/s/src')
    assert remote_ps.host_timeout(plain) == remote_ps.TIMEOUT
    assert remote_ps.host_timeout(dict(plain, wsl_distro='Ubuntu')) > remote_ps.TIMEOUT
    assert remote_ps.host_timeout(dict(name='w', target='w', win_src='C:/s')) > remote_ps.TIMEOUT
