import json
import os
import subprocess

import pytest

from plangraph.spawner import doctor, remote_ps


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


@pytest.mark.parametrize('mode,detail', [('ok', 'ok'), ('extra', 'ok'), ('exit', 'host query failed'),
    ('json', 'host query failed'), ('shape', 'host query failed'),
    ('timeout', 'host query failed'), ('missing', 'unavailable'),
    *[(rows, 'host query failed') for rows in [
        ['x'], [None], [3], [[]], [{}],
        [dict(name='a', status='pass')],
        *[[dict(name='a', status='pass', detail='ok') | {key: value}]
          for key in ('name', 'status', 'detail') for value in (None, 3, [], {})],
        [dict(name='a', status='pass', detail='ok'), 'x'],
    ]]])
def test_doctor_rust_sources(monkeypatch, mode, detail):
    import asyncio
    rows = [dict(name='ps_host_a', status='pass', detail='ok')]
    if mode == 'extra':
        rows[0]['extra'] = {'future': 2}
    if isinstance(mode, list):
        rows = mode
    def run(command, **kwargs):
        assert command[1:] == ['doctor-sources']
        assert kwargs['stdin'] == kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs['stdout'] == subprocess.PIPE
        assert kwargs['timeout'] == doctor.TIMEOUT
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(command, 10)
        if mode == 'missing':
            raise FileNotFoundError('PRIVATE-DIAGNOSTIC')
        return subprocess.CompletedProcess(command, int(mode == 'exit'),
            'bad' if mode == 'json' else '{}' if mode == 'shape' else json.dumps(rows))
    async def mcp(*args):
        return 'pass', 'ok'
    monkeypatch.setattr(doctor.subprocess, 'run', run)
    monkeypatch.setattr(remote_ps, 'configured_hosts', lambda: [])
    monkeypatch.setattr(doctor, 'mcp', mcp)
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: None)
    result = asyncio.run(doctor.diagnose())['checks'][0]
    assert result == ({key: rows[0][key] for key in ('name', 'status', 'detail')}
        if mode in ('ok', 'extra') else
        dict(name='ps_hosts', status='fail', detail=detail))


def test_doctor_deadline_many_hosts(monkeypatch):
    import asyncio
    monkeypatch.setattr(remote_ps, 'configured_hosts', lambda: [{}] * 65)
    def run(command, **kwargs):
        assert kwargs['timeout'] == 5 * remote_ps.TIMEOUT + doctor.TIMEOUT
        return subprocess.CompletedProcess(command, 0, '[]')
    async def mcp(*args):
        return 'pass', 'ok'
    monkeypatch.setattr(doctor.subprocess, 'run', run)
    monkeypatch.setattr(doctor, 'mcp', mcp)
    monkeypatch.setattr(doctor.shutil, 'which', lambda name: None)
    asyncio.run(doctor.diagnose())


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
