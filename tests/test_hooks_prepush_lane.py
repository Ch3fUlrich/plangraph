"""WRITER-PUSHES-EACH-ITEM-COMMIT / LE-FAST-CHECKS-RESULT-READ-FROM-TAIL: a lane/* push is scrub, commit gate and stamp only."""
import os
import stat
import time

import pytest

from test_hooks_prepush import ZERO, commit_file, fake_ip, git, repo, run_hook  # noqa: F401 (repo is a fixture)

BOUND_SECONDS = 20


def stub_plangraph(tmp_path, green):
    """A `plangraph` on PATH that answers the stamp question; the log shows what the hook asked."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    log = tmp_path / "plangraph.log"
    tool = directory / "plangraph"
    tool.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit {0 if green else 1}\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}, log


def lane_line(sha, remote=ZERO, name="pr10"):
    return f"refs/heads/lane/{name} {sha} refs/heads/lane/{name} {remote}"


def with_tripwire_preflight(r):
    """A pushed tree whose preflight would leave a mark: a lane push must never run it."""
    mark = r.parent / "preflight-ran"
    return commit_file(r, "tools/preflight.py",
                       "import os, pathlib\npathlib.Path(os.environ['PREFLIGHT_MARK']).write_text('ran')\n"), mark


def test_lane_push_with_green_stamp_runs_no_preflight_and_stays_fast(repo, tmp_path):
    r, _ = repo
    head, mark = with_tripwire_preflight(r)
    env, log = stub_plangraph(tmp_path, green=True)
    start = time.monotonic()
    res = run_hook(r, [lane_line(head)], {**env, "PREFLIGHT_MARK": str(mark)})
    elapsed = time.monotonic() - start
    assert res.returncode == 0, res.stderr
    assert not mark.exists(), "preflight ran for a lane push"
    assert f"fast-checks --stamp-green {head}" in log.read_text()
    assert elapsed < BOUND_SECONDS, f"lane push took {elapsed:.1f}s"
    assert git(r, "worktree", "list").count("\n") == 1, "the hook left a worktree"


def test_lane_push_without_a_green_stamp_is_refused(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    env, _ = stub_plangraph(tmp_path, green=False)
    res = run_hook(r, [lane_line(head)], env)
    assert res.returncode == 1
    assert "no green fast-checks stamp" in res.stderr


def test_unstamped_item_push_needs_the_explicit_flag(repo, tmp_path):
    r, _ = repo
    head = commit_file(r, "app/lane.txt", "clean\n")
    env, log = stub_plangraph(tmp_path, green=False)
    res = run_hook(r, [lane_line(head)], {**env, "PG_LANE_PUSH_UNSTAMPED": "1"})
    assert res.returncode == 0, res.stderr
    assert "without a fast-checks stamp" in res.stderr
    assert not log.exists()


def test_lane_push_still_scrubs_and_runs_the_commit_gate(repo, tmp_path):
    r, _ = repo
    env, _ = stub_plangraph(tmp_path, green=True)
    hit = commit_file(r, "app/lane.txt", f"ip = {fake_ip()}\n")
    assert run_hook(r, [lane_line(hit)], env).returncode == 1
    git(r, "reset", "-q", "--hard", "HEAD~1")
    git(r, "commit", "-q", "--allow-empty", "-m", "no trailers")
    bare = git(r, "rev-parse", "HEAD").strip()
    res = run_hook(r, [lane_line(bare)], env)
    assert res.returncode == 1
    assert "Plan-Item" in res.stderr or "Writer" in res.stderr


def test_two_lane_pushes_leave_every_worktree_of_the_clone_in_place(repo, tmp_path):
    r, base = repo
    other = tmp_path / "other"
    git(r, "worktree", "add", "-q", "--detach", str(other), base)
    head = commit_file(r, "app/lane.txt", "clean\n")
    env, _ = stub_plangraph(tmp_path, green=True)
    before = git(r, "worktree", "list", "--porcelain").count("worktree ")
    for name in ("one", "two"):
        assert run_hook(r, [lane_line(head, name=name)], env).returncode == 0
    assert git(r, "worktree", "list", "--porcelain").count("worktree ") == before
    assert other.is_dir()
