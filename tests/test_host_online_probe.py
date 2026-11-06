import os
from pathlib import Path
import shutil
import runpy
import subprocess
import sys

import pytest

PWSH = shutil.which('pwsh')
ROOT = Path(__file__).resolve().parents[1]
BASE = 'PLANGRAPH_PROBE_ROOT="/worker"\nPLANGRAPH_PROBE_NOTIFY_CMD="notify --flag"\n'


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    log = tmp_path / 'commands'
    monkeypatch.setenv('COMMAND_LOG', str(log))
    for name in ('systemctl', 'ssh', 'schtasks'):
        tool = tmp_path / name
        tool.write_text('#!/bin/sh\necho "$0 $*" >> "$COMMAND_LOG"\nexit 99\n')
        tool.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    config = tmp_path / 'watchdog.env'
    config.write_text(BASE + "PLANGRAPH_PROBE_HOSTS='old'\n# keep\n")
    config.chmod(0o600)

    def run(host='new', dry=False, ps=False, path=None):
        args = (['pwsh', '-NoProfile', '-File', str(ROOT / 'deploy/bootstrap.ps1'),
                 '-ProbeHost', host, '-ProbeConfig', str(path or config)] if ps else
                ['bash', str(ROOT / 'deploy/bootstrap.sh'), '--probe-host', host,
                 '--probe-config', str(path or config)])
        if dry:
            args.append('-DryRun' if ps else '--dry-run')
        result = subprocess.run(args, text=True, capture_output=True)
        assert not log.exists(), result.stdout + result.stderr
        return result

    return config, run


def test_probe_host_added_once(probe):
    config, run = probe
    assert run().returncode == 0
    expected = BASE + "PLANGRAPH_PROBE_HOSTS='old new'\n# keep\n"
    assert config.read_text() == expected
    assert run().returncode == 0
    assert config.read_text() == expected
    assert config.stat().st_mode & 0o777 == 0o600
    assert list(config.parent.glob('watchdog.env.*')) == []


def test_invalid_host_or_config_unchanged(probe):
    config, run = probe
    original = config.read_bytes()
    for host in ('-option', 'two words', 'line\nbreak', 'bad;command'):
        assert run(host).returncode != 0
        assert config.read_bytes() == original
    assert run(path=config.parent / 'missing').returncode != 0
    for suffix in ("PLANGRAPH_PROBE_HOSTS=other\n", "export PLANGRAPH_PROBE_HOSTS=other\n"):
        config.write_bytes(original + suffix.encode())
        before = config.read_bytes()
        assert run().returncode != 0
        assert config.read_bytes() == before
    for value in ("'old -bad'", "'unterminated", "'old bad;command'"):
        config.write_text(BASE + 'PLANGRAPH_PROBE_HOSTS=' + value + '\n')
        before = config.read_bytes()
        assert run().returncode != 0
        assert config.read_bytes() == before
    config.chmod(0o644)
    assert run().returncode != 0
    assert config.read_bytes() == before


def test_probe_dry_run_preserves_config(probe):
    config, run = probe
    before = config.read_bytes(), config.stat()
    result = run(dry=True)
    assert result.returncode == 0
    assert result.stdout == 'Would add probe host new\n'
    assert config.read_bytes() == before[0]
    assert config.stat().st_mtime_ns == before[1].st_mtime_ns


@pytest.mark.skipif(not shutil.which('pwsh'), reason='pwsh absent; PowerShell fixture skipped')
def test_powershell_same_failure(probe):
    config, run = probe
    parsed = subprocess.run(['pwsh', '-NoProfile', '-Command',
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('"
        + str(ROOT / 'deploy/bootstrap.ps1').replace("'", "''")
        + "',[ref]$null,[ref]$e); if ($e) { exit 1 }"], capture_output=True)
    assert parsed.returncode == 0
    before = config.read_bytes()
    assert run('bad host', ps=True).returncode != 0
    assert config.read_bytes() == before
    assert run(dry=True, ps=True).returncode == 0
    assert config.read_bytes() == before
    assert run(ps=True).returncode == 0
    assert run(ps=True).returncode == 0
    assert config.read_text().count('old new') == 1


def test_export_duplicate_rejected(probe):
    config, run = probe
    config.write_text(config.read_text() + 'export PLANGRAPH_PROBE_HOSTS=other\n')
    before = config.read_bytes()
    result = run()
    assert result.returncode != 0
    assert 'exactly one hosts assignment required' in result.stderr
    assert config.read_bytes() == before


def test_native_windows_rejected_before_config_access():
    code = (ROOT / 'src/plangraph/spawner/probe_enroll.py').read_text()
    prelude = "import os, pathlib, re, shlex, stat, sys, tempfile; os.name = 'nt'\n"
    result = subprocess.run([sys.executable, '-c', prelude + code], capture_output=True)
    assert result.returncode != 0
    assert b'Probe enrollment requires POSIX pwsh' in result.stderr


def test_powershell_interpreter_detection():
    source = (ROOT / 'deploy/bootstrap.ps1').read_text()
    assert "@('python3', 'python', 'py')" in source
    assert 'WindowsApps' in source
    assert '& $pythonCommand -m plangraph.spawner.stall_watchdog tick --dry-run' in source
    assert '& $pythonCommand -m plangraph.spawner.probe_enroll' in source


@pytest.mark.parametrize('ending', ['\r\n', '\n', ''])
@pytest.mark.parametrize('quote', ["'", '"'])
def test_assignment_format_preserved(probe, ending, quote):
    config, run = probe
    original = 'export PLANGRAPH_PROBE_HOSTS=' + quote + 'old' + quote + ' # note' + ending
    config.write_bytes(original.encode())
    assert run().returncode == 0
    assert config.read_bytes() == original.replace('old', 'old new').encode()


def test_optimized_validation(probe, monkeypatch):
    config, run = probe
    monkeypatch.setenv('PYTHONOPTIMIZE', '1')
    before = config.read_bytes()
    assert run('bad host').returncode != 0
    assert config.read_bytes() == before
    config.chmod(0o644)
    assert run().returncode != 0
    assert config.read_bytes() == before
    config.chmod(0o600)
    config.write_bytes(before + b'PLANGRAPH_PROBE_HOSTS=other\n')
    before = config.read_bytes()
    assert run().returncode != 0
    assert config.read_bytes() == before


def test_enrollment_only_documented():
    assert 'Probe arguments run enrollment only, even with install flags' in (ROOT / 'deploy/README.md').read_text()


@pytest.mark.skipif(not shutil.which('pwsh') or os.name != 'posix', reason='requires POSIX pwsh')
@pytest.mark.parametrize('interpreter', ['python3', 'py'])
def test_powershell_without_python(probe, tmp_path, monkeypatch, interpreter):
    config, run = probe
    tools = tmp_path / 'bin'
    tools.mkdir()
    (tools / 'pwsh').symlink_to(shutil.which('pwsh'))
    (tools / interpreter).symlink_to(sys.executable)
    monkeypatch.setenv('PATH', str(tools))
    assert run(ps=True).returncode == 0
    assert 'old new' in config.read_text()


@pytest.mark.parametrize('ps', [False, pytest.param(True, marks=pytest.mark.skipif(
    not shutil.which('pwsh'), reason='pwsh absent'))])
@pytest.mark.parametrize('quote', ['', "'", '"'])
def test_tab_separated_hosts(probe, ps, quote):
    config, run = probe
    original = BASE + 'PLANGRAPH_PROBE_HOSTS=' + quote + 'old\tnew' + quote + '\n'
    config.write_text(original)
    assert run(ps=ps).returncode == 0
    assert config.read_text() == original
    assert run('extra', ps=ps).returncode == 0
    assert 'old new extra' in config.read_text()


def test_missing_config_has_clean_error(probe):
    config, run = probe
    result = run(path=config.parent / 'missing')
    assert result.returncode != 0
    assert 'config must exist and be private' in result.stderr
    assert 'Traceback' not in result.stderr


def test_symlink_config_rejected_unchanged(probe):
    config, run = probe
    link = config.parent / 'linked.env'
    link.symlink_to(config)
    before = config.read_bytes()
    result = run(path=link)
    assert result.returncode != 0
    assert 'symlink' in result.stderr
    assert link.is_symlink()
    assert config.read_bytes() == before


@pytest.mark.skipif(not shutil.which('pwsh'), reason='requires POSIX pwsh')
def test_powershell_tick_without_python(tmp_path, monkeypatch):
    tools = tmp_path / 'bin'
    tools.mkdir()
    log = tmp_path / 'calls'
    python = tools / 'python3'
    python.write_text('#!/bin/sh\necho "$*" >> "$COMMAND_LOG"\n')
    python.chmod(0o700)
    monkeypatch.setenv('COMMAND_LOG', str(log))
    monkeypatch.setenv('PATH', str(tools))
    result = subprocess.run([PWSH,
        '-NoProfile', '-File', str(ROOT / 'deploy/bootstrap.ps1'), '-DryRun'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ['-m plangraph.spawner.stall_watchdog tick --dry-run']


def test_failed_replace_preserves_private_config(probe, monkeypatch):
    config, _ = probe
    before = config.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['probe_enroll', 'new', str(config), '0'])

    def fail_replace(temporary, destination):
        temporary = Path(temporary)
        assert temporary.parent == config.parent
        assert destination == config
        assert temporary.stat().st_mode & 0o777 == 0o600
        raise OSError('replacement failed')

    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='replacement failed'):
        runpy.run_module('plangraph.spawner.probe_enroll', run_name='__main__')
    assert config.read_bytes() == before
    assert list(config.parent.glob('watchdog.env.*')) == []
