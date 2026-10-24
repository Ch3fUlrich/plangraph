"""Supervisor diagnostics survive rotation, credential echoes and drain failures."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from plangraph.spawner import cli


def launch(monkeypatch, tmp_path, body, probe=None):
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(state=tmp_path))
    monkeypatch.setattr(subprocess, 'run', probe or (lambda *a, **kw: SimpleNamespace(returncode=1)))
    original = subprocess.Popen
    commands = []
    def start(command, **kw):
        commands.append((command, kw))
        command[2] = command[2].replace('supervisor.main(["--supervise"])', 'exec(' + repr(body) + ')')
        return original(command, **kw)
    monkeypatch.setattr(cli, '_start_supervisor', start)
    rc = cli.main(['--state', str(tmp_path), 'spawn', 'x', '--card', '{"task":"test"}', '--leg', 'missing'])
    return rc, commands


def test_rotated_failure_tail(monkeypatch, tmp_path, capsys):
    rc, _ = launch(monkeypatch, tmp_path,
        'sys.stdin.readline();\nfor _ in range(400): os.write(2, b"x" * 8191 + b"\\n")\nraise ImportError("last diagnostic")')
    assert rc == 1
    assert 'ImportError: last diagnostic' in capsys.readouterr().err


def test_disk_redaction_and_private_mode(monkeypatch, tmp_path):
    monkeypatch.setenv('CUSTOM_CREDENTIAL', 'opaque-credential-value')
    launch(monkeypatch, tmp_path, 'sys.stdin.readline(); os.write(2, b"opaque-credential-value\\n"); raise ValueError("end")')
    logs = list(tmp_path.glob('*.log'))
    assert logs
    for log in logs:
        assert 'opaque-credential-value' not in log.read_text()
        assert log.stat().st_mode & 0o777 == 0o600


def test_systemexit_preserved(monkeypatch, tmp_path):
    rc, commands = launch(monkeypatch, tmp_path, 'raise SystemExit(3)')
    command, kw = commands[0]
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=kw['env'], text=True)
    child.communicate('', timeout=10)
    assert child.returncode == 3


@pytest.mark.parametrize('error', [OSError('probe unavailable'), subprocess.TimeoutExpired('import', 10)])
def test_probe_failure_launches(monkeypatch, tmp_path, error):
    def probe(*a, **kw):
        raise error
    rc, commands = launch(monkeypatch, tmp_path, 'raise SystemExit(3)', probe)
    assert rc == 1
    assert commands


def test_unrelated_test_credentials_survive(monkeypatch, request):
    monkeypatch.setenv('MY_TEST_SECRET', 'test-owned')
    # Re-enter the fixture after a test-owned variable exists: broad scrubbing
    # would remove it even though it is unrelated to host provider credentials.
    import conftest
    fixture = conftest.hermetic_spawner_host.__wrapped__(
        monkeypatch, request.getfixturevalue('tmp_path_factory'), request)
    next(fixture)
    assert os.environ['MY_TEST_SECRET'] == 'test-owned'
    next(fixture, None)


def test_missing_log_directory_still_drains(monkeypatch, tmp_path):
    _, commands = launch(monkeypatch, tmp_path, 'raise SystemExit(3)')
    command, kw = commands[0]
    command = list(command)
    command[3] = str(tmp_path / 'removed' / 'stderr.log')
    command[2] = command[2].replace('exec(\'raise SystemExit(3)\')',
        'exec(' + repr('for _ in range(200): os.write(2, b"x" * 8192)\nraise SystemExit(3)') + ')')
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=kw['env'])
    assert child.wait(timeout=10) == 3


def test_site_packages_path_is_appended(monkeypatch, tmp_path):
    import plangraph
    fake_root = tmp_path / 'site-packages'
    monkeypatch.setattr(plangraph, '__file__', str(fake_root / 'plangraph' / '__init__.py'))
    monkeypatch.setenv('PYTHONPATH', '/existing/first')
    saved = []
    monkeypatch.setattr(cli, 'SpawnService', lambda **kw: SimpleNamespace(state=tmp_path))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    def start(command, **kw):
        saved.append(kw)
        return SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO('{"status":"running","run_id":"fake"}\n'))
    monkeypatch.setattr(cli, '_start_supervisor', start)
    assert cli.main(['spawn', 'x', '--card', '{"task":"test"}', '--leg', 'missing']) == 0
    assert saved[0]['env']['PYTHONPATH'] == '/existing/first' + os.pathsep + str(fake_root)


def test_checkout_beats_stale_installed_copy(monkeypatch, tmp_path, capsys):
    stale = tmp_path / 'stale-wheel' / 'plangraph'
    stale.mkdir(parents=True)
    (stale / '__init__.py').write_text('raise ImportError("stale wheel selected")\n')
    monkeypatch.setenv('PYTHONPATH', str(stale.parent))
    # Even a successful isolated probe must not make the child use the stale tree.
    rc, _ = launch(monkeypatch, tmp_path, 'raise ImportError("checkout selected")',
                   lambda *a, **kw: SimpleNamespace(returncode=0))
    assert rc == 1
    error = capsys.readouterr().err
    assert 'ImportError: checkout selected' in error
    assert 'stale wheel selected' not in error


def test_concurrent_supervisors_and_pruning(monkeypatch, tmp_path):
    _, commands = launch(monkeypatch, tmp_path, 'raise SystemExit(3)')
    command, kw = commands[0]
    def run(index):
        cmd = list(command)
        cmd[3] = str(tmp_path / f'supervisor-concurrent-{index}.stderr.log')
        cmd[2] = cmd[2].replace('exec(\'raise SystemExit(3)\')',
            'exec(' + repr(f'os.write(2, b"child-{index}\\n"); raise SystemExit(3)') + ')')
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=kw['env'])
        assert child.wait(timeout=10) == 3
        return Path(cmd[3]).read_text()
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, range(2))) == ['child-0\n', 'child-1\n']
    for index in range(25):
        (tmp_path / f'supervisor-old-{index}.stderr.log').touch()
    launch(monkeypatch, tmp_path, 'raise SystemExit(3)')
    assert len(list(tmp_path.glob('supervisor-*.stderr.log'))) == 20
