"""C4 real child transport tests; no network or live configuration."""
import os
from pathlib import Path
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from plangraph.keeper import placement_rules_bridge as bridge
from plangraph.keeper.errors import Refused


@pytest.fixture
def helper(monkeypatch, rules_binary):
    binary = rules_binary
    monkeypatch.setattr(bridge, '_bin_env', lambda: {'PG_PLACEMENT_RULES_BIN': str(binary)})
    return binary


@pytest.fixture(scope='session')
def rules_binary(tmp_path_factory):
    import fcntl
    root = Path(__file__).resolve().parents[1]
    cargo = shutil.which('cargo')
    if cargo is None:
        pytest.fail('cargo is required to build the real pg-placement-rules helper')
    shared = tmp_path_factory.getbasetemp()
    if shared.name.startswith('popen-gw'):
        shared = shared.parent
    with (shared / 'placement-rules-build.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stamp = shared / 'placement-rules-built'
        if not stamp.exists():
            subprocess.run([cargo, 'build', '-p', 'pg-worker-core', '--bin',
                            'pg-placement-rules'], cwd=root / 'rust', check=True)
            stamp.touch()
    return root / 'rust/target/debug/pg-placement-rules'


def fake(tmp_path, monkeypatch, script):
    binary = tmp_path / 'helper'
    binary.write_text('#!/bin/sh\n' + script)
    binary.chmod(0o700)
    monkeypatch.setattr(bridge, '_bin_env', lambda: {
        'PG_PLACEMENT_RULES_BIN': str(binary), 'TOKEN': 'private' + '-fixture'})
    return binary


def test_real_rust_helper_roundtrip(helper):
    assert bridge.call_rules({'op': 'specs'})['specs']
    with pytest.raises(Refused, match='rules_invalid'):
        bridge.call_rules({'op': 'unknown'})


def test_fake_sh_child_timeout_reaped(tmp_path, monkeypatch):
    pidfile = tmp_path / 'pid'
    fake(tmp_path, monkeypatch, f'echo $$ > "{pidfile}"\nwhile :; do :; done\n')
    monkeypatch.setattr(bridge, '_TIMEOUT', 0.15)
    with pytest.raises(Refused, match='rules_unavailable'):
        bridge.call_rules({'op': 'specs'})
    with pytest.raises(ChildProcessError):
        os.waitpid(int(pidfile.read_text()), os.WNOHANG)


def test_oversized_output_refused(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, 'while :; do printf "%065536d" 0; done\n')
    with pytest.raises(Refused, match='rules_unavailable'):
        bridge.call_rules({'op': 'specs'})


def test_missing_binary_no_secrets(tmp_path, monkeypatch):
    secret = 'credential' + '-fixture'
    monkeypatch.setattr(bridge, '_bin_env', lambda: {
        'PG_PLACEMENT_RULES_BIN': str(tmp_path / 'missing'), 'TOKEN': secret})
    with pytest.raises(Refused) as exc:
        bridge.call_rules({'op': secret})
    assert secret not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_success_after_deadline_during_cleanup(tmp_path, monkeypatch):
    fake(tmp_path, monkeypatch, 'read -r body; printf \'{"ok":{}}\'')
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(bridge.time, 'monotonic', lambda: next(ticks))
    assert bridge.call_rules({}) == {}


def test_fixture_preserves_process_environ(tmp_path, monkeypatch):
    real = os.environ
    fake(tmp_path, monkeypatch, 'exit 0')
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: os.environ is real).result()


def test_injection_is_stdin_not_shell(tmp_path, monkeypatch, helper):
    marker = tmp_path / 'not-created'
    with pytest.raises(Refused, match='rules_invalid'):
        bridge.call_rules({'op': f'$(touch {marker})'})
    assert not marker.exists()


@pytest.mark.parametrize('script', [
    'printf "not json"', 'printf \'{"ok":{}}\'; exit 2',
    'printf \'{"ok":{},"extra":1}\'',
    'test -z "$TOKEN" || exit 1; read -r body; printf \'{"ok":{}}\'',
])
def test_bad_output_and_sanitized_env(tmp_path, monkeypatch, script):
    fake(tmp_path, monkeypatch, script)
    if script.startswith('test'):
        assert bridge.call_rules({}) == {}
    else:
        with pytest.raises(Refused, match='rules_unavailable'):
            bridge.call_rules({})
