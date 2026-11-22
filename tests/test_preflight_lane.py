"""preflight --lane: the seconds-fast policy and Rust lint checks (LAND-PUSH-GATE-FAST-ONLY)."""
from pathlib import Path
import os
import subprocess

import pytest

from test_preflight import fake_runner, preflight, repo  # noqa: F401

RUSTUP_HOME = os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup"))
CARGO = [["cargo", "fmt", "--check"], ["cargo", "clippy", "-p", "x", "--all-targets", "--", "-D", "warnings"], ["cargo", "test", "-p", "x"]]


def names(calls):
    return [c[0][1] if c[0][0] == "cargo" else Path(c[0][1]).name for c in calls]


def test_lane_runs_scrub_policy_skill_lint_and_cargo_fmt_only(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [list(CARGO[0]), list(CARGO[2])])
    assert preflight.main(["--base", "HEAD", "--lane"], repo) == 0
    ran = names(calls)
    assert "scrub_check.py" in ran and "check_file_size.py" in ran and "check_new_code_lang.py" in ran
    assert "check_changelog_docs.py" in ran and "ci_commit_gate.py" in ran and "skill-rules.py" in ran
    assert "fmt" in ran
    assert "test" not in ran and "affected_tests.py" not in ran
    assert "SKIP tests" in capsys.readouterr().out


def test_a_failing_lane_policy_check_still_fails_the_lane(repo, monkeypatch):
    fake_runner(monkeypatch, "skill-rules.py", "skill rule lint line too long\n")
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [])
    assert preflight.main(["--base", "HEAD", "--lane"], repo) == 1


def test_without_lane_the_full_family_set_is_unchanged(repo, monkeypatch):
    calls = fake_runner(monkeypatch)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [list(c) for c in CARGO])
    assert preflight.main(["--base", "HEAD"], repo) == 0
    ran = names(calls)
    assert "clippy" in ran and "test" in ran and "affected_tests.py" in ran
    assert "skill-rules.py" not in ran


@pytest.mark.parametrize(("rust_source", "doc_lines", "refusal"), [
    ("pub fn answer() -> u8 { 42 }", 668, None),
    ("pub fn answer() -> u8 { return 42; }", 668, "clippy::needless_return"),
    ("pub fn answer() -> u8 { 42 }", 669, "669 lines > doc cap 668"),
])
def test_lane_checks_a_committed_range(repo, monkeypatch, capsys, rust_source, doc_lines, refusal):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.invalid", *args], text=True).strip()

    base = git("rev-parse", "HEAD")
    (repo / "rust/src").mkdir(parents=True)
    (repo / "rust/Cargo.toml").write_text(
        '[package]\nname = "preflight-lane-fixture"\nversion = "0.1.0"\nedition = "2021"\n')
    (repo / "rust/src/lib.rs").write_text(rust_source.replace(" { ", " {\n    ").replace(" }", "\n}") + "\n")
    (repo / "docs").mkdir()
    (repo / "docs/SPAWNER.md").write_text("text\n" * doc_lines)
    git("add", ".")
    git("commit", "-qm", "fixture change")

    monkeypatch.setenv("RUSTUP_HOME", RUSTUP_HOME)
    toolchain = Path(preflight.__file__).resolve().parents[1] / "rust-toolchain.toml"
    (repo / toolchain.name).write_bytes(toolchain.read_bytes())
    real_run = preflight.run
    fake_runner(monkeypatch)
    fake_run = preflight.run
    monkeypatch.setattr(preflight, "running_disk_refusal", lambda _: None)
    monkeypatch.setattr(preflight, "trim_shared_target", lambda _: None)
    monkeypatch.setattr(preflight, "run", lambda command, cwd, env: (
        real_run(command, cwd, env) if command[:2] in (["cargo", "fmt"], ["cargo", "clippy"])
        or Path(command[1]).name == "check_file_size.py" else fake_run(command, cwd, env)))

    assert preflight.main(["--base", base, "--lane"], repo) == int(refusal is not None)
    output = capsys.readouterr().out
    assert "cargo fmt --check" in output
    assert "cargo clippy -p preflight-lane-fixture --all-targets -- -D warnings" in output
    if refusal:
        assert refusal in output
    if doc_lines > 668:
        assert "docs/SPAWNER.md" in output
