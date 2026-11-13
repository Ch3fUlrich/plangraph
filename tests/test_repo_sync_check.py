"""Hermetic checkout-safety contracts for tools/repo_sync_check.py."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/repo_sync_check.py"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.com")
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    git(tmp_path, "init", "--bare", "--template=", "--initial-branch=main", str(origin))
    git(tmp_path, "clone", "--template=", str(origin), str(seed))
    (seed / "tracked.txt").write_text("initial\n")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "push", "origin", "main")
    repo = tmp_path / "code" / "sample"
    repo.parent.mkdir()
    git(tmp_path, "clone", "--template=", str(origin), str(repo))
    (seed / "tracked.txt").write_text("remote\n")
    (seed / "incoming.txt").write_text("incoming\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "advance")
    git(seed, "push", "origin", "main")
    return repo, seed, origin


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, timeout=20)


def state(repo):
    """Fetch metadata may advance; checkout, local refs and index must not."""
    return (git(repo, "rev-parse", "HEAD"), (repo / ".git/HEAD").read_bytes(),
            git(repo, "for-each-ref", "refs/heads"), (repo / ".git/index").read_bytes(),
            {name: (repo / ".git" / name).read_bytes() for name in
             ("ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD") if (repo / ".git" / name).is_file()},
            {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*")
             if ".git" not in p.relative_to(repo).parts and p.is_file()})


def alert_unchanged(repo, reason, behind=1, *args):
    (repo / "private-untracked.txt").write_text("private fixture content\n")
    before = state(repo)
    result = run("--repo", repo, *args)
    assert result.returncode == 1
    assert result.stdout == f"ALERT {repo.name} behind={behind} reason={reason}\n"
    assert result.stderr == ""
    assert state(repo) == before


def test_clean_behind_main_fast_forwards(repos):
    repo, seed, _ = repos
    result = run("--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert git(repo, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
    assert (repo / "tracked.txt").read_text() == "remote\n"


@pytest.mark.parametrize("staged", [False, True])
def test_modified_tracked_file_is_untouched(repos, staged):
    repo, _, _ = repos
    (repo / "tracked.txt").write_text("local modification\n")
    if staged:
        git(repo, "add", "tracked.txt")
    alert_unchanged(repo, "tracked-changes")


def test_detached_head_is_untouched(repos):
    repo, _, _ = repos
    git(repo, "switch", "--detach")
    alert_unchanged(repo, "detached")


def test_feature_branch_is_untouched(repos):
    repo, _, _ = repos
    git(repo, "switch", "-c", "feature")
    alert_unchanged(repo, "other-branch:feature")


def test_diverged_main_is_untouched(repos):
    repo, _, _ = repos
    (repo / "local.txt").write_text("local commit\n")
    git(repo, "add", "local.txt")
    git(repo, "commit", "-m", "local")
    alert_unchanged(repo, "diverged")


def test_untracked_file_does_not_block_fast_forward(repos):
    repo, seed, _ = repos
    untracked = repo / "private-untracked.txt"
    untracked.write_bytes(b"private fixture content\n")
    result = run("--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert git(repo, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
    assert untracked.read_bytes() == b"private fixture content\n"


def test_fetch_failure_is_untouched(repos):
    repo, _, _ = repos
    git(repo, "remote", "set-url", "origin", str(repo.parent / "absent.git"))
    alert_unchanged(repo, "fetch-failed", 0)


@pytest.mark.parametrize("ignored", [False, True])
def test_untracked_collision_is_untouched(repos, ignored):
    repo, _, _ = repos
    (repo / "incoming.txt").write_text("do not overwrite\n")
    if ignored:
        (repo / ".git/info").mkdir(exist_ok=True)
        (repo / ".git/info/exclude").write_text("incoming.txt\n")
    alert_unchanged(repo, "diverged")


def test_dry_run_does_not_merge(repos):
    repo, _, _ = repos
    before = state(repo)
    result = run("--repo", repo, "--dry-run")
    assert (result.returncode, result.stdout, result.stderr) == (
        0, "DRY-RUN sample behind=1\n", "")
    assert state(repo) == before


def test_dry_run_still_alerts_for_unsafe_checkout(repos):
    repo, _, _ = repos
    git(repo, "switch", "-c", "feature")
    alert_unchanged(repo, "other-branch:feature", 1, "--dry-run")


def test_default_discovery_and_verbose(repos):
    repo, _, _ = repos
    for name in ("skip-worktrees", "skip-lanes", "not-a-repo"):
        directory = repo.parent / name
        directory.mkdir()
        if name.startswith("skip-"):
            (directory / ".git").mkdir()
    result = run()
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    result = run("--verbose")
    assert (result.returncode, result.stdout, result.stderr) == (0, "OK sample\n", "")


def test_roots_and_repos_repeat_and_deduplicate(repos):
    repo, _, _ = repos
    git(repo, "switch", "-c", "feature")
    result = run("--root", repo.parent, "--root", repo.parent,
                 "--repo", repo, "--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, "ALERT sample behind=1 reason=other-branch:feature\n", "")


def test_origin_head_selects_non_main_default(repos):
    repo, seed, _ = repos
    git(seed, "push", "origin", "HEAD:trunk")
    git(repo, "branch", "-m", "trunk")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    result = run("--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert git(repo, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")


def test_fetch_timeout_is_untouched(repos, tools_importable, monkeypatch, capsys):
    repo, _, _ = repos
    module = importlib.import_module("repo_sync_check")
    real_run = module.subprocess.run

    def timeout_fetch(command, **kwargs):
        if "fetch" in command:
            assert command[-3:] == ["fetch", "-q", "origin"]
            assert kwargs["timeout"] == 60
            raise subprocess.TimeoutExpired(command, 60)
        return real_run(command, **kwargs)

    before = state(repo)
    monkeypatch.setattr(module.subprocess, "run", timeout_fetch)
    assert module.main(["--repo", str(repo)]) == 1
    output = capsys.readouterr()
    assert (output.out, output.err) == ("ALERT sample behind=0 reason=fetch-failed\n", "")
    assert state(repo) == before


def test_missing_origin_head_falls_back_to_main(repos):
    repo, seed, _ = repos
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    result = run("--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert git(repo, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")


def test_zero_behind_is_silent_even_on_dirty_feature(repos):
    repo, _, _ = repos
    assert run("--repo", repo).returncode == 0
    git(repo, "switch", "-c", "feature")
    (repo / "tracked.txt").write_text("local modification\n")
    before = state(repo)
    result = run("--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert state(repo) == before


def test_alert_does_not_skip_remaining_repositories(repos):
    repo, seed, origin = repos
    second = repo.parent / "second"
    git(repo.parent, "clone", "--template=", str(origin), str(second))
    git(second, "switch", "--detach", "HEAD~1")
    before = state(second)
    result = run("--repo", second, "--repo", repo)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, "ALERT second behind=1 reason=detached\n", "")
    assert state(second) == before
    assert git(repo, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
