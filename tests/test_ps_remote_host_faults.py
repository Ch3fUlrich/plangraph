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
    write(config, [dict(name='ws-build', target='ws', src='/stale/src', wsl_distro='Ubuntu')], root='/x')
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
             repos={'plangraph': {'root': '/srv/guest/pg'}})]}))


def test_ps_source_alias_match_ignores_case(config):
    shared_target_registry(config)
    write(config, [dict(name='WS-GUEST', target='elsewhere', src='/stale/src')])
    (config / 'hosts.json').write_text((config / 'hosts.json').read_text())
    assert remote_ps.configured_hosts()[0]['src'] == '/srv/guest/pg/src'


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
    assert (host['src'], host['wsl_distro']) == ('/srv/guest/pg/src', 'Ubuntu')
    assert seen['timeout'] == remote_ps.host_timeout(host) + doctor.TIMEOUT


def alive_transports():
    return [t for t in threading.enumerate() if t.name.startswith('ps-transport-')]


def test_repeated_calls_start_no_second_transport_for_a_blocked_host(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'TIMEOUT', 0.05)
    write(config, [dict(name='stuck', target='stuck', src='/s/src')])
    release = threading.Event()
    try:
        for _ in range(3):
            rows = remote_ps.list_runs([], transport=lambda h, t: release.wait(30))
            assert [r['status'] for r in rows] == ['unknown']
            assert len(alive_transports()) == 1
    finally:
        release.set()
        for thread in alive_transports():
            thread.join(5)
    rows = remote_ps.list_runs([], transport=lambda h, t: json.dumps(
        {'runs': [dict(run_id='r', host='x', status='running')]}))
    assert [r['status'] for r in rows] == ['running']


def test_healthy_host_behind_a_blocked_one_is_still_queried(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'MAX_WORKERS', 1)
    monkeypatch.setattr(remote_ps, 'TIMEOUT', 0.05)
    write(config, [dict(name='bad', target='bad', src='/s/src'),
                   dict(name='good', target='good', src='/s/src')])
    release = threading.Event()
    try:
        rows = remote_ps.list_runs([], transport=fake(None, lambda h, t: release.wait(30)))
    finally:
        release.set()
        for thread in alive_transports():
            thread.join(5)
    assert [(r['host'], r['status']) for r in rows] == [('bad', 'unknown'), ('good', 'running')]


def test_overlapping_calls_never_run_more_transports_than_the_cap(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'MAX_WORKERS', 1)
    write(config, [dict(name=f'h{i}', target=f'h{i}', src='/s/src') for i in range(2)])
    lock, state = threading.Lock(), dict(now=0, most=0)

    def transport(host, timeout):
        with lock:
            state['now'] += 1
            state['most'] = max(state['most'], state['now'])
        time.sleep(0.2)
        with lock:
            state['now'] -= 1
        return json.dumps({'runs': [dict(run_id='r', host='x', status='running')]})
    results = []
    callers = [threading.Thread(target=lambda: results.append(
        remote_ps.list_runs([], transport=transport))) for _ in range(3)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(30)
    assert state['most'] == 1
    assert all([r['status'] for r in rows] == ['running'] * 2 for rows in results) and len(results) == 3



@pytest.mark.parametrize('reverse', [False, True])
def test_shared_target_picks_the_entry_of_the_configured_wsl_distribution(config, reverse):
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'ssh_target': 'ws', 'os': 'linux'}
    guests = [dict(base, id='guest-a', wsl_distro='DistroA', repos={'plangraph': {'root': '/srv/a'}}),
              dict(base, id='guest-b', wsl_distro='DistroB', repos={'plangraph': {'root': '/srv/b'}}),
              dict(base, id='native', os='windows', repos={'plangraph': {'root': 'C:/pg'}})]
    (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'controller', 'hosts': (
        guests[::-1] if reverse else guests)}))
    write(config, [dict(name='q-a', target='ws', src='/stale/src', wsl_distro='DistroA'),
                   dict(name='q-b', target='ws', src='/stale/src', wsl_distro='DistroB'),
                   dict(name='q-c', target='ws', src='/stale/src', wsl_distro='DistroC'),
                   dict(name='q-direct', target='ws', src='/stale/src'),
                   dict(name='q-win', target='ws', win_src='C:/stale/src')])
    # Write the registry after write(): write() only touches it when a root is given.
    (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'controller', 'hosts': (
        guests[::-1] if reverse else guests)}))
    got = {h['name']: (h.get('src') or h.get('win_src'), h.get('wsl_distro'))
           for h in remote_ps.configured_hosts()}
    assert got == {'q-a': ('/srv/a/src', 'DistroA'), 'q-b': ('/srv/b/src', 'DistroB'),
                   'q-c': ('/stale/src', 'DistroC'), 'q-direct': ('/stale/src', None),
                   'q-win': ('C:/pg/src', None)}


STATE = '/srv/spawn state/dell'


def test_generated_ps_entry_carries_the_registry_state_dir():
    from plangraph.fleet_sync import genconfig
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'os': 'linux',
            'repos': {'plangraph': {'root': '/srv/pg'}}}
    registry = {'self': 'controller', 'hosts': [
        dict(base, id='kept', state_dir=STATE), dict(base, id='blank', state_dir=''),
        dict(base, id='absent'),
        dict(base, id='wsl', state_dir='/srv/wsl-state', wsl_distro='Ubuntu')]}
    rows = {r['name']: r for r in genconfig.generate(registry)['ps-hosts.json']['hosts']}
    assert rows['kept']['state_dir'] == STATE and rows['wsl']['state_dir'] == '/srv/wsl-state'
    assert 'state_dir' not in rows['blank'] and 'state_dir' not in rows['absent']


def test_registry_state_dir_replaces_a_stale_one_in_the_configured_host(config):
    shared_target_registry(config)
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'os': 'linux'}
    (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'controller', 'hosts': [
        dict(base, id='dell', state_dir=STATE, repos={'plangraph': {'root': '/srv/pg'}})]}))
    write(config, [dict(name='dell', target='dell', src='/stale/src', state_dir='/stale/state')])
    (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'controller', 'hosts': [
        dict(base, id='dell', state_dir=STATE, repos={'plangraph': {'root': '/srv/pg'}})]}))
    (host,) = remote_ps.configured_hosts()
    assert (host['src'], host['state_dir']) == ('/srv/pg/src', STATE)


@pytest.mark.parametrize('bad', ['relative', 3, '/a\nb', 'C:/s\x00', '', []])
def test_invalid_state_dir_is_an_invalid_host(config, bad):
    write(config, [dict(name='dell', target='dell', src='/s/src', state_dir=bad)])
    expected = [dict(name='dell', target='dell', src='/s/src', state_dir='')] if bad == '' else [
        dict(name='invalid-config', invalid=True)]
    assert remote_ps.configured_hosts() == expected


def test_state_dir_reaches_every_remote_query_form():
    posix = dict(name='p', target='p', src='/s/src', state_dir=STATE)
    command, plan = remote_ps.host_invocation(posix, query=True)
    assert plan is None and "PLANGRAPH_SPAWN_STATE='/srv/spawn state/dell'" in command
    wsl = dict(posix, wsl_distro='Ubuntu')
    command, plan = remote_ps.host_invocation(wsl, query=True)
    assert json.loads(plan)['steps'][-1]['env'] == {
        'PYTHONPATH': '/s/src', 'PLANGRAPH_SPAWN_STATE': STATE}
    windows = dict(name='w', target='w', win_src='C:/s/src', state_dir='C:/spawn/state')
    command, _ = remote_ps.host_invocation(windows, query=True)
    assert 'set "PLANGRAPH_SPAWN_STATE=C:\\spawn\\state"' in command
    # No state_dir keeps the command as it was.
    assert 'PLANGRAPH_SPAWN_STATE' not in remote_ps.host_invocation(
        dict(posix, state_dir=None), query=True)[0]


def test_ps_query_finds_a_run_launched_into_a_nondefault_state_dir(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from plangraph.spawner.service import SpawnService
    nondefault = tmp_path / 'spawn state'
    SpawnService(state=nondefault)._save(dict(run_id='run-loop', status='running', role='implement',
                                              host='h', started=time.time(), pid=os.getpid()))
    src = str(Path(remote_ps.__file__).resolve().parents[2])
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / 'default-home'),
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    env.pop('PLANGRAPH_SPAWN_STATE', None)

    def ask(state_dir):
        host = dict(name='h', target='h', src=src, **({'state_dir': str(nondefault)} if state_dir else {}))
        command, _ = remote_ps.host_invocation(host, query=True)
        out = subprocess.run(['sh', '-c', command], env=env, capture_output=True, text=True, timeout=60)
        return [row['run_id'] for row in json.loads(out.stdout)['runs']]
    assert ask(False) == []
    assert ask(True) == ['run-loop']


def test_overlapping_polls_start_one_transport_per_host(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'MAX_WORKERS', 2)
    write(config, [dict(name='only', target='only', src='/s/src')])
    lock, state = threading.Lock(), dict(now=0, most=0)

    def transport(host, timeout):
        with lock:
            state['now'] += 1
            state['most'] = max(state['most'], state['now'])
        time.sleep(0.2)
        with lock:
            state['now'] -= 1
        return json.dumps({'runs': [dict(run_id='r', host='x', status='running')]})
    results = []
    callers = [threading.Thread(target=lambda: results.append(
        remote_ps.list_runs([], transport=transport))) for _ in range(3)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(30)
    assert state['most'] == 1
    assert [[r['status'] for r in rows] for rows in results] == [['running']] * 3


def test_many_blocked_hosts_never_stop_a_healthy_host_being_queried(config, monkeypatch):
    monkeypatch.setattr(remote_ps, 'MAX_WORKERS', 1)
    monkeypatch.setattr(remote_ps, 'TIMEOUT', 0.02)
    monkeypatch.setattr(remote_ps, 'GRACE', 0.02)
    hosts = [dict(name=f'bad{i}', target=f'bad{i}', src='/s/src') for i in range(8)]
    write(config, [*hosts, dict(name='good', target='good', src='/s/src')])
    release = threading.Event()
    try:
        rows = remote_ps.list_runs([], transport=lambda h, t: release.wait(30) if h['name'].startswith('bad')
                                   else json.dumps({'runs': [dict(run_id='r', host='x', status='running')]}))
    finally:
        release.set()
        for thread in alive_transports():
            thread.join(5)
    assert rows[-1]['host'] == 'good' and rows[-1]['status'] == 'running'


def capture_ssh(monkeypatch):
    sent = []

    def popen(argv, **kwargs):
        sent.append(argv)
        raise RuntimeError('stop before connecting')
    monkeypatch.setattr(remote_ps.subprocess, 'Popen', popen)
    return sent


@pytest.mark.parametrize('form', [dict(lifecycle_cursor=3), dict(coverage_day='2026-11-24')])
def test_every_lifecycle_form_sends_the_state_dir_to_ssh(monkeypatch, form):
    sent = capture_ssh(monkeypatch)
    for host in (dict(name='p', target='p', src='/s/src', state_dir='/srv/state'),
                 dict(name='w', target='w', win_src='C:/s/src', state_dir='C:/spawn/state'),
                 dict(name='g', target='g', src='/s/src', state_dir='/srv/state', wsl_distro='Ubuntu')):
        with pytest.raises(RuntimeError):
            remote_ps.query_host(dict(host, **form), 5)
    posix, windows, guest = (argv[-1] for argv in sent)
    assert posix.startswith("PLANGRAPH_SPAWN_STATE=/srv/state pg-spawnd ")
    assert windows.startswith('set "PLANGRAPH_SPAWN_STATE=C:\\spawn\\state"&& pg-spawnd ')
    assert guest.startswith('wsl.exe ')


def test_registry_overlay_state_dir_is_validated_like_the_configured_one(config):
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'os': 'linux',
            'repos': {'plangraph': {'root': '/srv/pg'}}}
    write(config, [dict(name='dell', target='dell', src='/s/src')])
    (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'controller', 'hosts': [
        dict(base, id='dell', state_dir='relative/state')]}))
    assert remote_ps.configured_hosts() == [dict(name='invalid-config', invalid=True)]
    write(config, [dict(name='dell', target='dell', src='/s/src', state_dir='relative/state')])
    (config / 'hosts.json').unlink()
    assert remote_ps.configured_hosts() == [dict(name='invalid-config', invalid=True)]


def test_a_malformed_registry_state_dir_is_carried_for_rejection():
    from plangraph.fleet_sync import genconfig
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'os': 'linux',
            'repos': {'plangraph': {'root': '/srv/pg'}}}
    rows = {r['name']: r for r in genconfig.generate({'self': 'c', 'hosts': [
        dict(base, id='num', state_dir=5), dict(base, id='blank', state_dir='')]})['ps-hosts.json']['hosts']}
    assert rows['num']['state_dir'] == 5 and 'state_dir' not in rows['blank']


def test_source_precedence_is_resolved_before_validation(config):
    base = {'caps': {'mem_floor_mb': 1}, 'transport': 'ssh', 'os': 'linux',
            'repos': {'plangraph': {'root': '/srv/pg'}}}

    def registry(state):
        (config / 'hosts.json').write_text(json.dumps({'version': 1, 'self': 'c', 'hosts': [
            dict(base, id='dell', state_dir=state)]}))
    # A stale malformed configured value does not block a valid registry value.
    write(config, [dict(name='dell', target='dell', src='/s/src', state_dir='relative')])
    registry('/srv/state')
    assert remote_ps.configured_hosts()[0]['state_dir'] == '/srv/state'
    # A malformed registry value is rejected, not dropped to the default store.
    registry(5)
    assert remote_ps.configured_hosts() == [dict(name='invalid-config', invalid=True)]


@pytest.mark.parametrize('state', ['/srv/spawn+state', "/srv/it's here", '/srv/a;b $x'])
def test_posix_state_dir_the_registry_accepts_is_encoded_safely(state):
    import shlex
    command = remote_ps.host_command(dict(name='p', target='p', src='/s/src', state_dir=state), query=True)
    assert remote_ps.state_dir_ok(dict(src='/s/src', state_dir=state))
    assert f'{remote_ps.STATE_ENV}={state}' in shlex.split(command.split('; ', 1)[1])


@pytest.mark.parametrize('state', ['\\\\fileserver\\spawn\\state', '//fileserver/spawn/state', 'D:\\state'])
def test_windows_state_dir_the_registry_accepts_is_encoded_for_cmd(state):
    host = dict(name='w', target='w', win_src='C:/s/src', state_dir=state)
    assert remote_ps.state_dir_ok(host)
    command = remote_ps.host_command(host, query=True)
    assert 'set "PLANGRAPH_SPAWN_STATE=' + state.replace('/', '\\') + '"' in command


@pytest.mark.parametrize('state', ['C:/a"b', 'C:/a%PATH%', '\\\\server\\x"&calc'])
def test_windows_state_dir_with_cmd_metacharacters_is_refused(state):
    assert not remote_ps.state_dir_ok(dict(win_src='C:/s/src', state_dir=state))


def test_two_overlapping_polls_share_one_transport_and_its_result(config):
    write(config, [dict(name='slow', target='slow', src='/s/src')])
    calls = []

    def transport(host, timeout):
        calls.append(1)
        time.sleep(0.5)
        return json.dumps({'runs': [dict(run_id='r', host='x', status='running')]})
    results = []
    first = threading.Thread(target=lambda: results.append(remote_ps.list_runs([], transport=transport)))
    first.start()
    time.sleep(0.15)
    second = threading.Thread(target=lambda: results.append(remote_ps.list_runs([], transport=transport)))
    second.start()
    first.join(30)
    second.join(30)
    assert [[r['status'] for r in rows] for rows in results] == [['running']] * 2 and len(calls) == 1


def test_same_label_with_different_targets_are_queried_separately(config):
    write(config, [dict(name='label', target='first', src='/s/src'),
                   dict(name='label', target='second', src='/s/src')])
    asked = []

    def transport(host, timeout):
        asked.append(host['target'])
        time.sleep(0.1)
        return json.dumps({'runs': [dict(run_id='run-' + host['target'], host='x', status='running')]})
    rows = remote_ps.list_runs([], transport=transport)
    assert sorted(asked) == ['first', 'second']
    assert sorted(r['run_id'] for r in rows) == ['run-first', 'run-second']


def test_a_poll_after_a_state_dir_change_does_not_get_the_old_result(config):
    asked = []

    def transport(host, timeout):
        asked.append(host.get('state_dir'))
        time.sleep(0.4)
        return json.dumps({'runs': [dict(run_id='in-' + host['state_dir'][1:], host='x', status='running')]})

    def poll(state):
        write(config, [dict(name='h', target='h', src='/s/src', state_dir=state)])
        results.append(remote_ps.list_runs([], transport=transport))
    results = []
    first = threading.Thread(target=poll, args=('/old',))
    first.start()
    time.sleep(0.15)
    second = threading.Thread(target=poll, args=('/new',))
    second.start()
    first.join(30)
    second.join(30)
    assert sorted(asked) == ['/new', '/old']
    assert sorted(r['run_id'] for rows in results for r in rows) == ['in-new', 'in-old']
