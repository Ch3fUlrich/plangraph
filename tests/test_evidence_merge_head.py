"""Merge-head evidence tests split out of test_evidence_adapters.py (file-size gate)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from git_helpers import git_cli, git_supports_init_branch
from plangraph.keeper.evidence_git import GitAdapter

#: `git init -b main` needs git >= 2.28; presence of a git binary is not enough.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not git_supports_init_branch(),
    reason="git >= 2.28 not available (git init -b)",
)


def test_git_adapter_merge_commit_of_lane_merge_head(tmp_path: Path) -> None:
    # D-3507: a lane-side "merge main into the lane" PR head (2 parents) sits
    # on the ^2 side of the PR merge; merge_commit_of must return that merge.
    repo = tmp_path
    _git, _git_init = git_cli(tmp_path)
    _git_init(repo, "-q", "-b", "main")
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


def test_merge_commit_of_fails_closed_on_failed_lookup(tmp_path: Path) -> None:
    # D-3538: a failed rev-list lookup for a merge sha must return None on its
    # own (fail closed) and never fall through to the non-merge proc_fp path.
    repo = tmp_path
    _git, _git_init = git_cli(tmp_path)
    _git_init(repo, "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")

    _git(repo, "commit", "--allow-empty", "-m", "c0")
    _git(repo, "switch", "-c", "f")
    _git(repo, "commit", "--allow-empty", "-m", "f1")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--no-ff", "-m", "Merge f", "f")
    merge = _git(repo, "rev-parse", "HEAD")

    adapter = GitAdapter()
    # sanity: the same merge resolves against its own ref
    assert adapter.merge_commit_of(repo, merge, "main") == (merge, "test@example.invalid")
    # the lookup against an unresolvable ref fails closed on a merge sha
    assert adapter.merge_commit_of(repo, merge, "no-such-ref") is None
    # an unknown sha never maps to a merge commit
    assert adapter.merge_commit_of(repo, "0" * 40, "main") is None
