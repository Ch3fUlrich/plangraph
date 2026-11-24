"""Hermetic CLI coverage: SSH and git are binaries on a temporary PATH."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('mode', [
    'green', 'low_disk', 'missing', 'behind', 'unreachable', 'unreadable', 'unlisted', 'alias',
    'src_int', 'src_bool', 'spawner_int', 'spawner_bool', 'session_int', 'ps_shape',
    'no_ps', 'empty_ps', 'ps_bad_json', 'ps_unreadable',
])
def test_host_ready_stub_binaries(tmp_path, mode):
    binary = tmp_path / 'bin'
    binary.mkdir()
    ssh = binary / 'ssh'
    ssh.write_text('''#!/bin/sh
if [ "$MODE" = unreachable ]; then exit 255; fi
for arg do script="$arg"; done
case "$script" in
  *"echo src_missing"*)
    case "$script" in
      *deleted*) echo src_missing;;
      *) echo ok;;
    esac;;
  *meminfo*) echo 'MemAvailable: 8000000 kB';;
  *"df -Pk"*)
    if [ "$MODE" = low_disk ]; then echo 'fake 9000000 0 4194304 0% /';
    else echo 'fake 9000000 0 5242880 0% /'; fi;;
  git*) exec sh -c "$script";;
  *"doctor --json"*) echo '{"ok":true,"checks":[{"name":"keeper_whoami","status":"pass"}]}';;
  *"cli ps"*) echo '{"runs":[]}';;
esac
''')
    git = binary / 'git'
    git.write_text('''#!/bin/sh
case "$*" in
  *rev-list*)
    [ "$MODE" = unreadable ] && exit 1
    if [ "$MODE" = behind ]; then echo 3; else echo 0; fi;;
  *rev-parse*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;;
esac
''')
    ssh.chmod(0o755)
    git.chmod(0o755)
    cfg = tmp_path / 'hosts.json'
    host = dict(ssh='host-a', repo='/repo', spawner_src='/source',
                run_cap=4, floor_mb=3000, session_unit='worker')
    hosts = {'host-a': host, 'host-b': dict(host, ssh='host-b')}
    if mode.startswith('spawner_'):
        host['spawner_src'] = 123 if mode == 'spawner_int' else True
    if mode == 'session_int':
        host['session_unit'] = 123
    cfg.write_text(json.dumps({'hosts': hosts}))
    ps = tmp_path / 'plangraph'
    ps.mkdir()
    (ps / 'hosts.json').write_text(json.dumps({'self': 'fixture', 'hosts': []}))
    ps_hosts = [dict(name=name, target=name, src='/source') for name in hosts]
    if mode == 'missing':
        ps_hosts[0]['src'] = '/deleted'
    elif mode == 'unlisted':
        ps_hosts.pop()
    elif mode == 'alias':
        ps_hosts[0]['target'] = 'host-a-alias'
    elif mode.startswith('src_'):
        ps_hosts[0]['src'] = 123 if mode == 'src_int' else True
    elif mode == 'empty_ps':
        ps_hosts = []
    if mode == 'ps_unreadable':
        (ps / 'ps-hosts.json').mkdir()
    elif mode == 'ps_bad_json':
        (ps / 'ps-hosts.json').write_text('{')
    elif mode != 'no_ps':
        (ps / 'ps-hosts.json').write_text(json.dumps(
            [] if mode == 'ps_shape' else {'hosts': ps_hosts}))
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path),
               PATH=str(binary) + os.pathsep + os.environ['PATH'], MODE=mode,
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    fixture_cli = (
        'import json, sys; from pathlib import Path; '
        'from plangraph.spawner import host_ready, overflow; '
        'overflow.placement_config = lambda registry: json.loads(Path(sys.argv[1]).read_text()); '
        'raise SystemExit(host_ready.main(["--json"]))'
    )
    result = subprocess.run([sys.executable, '-c', fixture_cli, str(cfg)],
                            env=env, capture_output=True, text=True, timeout=15)
    rows = json.loads(result.stdout)
    assert result.returncode == (mode != 'green')
    assert 'Traceback' not in result.stderr
    for row in rows:
        if not row['ok']:
            assert f"HOST-NOT-READY {row['host']} " in result.stderr
    if mode == 'green':
        assert all(r['ok'] and r['behind_main'] == 0 for r in rows)
        assert 'HOST-NOT-READY' not in result.stderr
    elif mode == 'low_disk':
        assert all(r['disk_free_gb'] == 4 and r['failures'] == ['disk: disk_low: free 4 GB'] for r in rows)
    elif mode == 'missing':
        assert 'HOST-NOT-READY host-a src_missing failed' in result.stderr
    elif mode == 'behind':
        assert 'HOST-NOT-READY host-a behind_main 3 behind' in result.stderr
        assert all(r['behind_main'] == 3 for r in rows)
    elif mode == 'unreadable':
        assert all(r['behind_main'] is None for r in rows)
        for row in rows:
            assert f"HOST-NOT-READY {row['host']} behind_main command_failed" in result.stderr
    elif mode == 'unlisted':
        assert rows[0]['ok'] and not rows[1]['ok'] and rows[1]['src_ok'] is False
        assert 'HOST-NOT-READY host-b src_config missing_ps_host' in result.stderr
    elif mode == 'alias':
        assert ("HOST-NOT-READY host-a src_config ps target='host-a-alias' and "
                "overflow ssh='host-a' must be the same string") in result.stderr
    elif mode in ('ps_shape', 'ps_bad_json', 'ps_unreadable'):
        assert all(not r['ok'] and r['src_ok'] is False for r in rows)
        assert result.stderr.splitlines() == [
            f"HOST-NOT-READY {row['host']} src_config unreadable" for row in rows]
        assert all(r['failures'] == ['src_config: unreadable'] for r in rows)
    elif mode in ('no_ps', 'empty_ps'):
        assert all(not r['ok'] and r['src_ok'] is False for r in rows)
        assert result.stderr.splitlines() == [
            f"HOST-NOT-READY {row['host']} src_config missing_ps_host" for row in rows]
        assert all(r['failures'] == ['src_config: missing_ps_host'] for r in rows)
    elif mode.startswith(('src_', 'spawner_')):
        assert 'HOST-NOT-READY host-a src_config ' in result.stderr
        assert rows[0]['src_ok'] is False and rows[1]['ok']
    elif mode == 'session_int':
        assert 'HOST-NOT-READY host-a config invalid_response_or_config' in result.stderr
        assert rows[1]['ok']
    else:
        assert 'HOST-NOT-READY host-a unreachable other_checks_unknown' in result.stderr
        assert all(r['behind_main'] is None and r['doctor_ok'] is None
                   and r['src_ok'] is None and r['spawner_session'] is None for r in rows)


ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / 'deploy' / 'systemd'


def unit_options(name, section):
    """The options of one unit file section, in order (a key may repeat)."""
    rows, current = [], None
    for line in (UNITS / name).read_text().splitlines():
        if line.startswith('['):
            current = line.strip('[]')
        elif current == section and '=' in line and not line.startswith('#'):
            rows.append(tuple(line.split('=', 1)))
    return rows


def seconds(text):
    units = {'s': 1, 'sec': 1, 'min': 60}
    number = ''.join(c for c in text if c.isdigit())
    return int(number) * units[text[len(number):]]


def unit_command():
    """The unit's ExecStart with the interpreter of this test and the unit's PYTHONPATH."""
    options = unit_options('pg-host-ready.service', 'Service')
    starts = [value for key, value in options if key == 'ExecStart']
    assert len(starts) == 1
    return starts[0].split()


def test_host_ready_unit_runs_the_existing_entry_point_often_enough():
    from plangraph.spawner import host_ready
    command = unit_command()
    assert command[1:3] == ['-m', host_ready.__name__]
    expected = int(command[command.index('--not-ready-exit') + 1])
    service_options = dict(unit_options('pg-host-ready.service', 'Service'))
    # Only the distinct not-ready status is a success; 1 (crash, unpublished record) and 2 fail.
    assert expected not in (0, 1, 2) and int(service_options['SuccessExitStatus']) == expected
    assert host_ready.__name__ == 'plangraph.spawner.host_ready'
    service, timer = (dict(unit_options(f'pg-host-ready.{kind}', section))
                      for kind, section in (('service', 'Service'), ('timer', 'Timer')))
    assert dict(unit_options('pg-host-ready.service', 'Service'))['Type'] == 'oneshot'
    assert timer['Unit'] == 'pg-host-ready.service'
    interval = seconds(timer['OnUnitActiveSec'])
    # The longest wait between two records is one interval plus one parallel probe.
    assert interval + host_ready.HOST_TIMEOUT < host_ready.MAX_AGE
    assert seconds(service['TimeoutStartSec']) >= host_ready.HOST_TIMEOUT
    assert dict(unit_options('pg-host-ready.timer', 'Install'))['WantedBy'] == 'timers.target'


def test_host_ready_unit_publishes_registry_candidates(tmp_path, monkeypatch):
    """Run the unit's module on a real registry and its projections; only candidates get records."""
    from plangraph.fleet_sync import genconfig
    from plangraph.spawner import overflow
    config = tmp_path / 'plangraph'
    config.mkdir()
    checkouts = {}
    for name in ('alpha', 'beta', 'gamma'):
        # A checkout whose src is the real source: the probe imports plangraph from it.
        (tmp_path / name).mkdir()
        (tmp_path / name / 'src').symlink_to(ROOT / 'src')
        checkouts[name] = str(tmp_path / name)

    def host(name, **extra):
        return dict(id=name, transport='ssh', os='linux', roles=['implement'],
                    state_dir=str(tmp_path / 'state' / name), caps=dict(mem_floor_mb=3000),
                    repos={'plangraph': {'root': checkouts[name]}}, **extra)
    excluded = dict(host('gamma'), repos={})  # no plangraph checkout, so no spawner source
    registry = {'self': 'coord', 'hosts': [
        dict(id='coord', transport='local', os='linux', caps=dict(mem_floor_mb=3000), repos={}),
        host('alpha'), host('beta'), excluded]}
    (config / 'hosts.json').write_text(json.dumps(registry))
    genconfig.sync(registry, write=True, directory=config)
    binary = tmp_path / 'bin'
    binary.mkdir()
    ssh = binary / 'ssh'
    ssh.write_text('''#!/bin/sh
for arg do script="$arg"; done
case "$script" in
  *"echo src_missing"*|*--test-tools*) exec env -u PYTHONPATH sh -c "umask 022; $script";;
  *"command -v"*|*"codex login"*) echo ok;;
  *pg-host-readiness*) echo '{"errors":[],"sandbox_ok":true}';;
  *meminfo*) echo 'MemAvailable: 8000000 kB';;
  *"df -Pk"*) echo 'fake 9000000 0 5242880 0% /';;
  *rev-list*) echo 0;;
  *rev-parse*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;;
  *"doctor --json"*) echo '{"ok":true,"checks":[{"name":"keeper_whoami","status":"pass"}]}';;
  *remote_ps*) echo '{"runs":[]}';;
esac
''')
    ssh.chmod(0o755)
    # The probes run the module for real on the "remote" side with the interpreter of this test.
    (binary / 'python3').symlink_to(sys.executable)
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path), PYTHONSAFEPATH='1',
               PATH=str(binary) + os.pathsep + os.environ['PATH'], PYTHONPATH=str(ROOT / 'src'))
    env.pop('PLANGRAPH_HOST_NAME', None)
    command = [sys.executable, *unit_command()[1:]]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    check = subprocess.run([sys.executable, '-m', 'plangraph.spawner.host_ready', '--check-records'],
                           env=env, capture_output=True, text=True, timeout=60)
    assert check.returncode == 0 and check.stdout.splitlines() == ['alpha ready', 'beta ready'], check
    monkeypatch.setenv('HOME', str(tmp_path))
    records = {name: overflow.read_readiness(name) for name in ('alpha', 'beta')}
    assert all(record['fresh'] and record['ok'] for record in records.values()), records
    with pytest.raises(FileNotFoundError):
        overflow.read_readiness('gamma')
    assert sorted(path.name for path in overflow.readiness_dir().iterdir()) == [
        'alpha.json', 'beta.json']


def run_unit(tmp_path, registry, ssh_body=''):
    """Run the unit's command against a registry written under tmp_path; return the result."""
    config = tmp_path / 'plangraph'
    config.mkdir(exist_ok=True)
    (config / 'hosts.json').write_text(json.dumps(registry))
    binary = tmp_path / 'bin'
    binary.mkdir(exist_ok=True)
    (binary / 'ssh').write_text('#!/bin/sh\nexit 255\n')
    (binary / 'ssh').chmod(0o755)
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path),
               PATH=str(binary) + os.pathsep + os.environ['PATH'], PYTHONPATH=str(ROOT / 'src'))
    env.pop('PLANGRAPH_HOST_NAME', None)
    return subprocess.run([sys.executable, *unit_command()[1:]], env=env,
                          capture_output=True, text=True, timeout=60)


def test_unit_exit_status_separates_not_ready_from_crashes(tmp_path):
    from plangraph.fleet_sync import genconfig
    host = dict(id='alpha', transport='ssh', os='linux', roles=['implement'],
                state_dir=str(tmp_path / 'state'), caps=dict(mem_floor_mb=3000),
                repos={'plangraph': {'root': str(tmp_path / 'alpha')}})
    registry = {'self': 'coord', 'hosts': [
        dict(id='coord', transport='local', os='linux', caps=dict(mem_floor_mb=3000), repos={}), host]}
    expected = int(unit_command()[unit_command().index('--not-ready-exit') + 1])
    (tmp_path / 'plangraph').mkdir()
    genconfig.sync(registry, write=True, directory=tmp_path / 'plangraph')
    # The host cannot be reached: its record is published as not ready, the unit's own success.
    unreachable = run_unit(tmp_path, registry)
    assert unreachable.returncode == expected, unreachable.stderr
    assert 'HOST-NOT-READY alpha reachable' in unreachable.stdout + unreachable.stderr
    # A registry without `self` is a crash: neither 0 nor the not-ready status.
    crash = run_unit(tmp_path, {'hosts': registry['hosts']})
    assert crash.returncode not in (0, expected) and 'KeyError' in crash.stderr
    # A record that cannot be published is never the expected outcome either.
    shutil.rmtree(tmp_path / '.local', ignore_errors=True)
    (tmp_path / '.local').write_text('blocked')
    unpublished = run_unit(tmp_path, registry)
    assert unpublished.returncode == 1, unpublished.stderr
    # An empty registry is a usage error, not a ready or not-ready fleet.
    assert run_unit(tmp_path, {'self': 'coord', 'hosts': []}).returncode == 2


def test_check_records_reports_missing_stale_and_failed_hosts(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from plangraph.spawner import host_ready
    monkeypatch.setenv('HOME', str(tmp_path))
    directory = tmp_path / '.local/state/plangraph/host-ready'
    directory.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(seconds=host_ready.MAX_AGE + 60)).isoformat()
    for name, ok, at, failures in [('good', True, now.isoformat(), []),
                                   ('down', False, now.isoformat(), ['unreachable: other_checks_unknown']),
                                   ('old', True, stale, [])]:
        (directory / f'{name}.json').write_text(json.dumps(
            dict(host=name, ok=ok, checked_at=at, failures=failures)))
    out = []
    monkeypatch.setattr('builtins.print', lambda *a, **k: out.append(' '.join(map(str, a))))
    assert host_ready.check_records(['good', 'down', 'old', 'absent']) == 1
    assert out == ['good ready', 'down not-ready unreachable: other_checks_unknown',
                   'old stale', 'absent missing']
    out.clear()
    assert host_ready.check_records(['good', 'down']) == 0
