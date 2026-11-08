import json
import os
import time

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
    assert remote_ps._fetch(hosts[0], remote_ps.query_host)[0]['reason'] == 'host src missing'


def test_remote_ps_source_prefix_executes(ssh):
    binary, hosts = ssh
    binary.write_text('#!/bin/sh\nfor command do :; done\nexec sh -c "$command"\n')
    assert remote_ps._fetch(hosts[0], remote_ps.query_host)[0]['reason'] == 'host src missing'


@pytest.mark.parametrize('code,state', [(0, 'ok'), (44, 'src missing'), (255, 'unreachable')])
def test_doctor_ps_hosts_states(ssh, monkeypatch, capsys, code, state):
    monkeypatch.setenv('SSH_CODE', str(code))
    rows = ps_host_doctor.source_checks()
    assert [row['name'] for row in rows] == ['ps_host_host-a', 'ps_host_host-b']
    assert [row['detail'] for row in rows] == [state, state]
    assert 'PRIVATE-DIAGNOSTIC' not in json.dumps(rows) + str(capsys.readouterr())


def test_doctor_ps_hosts_parallel_timeout(ssh, monkeypatch):
    monkeypatch.setenv('SSH_DELAY', '2')
    monkeypatch.setattr(ps_host_doctor, 'TIMEOUT', 0.2)
    started = time.monotonic()
    assert all(row['detail'] == 'unreachable' for row in ps_host_doctor.source_checks())
    assert time.monotonic() - started < 0.38
