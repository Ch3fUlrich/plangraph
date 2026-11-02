"""Shared helpers for the check_changelog_docs tests (tmp git repos, identity via env only)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_changelog_docs.py"


def git(cwd, *args, env=None):
    base_env = dict(os.environ)
    base_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    base_env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    base_env["GIT_CONFIG_NOSYSTEM"] = "1"
    base_env["GIT_AUTHOR_NAME"] = "Test Author"
    base_env["GIT_AUTHOR_EMAIL"] = "author@example.invalid"
    base_env["GIT_COMMITTER_NAME"] = "Test Committer"
    base_env["GIT_COMMITTER_EMAIL"] = "committer@example.invalid"
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=base_env,
    )


def commit_files(repo, files: dict[str, str], message: str, trailers: list[str] | None = None) -> str:
    for rel_path, content in files.items():
        p = repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        res = git(repo, "add", rel_path)
        assert res.returncode == 0, f"git add failed: {res.stderr}"
    full_msg = message
    if trailers:
        full_msg = f"{message}\n\n" + "\n".join(trailers) + "\n"
    res = git(repo, "commit", "-m", full_msg)
    assert res.returncode == 0, f"git commit failed: {res.stderr}"
    head = git(repo, "rev-parse", "HEAD")
    return head.stdout.strip()


@pytest.fixture
def test_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    res = git(repo, "init", "-b", "main")
    assert res.returncode == 0
    base = commit_files(repo, {"README.md": "# Readme\n"}, "initial commit")
    return repo, base


def run_check(repo, base=None, *extra_args, env=None):
    cmd = [sys.executable, str(SCRIPT)]
    if base is not None:
        cmd.extend(["--base", base])
    cmd.extend(extra_args)
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=run_env)


def commit_raw(repo, files: dict[str, str], full_msg: str) -> str:
    for rel_path, content in files.items():
        p = repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        assert git(repo, "add", rel_path).returncode == 0
    res = git(repo, "commit", "-m", full_msg)
    assert res.returncode == 0, f"git commit failed: {res.stderr}"
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def docs_map_repo(repo, rule):
    commit_files(repo, {".docs-map": f"# enforce-after: 2026-11-08\n{rule}\n"}, "add docs map")
    return git(repo, "rev-parse", "HEAD").stdout.strip()
