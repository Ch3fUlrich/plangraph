"""Host opt-in test placement policy."""
import pytest

from bash_guard_support import guard  # noqa: F401

BARE = ["pytest", "py.test -q", "python -m pytest", "python3 -m pytest",
        "python3.12 -m pytest", "cargo test", "cargo nextest run",
        "X=1 pytest", "env X=1 pytest", "nice -n 10 pytest",
        "timeout 30 pytest", "env X=1 nice -n 10 timeout 30 cargo test",
        "bash -c 'pytest -q'", "eval 'cargo test'",
        "systemd-run --user --scope pytest"]
ALLOWED = ["cargo build", "cargo clippy", "pytest --version",
           "pytest --collect-only", "grep pytest x", "echo cargo test",
           "nice -n 10 grep pytest x", "pgtest pytest", "pgtest cargo test",
           "systemd-run --slice agent-tests.slice pytest",
           "nice -n 10 systemd-run --user --scope -q --slice=agent-tests.slice "
           "-p MemoryMax=1G python3 -m pytest"]


@pytest.mark.parametrize("command", BARE + ALLOWED)
def test_no_marker(guard, monkeypatch, tmp_path, command):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert guard.check(command) is None


@pytest.mark.parametrize("command", BARE + ALLOWED)
def test_marker(guard, monkeypatch, tmp_path, command):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    marker = tmp_path / "plangraph" / "pgtest-required"
    marker.parent.mkdir()
    marker.touch()
    reason = guard.check(command)
    if command in BARE:
        assert reason and "use pgtest <command>" in reason
    else:
        assert reason is None


def test_unreadable_marker(guard, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    marker = tmp_path / "plangraph" / "pgtest-required"
    marker.mkdir(parents=True)  # Cannot be opened as a marker file, even as root.
    assert guard.check("pytest") is None


def test_default_marker(guard, monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    marker = tmp_path / ".config/plangraph/pgtest-required"
    marker.parent.mkdir(parents=True)
    marker.touch()
    assert "use pgtest" in guard.check("pytest")
