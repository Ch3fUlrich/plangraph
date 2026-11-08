"""Hermetic CLI coverage: SSH and git are binaries on a temporary PATH."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('mode', ['green', 'missing', 'behind', 'unreachable', 'unreadable'])
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
    cfg.write_text(json.dumps({'hosts': {'host-a': host, 'host-b': dict(host, ssh='host-b')}}))
    ps = tmp_path / 'plangraph'
    ps.mkdir()
    (ps / 'ps-hosts.json').write_text(json.dumps({'hosts': [dict(
        name='host-a', target='host-a', src='/deleted' if mode == 'missing' else '/source')]}))
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path),
               PATH=str(binary) + os.pathsep + os.environ['PATH'], MODE=mode,
               PLANGRAPH_OVERFLOW_HOSTS=str(cfg),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    result = subprocess.run([sys.executable, '-m', 'plangraph.spawner.host_ready', '--json'],
                            env=env, capture_output=True, text=True, timeout=15)
    rows = json.loads(result.stdout)
    assert result.returncode == (mode != 'green')
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
    else:
        assert 'HOST-NOT-READY host-a unreachable other_checks_unknown' in result.stderr
        assert all(r['behind_main'] is None and r['doctor_ok'] is None
                   and r['src_ok'] is None and r['spawner_session'] is None for r in rows)
