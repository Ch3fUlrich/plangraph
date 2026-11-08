"""Committed comparisons must not include checkout-only paths."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def test_head_excludes_untracked_staged_and_worktree_changes(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "tools/affected_tests.py"
    env = dict((key, value) for key, value in os.environ.items() if not key.startswith("GIT_"))
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="core.hooksPath",
               GIT_CONFIG_VALUE_0="/dev/null", GIT_CONFIG_KEY_1="init.templateDir",
               GIT_CONFIG_VALUE_1="", GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
               GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_EMAIL="test@example.invalid")
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)

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
    spec = importlib.util.spec_from_file_location("affected_tests", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = ["tests/test_committed.py", "tests/test_staged.py",
                "tests/test_untracked.py", "tests/test_worktree.py"]
    assert module.changed_files(tmp_path, base) == expected
    assert module.changed_files(tmp_path, base, "") == expected
