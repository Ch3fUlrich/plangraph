import os
import subprocess
import sys
import pytest
from conftest import _sweep_dead_pgtest_dirs


def test_sweep_dead_pgtest_dirs(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    try:
        os.kill(dead_pid, 0)
    except ProcessLookupError:
        pass
    else:
        pytest.skip(f"PID {dead_pid} was recycled")

    dead_dir = tmp_path / f"pgtest-{dead_pid}-run"
    dead_dir.mkdir()
    live_dir = tmp_path / f"pgtest-{os.getpid()}-run"
    live_dir.mkdir()
    legacy_dir = tmp_path / "pgtest-abc123xy"
    legacy_dir.mkdir()
    other_dir = tmp_path / f"pgx-{dead_pid}-a"
    other_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    symlink_path = tmp_path / f"pgtest-{dead_pid}-l"
    symlink_path.symlink_to(target_dir)

    _sweep_dead_pgtest_dirs(tmp_path)

    assert not dead_dir.exists()
    assert live_dir.is_dir()
    assert legacy_dir.is_dir()
    assert other_dir.is_dir()
    assert symlink_path.is_symlink()
    assert target_dir.is_dir()
