"""Merge-head evidence tests split out of test_evidence_adapters.py (file-size gate)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from plangraph.keeper.evidence_git import GitAdapter


def _git(repo: Path, *args: str) -> str:
    # Scrub ambient GIT_* env so the repo-local user.name/user.email decide the
    # committer identity (hermetic regardless of the invoking environment).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, env=env,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_git_adapter_merge_commit_of_lane_merge_head(tmp_path: Path) -> None:
    # D-3507: a lane-side "merge main into the lane" PR head (2 parents) sits
    # on the ^2 side of the PR merge; merge_commit_of must return that merge.
    repo = tmp_path
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "commit", "--allow-empty", "-m", "c0")
    _git(repo, "switch", "-c", "x")
    _git(repo, "commit", "--allow-empty", "-m", "x1")
    _git(repo, "switch", "main")
    _git(repo, "config", "user.email", "merger@example.invalid")
    _git(repo, "merge", "--no-ff", "-m", "direct merge", "x")
    direct = _git(repo, "rev-parse", "HEAD")  # direct-push merge on main
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "switch", "-c", "f")
    _git(repo, "commit", "--allow-empty", "-m", "x1f")
    _git(repo, "switch", "main")
    _git(repo, "commit", "--allow-empty", "-m", "m1")
    _git(repo, "switch", "f")
    _git(repo, "merge", "--no-ff", "-m", "merge main into f", "main")
    h = _git(repo, "rev-parse", "HEAD")  # 2 parents: x1f and m1
    _git(repo, "switch", "main")
    _git(repo, "commit", "--allow-empty", "-m", "m2")
    m2 = _git(repo, "rev-parse", "HEAD")  # reachable from main only via M^1
    _git(repo, "config", "user.email", "merger@example.invalid")
    _git(repo, "merge", "--no-ff", "-m", "Merge f", "f")
    m = _git(repo, "rev-parse", "HEAD")  # 2 parents: m2 and h
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "switch", "-c", "u")
    _git(repo, "commit", "--allow-empty", "-m", "u1")
    _git(repo, "switch", "-c", "v", m2)
    _git(repo, "commit", "--allow-empty", "-m", "v1")
    _git(repo, "switch", "u")
    _git(repo, "merge", "--no-ff", "-m", "Merge v into u", "v")
    unmerged = _git(repo, "rev-parse", "HEAD")  # merge commit never merged into main
    _git(repo, "switch", "main")

    adapter = GitAdapter()
    email = "merger@example.invalid"

    # lane-side "merge main into the lane" PR head maps to the PR merge
    assert adapter.merge_commit_of(repo, h, "main") == (m, email)
    # the PR merge commit maps to itself (same tuple shape as the test above)
    assert adapter.merge_commit_of(repo, m, "main") == (m, email)
    # direct-push merge (first-parent, no PR merge above it where it is ^2):
    # behaviour unchanged -- it maps to itself, as before this fix
    assert adapter.merge_commit_of(repo, direct, "main") == (direct, email)
    # head reachable only via M^1
    assert adapter.merge_commit_of(repo, m2, "main") is None
    # unmerged branch merge commit
    assert adapter.merge_commit_of(repo, unmerged, "main") is None
