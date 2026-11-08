import json
import os
import subprocess
from threading import Barrier

import pytest

from plangraph.spawner import ps_host_doctor, remote_ps


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    binary = tmp_path / 'ssh'
    binary.write_text('#!/usr/bin/env python3\n'
                      'import os, sys, time\n'
                      'print("PRIVATE-DIAGNOSTIC", file=sys.stderr)\n'
                      'time.sleep(float(os.environ.get("SSH_DELAY", "0")))\n'
                      'sys.exit(int(os.environ.get("SSH_CODE", "0")))\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.defpath)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    config = tmp_path / 'plangraph'
    config.mkdir()
    hosts = [dict(name='host-a', target='host-a', src='/missing/src'),
             dict(name='host-b', target='host-b', win_src='C:/missing/src')]
    (config / 'ps-hosts.json').write_text(json.dumps(dict(hosts=hosts)))
    return binary, hosts


@pytest.mark.parametrize('code,reason', [(44, 'host src missing'),
                                       (255, 'host unreachable'),
                                       (1, 'host query failed')])
def test_remote_ps_source_reasons(ssh, monkeypatch, capsys, code, reason):
    monkeypatch.setenv('SSH_CODE', str(code))
    rows = remote_ps.list_runs([])
    assert [row['reason'] for row in rows] == [reason, reason]
    assert 'PRIVATE-DIAGNOSTIC' not in json.dumps(rows) + str(capsys.readouterr())


def test_remote_ps_source_timeout(ssh, monkeypatch):
    monkeypatch.setenv('SSH_DELAY', '2')
    monkeypatch.setattr(remote_ps, 'TIMEOUT', 0.1)
    assert all(row['reason'] == 'host query failed' for row in remote_ps.list_runs([]))


def test_remote_ps_missing_module(ssh):
    binary, hosts = ssh
    binary.write_text('#!/bin/sh\necho "No module named plangraph" >&2\nexit 1\n')
    assert remote_ps._fetch(hosts[0], remote_ps.query_host)[0]['reason'] == 'host query failed'


def test_remote_ps_source_prefix_executes(ssh):
    binary, hosts = ssh
    binary.write_text('#!/bin/sh\nfor command do :; done\nexec sh -c "$command"\n')
    assert remote_ps._fetch(hosts[0], remote_ps.query_host)[0]['reason'] == 'host src missing'


@pytest.mark.parametrize('code,state', [(0, 'ok'), (44, 'src missing'), (255, 'unreachable'),
                                       (1, 'host query failed')])
def test_doctor_ps_hosts_states(ssh, monkeypatch, capsys, code, state):
    monkeypatch.setenv('SSH_CODE', str(code))
    rows = ps_host_doctor.source_checks()
    assert [row['name'] for row in rows] == ['ps_host_host-a', 'ps_host_host-b']
    assert [row['detail'] for row in rows] == [state, state]
    assert 'PRIVATE-DIAGNOSTIC' not in json.dumps(rows) + str(capsys.readouterr())


def test_doctor_ps_hosts_parallel_timeout(ssh, monkeypatch):
    monkeypatch.setattr(ps_host_doctor, 'TIMEOUT', 0.2)
    in_flight = Barrier(len(ssh[1]))
    def run(command, **kwargs):
        assert kwargs['timeout'] == 0.2
        # Deadlock guard only: serial execution cannot pass this barrier.
        in_flight.wait(timeout=10)
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(ps_host_doctor.subprocess, 'run', run)
    assert all(row['detail'] == 'host query failed' for row in ps_host_doctor.source_checks())


@pytest.mark.parametrize('error', [ValueError, AttributeError])
def test_doctor_invalid_command(monkeypatch, error):
    host = dict(name='host-a', target='host-a', src='/src')
    monkeypatch.setattr(ps_host_doctor, 'configured_hosts', lambda: [host])
    def invalid(host):
        raise error('PRIVATE-DIAGNOSTIC')
    monkeypatch.setattr(ps_host_doctor, 'host_command', invalid)
    assert ps_host_doctor.source_checks() == [
        dict(name='ps_host_host-a', status='fail', detail='invalid configuration')]


@pytest.mark.parametrize('entry', [None, 3, 'host', []])
def test_doctor_invalid_entry(monkeypatch, entry):
    monkeypatch.setattr(ps_host_doctor, 'configured_hosts', lambda: [entry])
    assert ps_host_doctor.source_checks() == [
        dict(name='ps_hosts', status='fail', detail='invalid configuration')]


def test_stderr_does_not_control_result(ssh):
    binary, hosts = ssh
    binary.write_text('#!/usr/bin/env python3\nimport sys\n'
                      'sys.stderr.write("No module named plangraph" * 100000)\n'
                      'print(\'{"runs": []}\')\n')
    assert remote_ps._fetch(hosts[0], remote_ps.query_host) == []


@pytest.mark.parametrize('wsl', [False, True])
def test_source_paths_are_literal(tmp_path, wsl):
    source = tmp_path / "source space'; touch INJECTED; echo '"
    source.mkdir()
    host = dict(src=str(source))
    if wsl:
        host['wsl_distro'] = 'Ubuntu'
        binary = tmp_path / 'wsl'
        binary.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
        binary.chmod(0o755)
    command = remote_ps.host_command(host)
    result = subprocess.run(command, shell=True, cwd=tmp_path,
                            env={'PATH': str(tmp_path) + os.pathsep + os.defpath})
    assert result.returncode == 0
    assert not (tmp_path / 'INJECTED').exists()
    assert remote_ps.host_command(host, query=True).startswith(
        'wsl -d Ubuntu -- sh -c ' if wsl else remote_ps.source_check(host))


def test_windows_precedence_and_validation():
    host = dict(win_src='C:/source space', wsl_distro='Ubuntu')
    assert remote_ps.host_command(host) == remote_ps.source_check(host)
    assert remote_ps.host_command(host, query=True).startswith(remote_ps.source_check(host))
    for path in ['C:/x&echo bad', 'C:/%PATH%', 'C:/x"', 'C:/x\n']:
        with pytest.raises(ValueError):
            remote_ps.host_command(dict(win_src=path), query=True)
