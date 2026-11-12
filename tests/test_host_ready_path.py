"""Hermetic CLI coverage: SSH and git are binaries on a temporary PATH."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('mode', [
    'green', 'missing', 'behind', 'unreachable', 'unreadable', 'unlisted', 'alias',
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
  *"df -Pk"*) echo 'fake 9000000 0 9000000 0% /';;
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
               PLANGRAPH_OVERFLOW_HOSTS=str(cfg),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-m', 'plangraph.spawner.host_ready', '--json'],
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
