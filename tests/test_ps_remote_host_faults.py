"""plangraph_agent_ps keeps listing the healthy hosts when one host's query faults."""
import json
import threading
import time

import pytest

from plangraph.spawner import remote_ps


def registry(root):
    return {'version': 1, 'self': 'controller', 'hosts': [
        {'id': 'dell', 'transport': 'ssh', 'os': 'linux', 'aliases': ['dell-build'],
         'caps': {'mem_floor_mb': 1}, 'repos': {'plangraph': {'root': root}}},
        {'id': 'ws', 'transport': 'ssh', 'os': 'linux', 'wsl_distro': 'Ubuntu',
         'caps': {'mem_floor_mb': 1}, 'repos': {'plangraph': {'root': '/srv/guest/pg'}}}]}


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
          root='/srv/checkout')
    (host,) = remote_ps.configured_hosts()
    assert host['src'] == '/srv/checkout/src' and host['name'] == 'dell-build'


def test_ps_source_registry_alias_and_wsl_hop(config):
    write(config, [dict(name='ws-build', target='ws', src='/stale/src')], root='/x')
    (host,) = remote_ps.configured_hosts()
    assert (host['src'], host['wsl_distro']) == ('/srv/guest/pg/src', 'Ubuntu')


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
                   dict(name='good', target='good', src='/s/src'),
                   dict(name='late', target='late', src='/s/src')])
    release = threading.Event()
    result = []
    worker = threading.Thread(target=lambda: result.append(remote_ps.list_runs(
        [], transport=fake(None, lambda h, t: release.wait(30)))), daemon=True)
    worker.start()
    try:
        # list_runs must return while the hung host is still blocked: the event is set only after.
        worker.join(10)
        returned = not worker.is_alive()
    finally:
        release.set()
        worker.join(40)
    assert returned, 'a hung host held the whole listing'
    rows = result[0]
    assert [(r['host'], r['status']) for r in rows] == [
        ('bad', 'unknown'), ('good', 'running'), ('late', 'running')]
    assert rows[0]['reason'] == 'host query failed'


def test_more_hosts_than_workers_each_get_their_own_budget(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'MAX_WORKERS', 2)
    monkeypatch.setattr(remote_ps, 'TIMEOUT', 0.7)
    write(config, [dict(name=f'h{i}', target=f'h{i}', src='/s/src') for i in range(8)])

    def slow(host, timeout):
        time.sleep(0.6)
        return json.dumps({'runs': [dict(run_id='r' + host['name'], host='x', status='running')]})
    rows = remote_ps.list_runs([], transport=slow)
    assert [r['status'] for r in rows] == ['running'] * 8


def test_coverage_and_lifecycle_queries_use_the_host_budget(config, monkeypatch, tmp_path):
    write(config, [dict(name='w', target='w', src='/s/src', wsl_distro='Ubuntu')])
    seen = []
    monkeypatch.setattr(remote_ps, 'lifecycle_command', lambda *a, **k: type('R', (), dict(
        stdout=b'{"day":"d","runs":[],"fleet":[],"rows":[],"next_cursor":0,"more":false}'))())

    def transport(host, timeout):
        seen.append(timeout)
        return '{"day":"d","runs":[],"fleet":[],"rows":[],"next_cursor":0,"more":false}'
    remote_ps.attempt_coverage_inputs(tmp_path, 'd', transport=transport)
    remote_ps.changed_runs(tmp_path, {'local': 0}, transport=transport)
    assert seen == [remote_ps.host_timeout(dict(wsl_distro='x'))] * 2


def test_wsl_and_windows_hosts_get_a_longer_query_budget(config):
    plain = dict(name='a', target='a', src='/s/src')
    assert remote_ps.host_timeout(plain) == remote_ps.TIMEOUT
    assert remote_ps.host_timeout(dict(plain, wsl_distro='Ubuntu')) > remote_ps.TIMEOUT
    assert remote_ps.host_timeout(dict(name='w', target='w', win_src='C:/s')) > remote_ps.TIMEOUT


CLI_SCRIPT = """
import sys, threading
from plangraph.spawner import cli, remote_ps
remote_ps.TIMEOUT = 0.1
release = threading.Event()
def transport(host, timeout):
    if host['name'] == 'bad':
        release.wait(600)
    return '{"runs": [{"run_id": "r1", "host": "x", "status": "running"}]}'
real = remote_ps.list_runs
remote_ps.list_runs = lambda rows: real(rows, transport=transport)
cli.main(['--state', sys.argv[1], 'ps'])
"""


def test_ps_cli_prints_and_exits_while_a_host_is_blocked(config, tmp_path):
    import os
    import subprocess
    import sys
    write(config, [dict(name='bad', target='bad', src='/s/src'),
                   dict(name='good', target='good', src='/s/src')])
    state = tmp_path / 'state'
    state.mkdir()
    env = dict(os.environ, XDG_CONFIG_HOME=str(config.parent),
               PYTHONPATH=os.pathsep.join(sys.path))
    # A worker still blocked in a transport must not keep the interpreter alive at exit.
    done = subprocess.run([sys.executable, '-c', CLI_SCRIPT, str(state)], env=env,
                          capture_output=True, text=True, timeout=30)
    rows = json.loads(done.stdout)['runs']
    assert [(r['host'], r['status']) for r in rows] == [('bad', 'unknown'), ('good', 'running')]


def shared_target_registry(config):
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'ssh_target': 'ws'}
    (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'controller', 'hosts': [
        dict(base, id='ws', os='windows', repos={'plangraph': {'root': 'C:/pg'}}),
        dict(base, id='ws-wsl', os='linux', wsl_distro='Ubuntu', aliases=['Ws-Guest'],
             repos={'plangraph': {'root': '/home/g/pg'}})]}))


def test_ps_source_alias_match_ignores_case(config):
    shared_target_registry(config)
    write(config, [dict(name='WS-GUEST', target='elsewhere', src='/stale/src')])
    (config / 'hosts.json').write_text((config / 'hosts.json').read_text())
    assert remote_ps.configured_hosts()[0]['src'] == '/home/g/pg/src'


def test_ps_shared_target_never_turns_windows_into_wsl(config):
    shared_target_registry(config)
    write(config, [dict(name='native', target='ws', win_src='C:/stale/src')])
    (config / 'hosts.json').write_text((config / 'hosts.json').read_text())
    assert remote_ps.configured_hosts() == [dict(name='native', target='ws', win_src='C:/pg/src')]


def test_doctor_probe_gets_the_registry_resolved_hosts_and_slow_budget(config, monkeypatch):
    import asyncio
    import subprocess
    from plangraph.spawner import doctor
    shared_target_registry(config)
    write(config, [dict(name='ws-wsl', target='ws', src='/stale/src')])
    (config / 'hosts.json').write_text((config / 'hosts.json').read_text())
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, '[]')

    async def mcp(*args):
        return 'pass', 'ok'
    monkeypatch.setattr(doctor.subprocess, 'run', run)
    monkeypatch.setattr(doctor, 'mcp', mcp)
    asyncio.run(doctor.diagnose())
    (host,) = json.loads(seen['env']['PLANGRAPH_PS_HOSTS_JSON'])['hosts']
    assert (host['src'], host['wsl_distro']) == ('/home/g/pg/src', 'Ubuntu')
    assert seen['timeout'] == remote_ps.host_timeout(host) + doctor.TIMEOUT
