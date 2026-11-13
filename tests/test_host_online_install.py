import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'commands'
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'PLANGRAPH_SPAWN_STATE'):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv('COMMAND_LOG', str(log))
    monkeypatch.setenv('REAL_PYTHON', sys.executable)
    monkeypatch.setenv('PYTHONPATH', str(ROOT / 'src'))
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path / 'autoos'))
    (tmp_path / 'autoos').mkdir()
    (tmp_path / 'autoos/setup.sh').write_text('echo setup >> "$COMMAND_LOG"\n')
    program = '''import os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(name + ' ' + ('-c' if args[:1] == ['-c'] else ' '.join(args)) + '\\n')
if 'tick' in args:
    print('silent: 0')
    sys.exit(int(os.environ.get('COUNT_EXIT', '0')))
if 'init-and-seed' in args:
    sys.exit(int(os.environ.get('CATALOG_EXIT', '0')))
if name in ('python', 'python3') and 'install' in args and os.environ.get('REAL_INSTALL'):
    sys.exit(subprocess.call([os.environ['REAL_PYTHON']] + args))
'''
    for name in ('python', 'python3', 'systemctl', 'ssh', 'schtasks', 'powershell.exe', 'AutoOS', 'plangraph', 'wsl.exe'):
        path = bin_dir / name
        path.write_text('#!' + sys.executable + '\n' + program)
        path.chmod(0o755)
    def run(*args, ps=False):
        cmd = [shutil.which('pwsh'), '-NoProfile', '-File', str(ROOT / 'deploy/bootstrap.ps1')] if ps else ['bash', str(ROOT / 'deploy/bootstrap.sh')]
        result = subprocess.run(cmd + list(args), capture_output=True, text=True, cwd=ROOT)
        return result, log.read_text().splitlines()
    return run, tmp_path


def test_bootstrap_dry_run_writes_nothing(host):
    run, root = host
    before = set(root.rglob('*'))
    result, commands = run('--dry-run', '--autoos-root', str(root / 'autoos'))
    assert result.returncode == 0, result.stderr
    assert sum('tick --dry-run' in c for c in commands) == 1
    assert all(c.endswith(' -c') or 'tick --dry-run' in c for c in commands)
    assert set(root.rglob('*')) - before == {root / 'commands'}


def test_count_precedes_install(host):
    result, commands = host[0]()
    assert result.returncode == 0, result.stderr
    assert next(i for i, c in enumerate(commands) if 'tick --dry-run' in c) < next(i for i, c in enumerate(commands) if 'init-and-seed' in c) < next(i for i, c in enumerate(commands) if 'install --host linux' in c)


@pytest.mark.parametrize('failure', ['COUNT_EXIT', 'CATALOG_EXIT'])
def test_count_failure_blocks_install(host, monkeypatch, failure):
    monkeypatch.setenv(failure, '7')
    result, commands = host[0]()
    assert result.returncode != 0
    assert not any('install' in c for c in commands)
    if failure == 'COUNT_EXIT':
        assert not any('init-and-seed' in c for c in commands)


def test_default_path_never_calls_autoos(host):
    result, commands = host[0]()
    assert result.returncode == 0
    assert not any('AutoOS' in c or 'setup' in c for c in commands)
    assert 'AutoOS' not in result.stderr
    assert sum(c.startswith('plangraph install --only ') for c in commands) == 1


def test_fake_systemctl_enables_watchdog_only(host, monkeypatch):
    monkeypatch.setenv('REAL_INSTALL', '1')
    result, commands = host[0]()
    assert result.returncode == 0, result.stderr
    assert [c for c in commands if ' enable ' in c] == ['systemctl --user enable --now plangraph-stall-watchdog.timer']
    assert 'systemctl --user disable --now plangraph-remote-probe.timer' in commands


@pytest.mark.skipif(not shutil.which('pwsh'), reason='pwsh unavailable')
def test_powershell_same_order_and_failure(host, monkeypatch):
    result, commands = host[0]('-DryRun', ps=True)
    assert result.returncode == 0, result.stderr
    assert commands == ['python3 -m plangraph.spawner.stall_watchdog tick --dry-run']
    (host[1] / 'commands').unlink()
    result, commands = host[0](ps=True)
    assert result.returncode == 0, result.stderr
    assert ['tick' in commands[0], 'init-and-seed' in commands[1], 'install --only' in commands[2], 'install --host windows' in commands[3]] == [True] * 4
    (host[1] / 'commands').unlink()
    monkeypatch.setenv('COUNT_EXIT', '7')
    result, commands = host[0](ps=True)
    assert result.returncode != 0
    assert not any('install' in c or 'init-and-seed' in c for c in commands)
