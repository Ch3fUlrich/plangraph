"""One push-gate pass reports every Rust and Python failure (LE-TRAIN-FAST-CHECKS-MISS-CROSS-PART-BREAKS)."""
from pathlib import Path

from test_preflight import preflight, repo  # noqa: F401

CARGO_FAILURES = ("test codex_effort::first_leg ... FAILED\n"
                  "test quota::native_windows_home ... FAILED\n"
                  "test result: FAILED. 3 passed; 2 failed\n")


def recording_runner(monkeypatch, outcomes):
    calls = []

    def run(command, cwd, env):
        calls.append(command)
        key = command[1] if command[0] == "cargo" else Path(command[1]).name
        return outcomes.get(key, (0, ""))

    monkeypatch.setattr(preflight, "run", run)
    return calls


def test_cargo_test_runs_with_no_fail_fast(repo, monkeypatch):
    calls = recording_runner(monkeypatch, {})
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "test", "-p", "x"]])
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    assert ["cargo", "test", "--no-fail-fast", "-p", "x"] in calls


def test_a_failing_cargo_step_does_not_hide_the_later_cargo_steps(repo, monkeypatch, capsys):
    calls = recording_runner(monkeypatch, {"fmt": (1, "Diff in lib.rs\n"), "test": (1, CARGO_FAILURES)})
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [
        ["cargo", "fmt", "--check"], ["cargo", "clippy", "-p", "x"], ["cargo", "test", "-p", "x"]])
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 1
    assert [c[1] for c in calls if c[0] == "cargo"] == ["fmt", "clippy", "test"]
    out = capsys.readouterr().out
    assert "FAIL rust" in out
    assert "failing test: codex_effort::first_leg" in out
    assert "failing test: quota::native_windows_home" in out


def test_python_tests_still_run_and_report_after_a_rust_failure(repo, monkeypatch, capsys):
    calls = recording_runner(monkeypatch, {"test": (1, CARGO_FAILURES),
                                           "affected_tests.py": (1, "FAILED tests/test_a.py::t1\n")})
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [["cargo", "test", "-p", "x"]])
    assert preflight.main(["--base", "HEAD"], repo) == 1
    assert any(Path(c[1]).name == "affected_tests.py" for c in calls if c[0] != "cargo")
    out = capsys.readouterr().out
    assert "FAIL rust" in out and "FAIL tests" in out
