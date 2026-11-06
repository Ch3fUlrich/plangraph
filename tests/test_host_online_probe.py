import os
from pathlib import Path
import shutil
import subprocess

import pytest

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
    for suffix in ("PLANGRAPH_PROBE_HOSTS=other\n",):
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
    before = config.read_bytes()
    assert run('bad host', ps=True).returncode != 0
    assert config.read_bytes() == before
    assert run(dry=True, ps=True).returncode == 0
    assert config.read_bytes() == before
    assert run(ps=True).returncode == 0
    assert run(ps=True).returncode == 0
    assert config.read_text().count('old new') == 1
