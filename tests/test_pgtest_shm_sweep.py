import configparser
import os
import subprocess
import sys
from pathlib import Path
import pytest
from pgtest_shm import _sweep_dead_pgtest_dirs


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

    dead_dir = tmp_path / f"pg{dead_pid}-run"
    dead_dir.mkdir()
    live_dir = tmp_path / f"pg{os.getpid()}-run"
    live_dir.mkdir()
    legacy_dir = tmp_path / "pgtest-abc123xy"
    legacy_dir.mkdir()
    other_dir = tmp_path / f"pgx-{dead_pid}-a"
    other_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    symlink_path = tmp_path / f"pg{dead_pid}-l"
    symlink_path.symlink_to(target_dir)

    _sweep_dead_pgtest_dirs(tmp_path)

    assert not dead_dir.exists()
    assert live_dir.is_dir()
    assert legacy_dir.is_dir()
    assert other_dir.is_dir()
    assert symlink_path.is_symlink()
    assert target_dir.is_dir()


def test_sweep_dead_pgtest_worker_dirs(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"], cwd=tmp_path)
    dead.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(dead.pid, 0)
    live = subprocess.Popen(
        [sys.executable, "-c",
         "import os, time; print(os.getpid(), flush=True); time.sleep(60)"],
        cwd=tmp_path, stdout=subprocess.PIPE, text=True,
    )
    try:
        live_pid = int(live.stdout.readline())
        assert live_pid == live.pid
        dead_dir = tmp_path / f"pgtest-{dead.pid}-gw0-x"
        dead_dir.mkdir()
        (dead_dir / "payload").write_text("stale")
        kept = [tmp_path / name for name in (
            f"pgtest-{live_pid}-gw0-x", f"pgtest-{os.getpid()}-gw0-x",
            "pgtest-abc", "pgtest-gw0-abc", "pgtest-0-gw0-x", "target",
        )]
        for directory in kept:
            directory.mkdir()
        link = tmp_path / f"pgtest-{dead.pid}-gw0-link"
        link.symlink_to(kept[-1], target_is_directory=True)

        _sweep_dead_pgtest_dirs(tmp_path)

        assert not dead_dir.exists()
        assert all(directory.is_dir() for directory in kept)
        assert link.is_symlink()
        assert live.poll() is None
    finally:
        live.kill()
        live.wait(timeout=5)
        live.stdout.close()


@pytest.mark.parametrize("prefix", ["pg", "pgtest-"])
def test_sweep_never_probes_own_pid(tmp_path, monkeypatch, prefix):
    own_dir = tmp_path / f"{prefix}{os.getpid()}-gw0-x"
    own_dir.mkdir()

    def forbidden_probe(*args):
        pytest.fail("the sweep must not probe its own pid")

    monkeypatch.setattr(os, "kill", forbidden_probe)
    _sweep_dead_pgtest_dirs(tmp_path)
    assert own_dir.is_dir()


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_sweep_keeps_unverifiable_worker_pid(tmp_path, monkeypatch, error):
    directory = tmp_path / f"pgtest-{os.getpid() + 1}-gw0-x"
    directory.mkdir()
    calls = []

    def unverifiable(pid, signal):
        calls.append((pid, signal))
        raise error

    monkeypatch.setattr(os, "kill", unverifiable)
    _sweep_dead_pgtest_dirs(tmp_path)
    assert calls == [(os.getpid() + 1, 0)]
    assert directory.is_dir()


@pytest.mark.parametrize("explicit_parent", [True, False])
def test_worker_tmpdir_sweeps_before_creation(tmp_path, monkeypatch, pytestconfig,
                                           explicit_parent):
    conftest = pytestconfig.pluginmanager.getplugin(
        str(Path(__file__).with_name("conftest.py").resolve()))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    if explicit_parent:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
    else:
        monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(conftest.tempfile, "tempdir", str(tmp_path))
    swept = []
    monkeypatch.setattr(conftest, "_sweep_dead_pgtest_dirs", swept.append)
    mkdtemp = conftest.tempfile.mkdtemp

    def create_after_sweep(*args, **kwargs):
        assert swept == [str(tmp_path)]
        return mkdtemp(*args, **kwargs)

    monkeypatch.setattr(conftest.tempfile, "mkdtemp", create_after_sweep)
    conftest._use_tmpfs_tempdir()
    path = Path(conftest.tempfile.tempdir)
    assert path.parent == tmp_path
    assert path.name.startswith(f"pgtest-{os.getpid()}-gw0-")
    assert os.environ["TMPDIR"] == str(path)


def test_pytest_tmp_path_retention_config():
    config = configparser.ConfigParser()
    config.read(Path(__file__).resolve().parents[1] / "pytest.ini")
    assert config["pytest"]["tmp_path_retention_count"] == "1"
    assert config["pytest"]["tmp_path_retention_policy"] == "failed"
