"""preflight --lane: the seconds-fast checks only, no test of any kind (LAND-PUSH-GATE-FAST-ONLY)."""
from pathlib import Path

from test_preflight import fake_runner, preflight, repo  # noqa: F401

CARGO = [["cargo", "fmt", "--check"], ["cargo", "clippy", "-p", "x", "--all-targets"], ["cargo", "test", "-p", "x"]]


def names(calls):
    return [c[0][1] if c[0][0] == "cargo" else Path(c[0][1]).name for c in calls]


def test_lane_runs_scrub_policy_skill_lint_and_cargo_fmt_only(repo, monkeypatch, capsys):
    calls = fake_runner(monkeypatch)
    monkeypatch.setattr(preflight, "rust_commands", lambda *a: [list(c) for c in CARGO])
    assert preflight.main(["--base", "HEAD", "--lane"], repo) == 0
    ran = names(calls)
    assert "scrub_check.py" in ran and "check_file_size.py" in ran and "check_new_code_lang.py" in ran
    assert "check_changelog_docs.py" in ran and "ci_commit_gate.py" in ran and "skill-rules.py" in ran
    assert "fmt" in ran
    assert "clippy" not in ran and "test" not in ran and "affected_tests.py" not in ran
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
