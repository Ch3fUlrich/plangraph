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
@pytest.fixture(params=['bash', pytest.param('pwsh', marks=pytest.mark.skipif(
    not shutil.which('pwsh'), reason='requires POSIX pwsh'))])
def probe(tmp_path, monkeypatch, request):
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
    ps = request.param == 'pwsh'; wrapper = ROOT / ('deploy/bootstrap.ps1' if ps else 'deploy/bootstrap.sh')
    if ps:
        result = subprocess.run(['pwsh', '-NoProfile', '-Command',
                                 "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + str(wrapper)
                                 + "',[ref]$null,[ref]$e); if ($e) { exit 1 }"], capture_output=True)
        assert result.returncode == 0
    def run(host='new', dry=False, path=None):
        args = (['pwsh', '-NoProfile', '-File', str(wrapper), '-ProbeHost', host,
                 '-ProbeConfig', str(path or config)] if ps else
                ['bash', str(wrapper), '--probe-host', host, '--probe-config', str(path or config)])
        result = subprocess.run(args + (['-DryRun' if ps else '--dry-run'] if dry else []),
                                capture_output=True, text=True)
        assert not log.exists(), result.stdout + result.stderr
        return result
    return config, run
@pytest.mark.parametrize('quote,ending', [("'", '\n'), ('"', '\r\n'), ('', '')])
def test_added_once_plain_assignment(probe, quote, ending):
    config, run = probe; original = BASE + 'export PLANGRAPH_PROBE_HOSTS=' + quote + 'old\tnew' + quote + ending
    config.write_bytes(original.encode())
    assert run().returncode == 0 and config.read_bytes() == original.encode()
    assert run('extra').returncode == run('extra').returncode == 0
    assert config.read_bytes() == (BASE + 'export PLANGRAPH_PROBE_HOSTS='
                                  + (quote or "'") + 'old new extra' + (quote or "'") + ending).encode()
    assert config.stat().st_mode & 0o777 == 0o600
    assert list(config.parent.glob('watchdog.env.*')) == []
@pytest.mark.parametrize('case', ['host', 'missing', 'duplicate', 'export', 'syntax',
                                 'configured', 'eval', 'mode', 'symlink'])
def test_invalid_unchanged(probe, case, monkeypatch):
    config, run = probe
    monkeypatch.setenv('PYTHONOPTIMIZE', '1')
    suffix = {'duplicate': 'PLANGRAPH_PROBE_HOSTS=other\n', 'export': 'export PLANGRAPH_PROBE_HOSTS=other\n'}
    if case in suffix:
        config.write_text(config.read_text() + suffix[case])
    if case in ('syntax', 'configured', 'eval'):
        value = {'syntax': "'unterminated", 'configured': "'old -bad'",
                 'eval': '$(ssh forbidden)'}[case]
        config.write_text(BASE + 'PLANGRAPH_PROBE_HOSTS=' + value + '\n')
    path = config.parent / case if case in ('symlink', 'missing') else config
    if case == 'symlink': path.symlink_to(config)
    if case == 'mode': config.chmod(0o644)
    before = config.read_bytes(), config.stat()
    assert run('bad;command' if case == 'host' else 'new', path=path).returncode != 0
    assert config.read_bytes() == before[0] and config.stat().st_mtime_ns == before[1].st_mtime_ns
    assert case != 'symlink' or path.is_symlink()
def test_dry_run_prints_only(probe):
    config, run = probe
    before = config.read_bytes(), config.stat()
    result = run(dry=True)
    assert result.returncode == 0
    assert result.stdout == 'Would add probe host new\n'
    assert not result.stderr and config.read_bytes() == before[0]
    assert config.stat() == before[1]
def test_failed_replace_is_private_sibling(probe, monkeypatch):
    config, _ = probe; before = config.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['probe_enroll', 'new', str(config), '0'])
    def fail(temporary, destination):
        assert Path(temporary).parent == config.parent and destination == config
        assert Path(temporary).stat().st_mode & 0o777 == 0o600
        raise OSError('replacement failed')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='replacement failed'):
        runpy.run_module('plangraph.spawner.probe_enroll', run_name='__main__')
    assert config.read_bytes() == before and not list(config.parent.glob('watchdog.env.*'))
def test_size_delegation_and_human_gate():
    assert len(Path(__file__).read_text().splitlines()) <= 115
    for name in ('bootstrap.sh', 'bootstrap.ps1'):
        assert '-m plangraph.spawner.probe_enroll' in (ROOT / 'deploy' / name).read_text()
    readme = (ROOT / 'deploy/README.md').read_text()
    assert 'timer stays disabled' in readme and 'A human sets root/notify config' in readme


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


def test_enrollment_only_documented():
    assert 'Probe arguments run enrollment only, even with install flags' in (ROOT / 'deploy/README.md').read_text()
