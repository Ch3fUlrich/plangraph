"""Committed comparisons must not include checkout-only paths."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_head_excludes_untracked_staged_and_worktree_changes(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools/affected_tests.py"
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
               GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_EMAIL="test@example.invalid")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, env=env, text=True).strip()

    git("init", "-q")
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("committed", "staged", "worktree"):
        (tests / f"test_{name}.py").write_text("VALUE = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tests / "test_committed.py").write_text("VALUE = 2\n")
    git("commit", "-qam", "committed change")
    head = git("rev-parse", "HEAD")
    (tests / "test_staged.py").write_text("VALUE = 3\n")
    git("add", ".")
    (tests / "test_worktree.py").write_text("VALUE = 4\n")
    (tests / "test_untracked.py").write_text("VALUE = 5\n")

    def plan(*args):
        output = subprocess.check_output(
            [sys.executable, str(script), "--root", str(tmp_path), "--base", base, *args],
            env=env, text=True)
        return json.loads(output)["tests"]

    assert plan("--head", head) == ["tests/test_committed.py"]
    assert plan("--head", base) == []
    assert plan() == ["tests/test_committed.py", "tests/test_staged.py",
                      "tests/test_untracked.py", "tests/test_worktree.py"]
