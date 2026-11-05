"""Hermetic checkout audits and real local pushes through the installed gate."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/check_push_gate.py"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("GIT_", "PLANGRAPH_SCRUB_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PLANGRAPH_PUBLIC_REPO", "fixture/public")
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-qb", "main")
    git(r, "config", "user.name", "fixture")
    git(r, "config", "user.email", "fixture@example.invalid")
    for rel in ("tools/hooks/pre-push", "tools/split/scrub_check.py",
                "tools/check_file_size.py"):
        dest = r / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    shutil.copy2(ROOT / ".filesize-baseline", r / ".filesize-baseline")
    commit(r)
    git(r, "remote", "add", "origin", "https://example.invalid/fixture/public")
    return r


def commit(r):
    git(r, "add", "-A")
    git(r, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def test_public_private_and_install(repo):
    assert run(repo).stdout.startswith("NO-GATE ")
    assert run(repo).returncode == 1
    assert run("--install", repo).returncode == 0
    assert git(repo, "config", "core.hooksPath") == "tools/hooks"
    assert run(repo).returncode == 0
    git(repo, "remote", "set-url", "origin", "https://example.invalid/private")
    git(repo, "config", "core.hooksPath", "missing")
    assert run(repo).returncode == 0
    result = run("--install", repo, "--force")
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""
    assert git(repo, "config", "core.hooksPath") == "missing"


@pytest.mark.parametrize("damage", ["different", "mode", "missing"])
def test_broken_gate(repo, damage):
    assert run("--install", repo).returncode == 0
    hook = repo / "tools/hooks/pre-push"
    if damage == "different":
        hook.write_text("#!/bin/sh\nexit 0\n")
    elif damage == "mode":
        hook.chmod(0o644)
    else:
        git(repo, "config", "core.hooksPath", "missing")
    assert run(repo).returncode == 1


def test_worktrees_unknown_and_credentials(repo, tmp_path):
    git(repo, "remote", "set-url", "origin", "https://user:secret@example.invalid/fixture/public")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "linked", str(linked))
    result = run("--root", tmp_path)
    assert result.returncode == 1 and result.stdout.count("NO-GATE ") == 1
    assert "user:" not in result.stdout and "secret" not in result.stdout
    result = run(tmp_path / "absent")
    assert result.returncode == 1 and result.stdout.startswith("UNREADABLE ")
    (repo / ".git/config").write_text("[broken\n")
    result = run(repo)
    assert result.returncode == 1 and result.stdout.startswith("UNREADABLE ")


def test_install_refusals(repo):
    git(repo, "config", "core.hooksPath", "other")
    assert run("--install", repo).returncode == 1
    assert git(repo, "config", "core.hooksPath") == "other"
    git(repo, "config", "--add", "core.hooksPath", "extra")
    assert run("--install", repo, "--force").returncode == 0
    assert git(repo, "config", "--get-all", "core.hooksPath") == "tools/hooks"
    git(repo, "rm", "tools/hooks/pre-push")
    commit(repo)
    git(repo, "config", "core.hooksPath", "other")
    result = run("--install", repo, "--force")
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""
    assert git(repo, "config", "core.hooksPath") == "other"


def test_global_gate_and_usage(repo):
    git(repo, "config", "--global", "core.hooksPath", str(repo / "tools/hooks"))
    assert run(repo).returncode == 0
    git(repo, "config", "core.hooksPath", "missing")
    assert run(repo).returncode == 1
    git(repo, "config", "--add", "core.hooksPath", "tools/hooks")
    assert run(repo).returncode == 0
    assert run("--force", repo).returncode == 2


@pytest.mark.parametrize("shape", ["home", "word", "id"])
def test_installed_real_push(repo, tmp_path, monkeypatch, shape):
    bare = tmp_path / "public.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "set-url", "origin", str(bare))
    monkeypatch.setenv("PLANGRAPH_PUBLIC_REPO", str(bare))
    git(repo, "push", "-q", "origin", "main")
    assert run("--install", repo).returncode == 0
    word = "synthetic" + "privateword"
    words = tmp_path / "words"
    words.write_text(word + "\n")
    monkeypatch.setenv("PLANGRAPH_SCRUB_WORDS", str(words))
    content = {"home": "/" + "home/fixture/notes", "word": word, "id": "D-1234"}[shape]
    (repo / "note.txt").write_text(content + "\n")
    commit(repo)
    result = subprocess.run(["git", "-C", str(repo), "push", "origin", "main"],
                            capture_output=True, text=True)
    assert (result.returncode == 0) == (shape == "id"), result.stderr
    if shape != "id":
        assert "scrub hit" in result.stderr


def test_unset_repo_is_conservative(repo, monkeypatch):
    monkeypatch.delenv("PLANGRAPH_PUBLIC_REPO")
    git(repo, "remote", "set-url", "origin", "https://example.invalid/other")
    before = (repo / ".git/config").read_bytes()
    result = run("--install", repo, "--force")
    assert result.returncode == 1 and "PLANGRAPH_PUBLIC_REPO" in result.stdout
    assert (repo / ".git/config").read_bytes() == before
    assert run(repo).returncode == 1


def test_install_checks_bytes(repo):
    (repo / "tools/hooks/pre-push").write_text("#!/bin/sh\nexit 0\n")
    assert run("--install", repo).returncode == 1


def test_subdirectory_gate(repo):
    assert run("--install", repo).returncode == 0
    assert run(repo / "tools").returncode == 0
