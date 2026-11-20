"""Merge commits that edit a gate file themselves need a Gate-Edit trailer."""
from __future__ import annotations

import subprocess
from pathlib import Path

from test_ci_commit_gate import _commit, _git, _git_env, _head, check, repo  # noqa: F401


GATE_MSG = "chore: gate\n\nWriter: mimo-v2.6-flash run-g\n\nGate-Edit: D-2209\n"


def _conflicted_workflow_merge(repo: Path, message: str) -> tuple[str, str]:
    """A main-into-lane merge that resolves a conflict in a workflow file; returns (base, merge sha)."""
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: x\n"})
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: side\n"})
    _git(repo, "checkout", "-q", "-")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: lane\n"})
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", "side"], cwd=repo, env=_git_env(), capture_output=True)
    (repo / ".github/workflows/x.yml").write_text("name: resolved\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return base, _head(repo)


def test_merge_resolving_workflow_conflict_needs_gate_edit(repo: Path):
    base, merge = _conflicted_workflow_merge(repo, "Merge side into lane")
    assert check(base, merge, cwd=repo) == [f"{merge}: gate edit without Gate-Edit trailer"]


def test_merge_resolving_workflow_conflict_with_gate_edit_passes(repo: Path):
    base, merge = _conflicted_workflow_merge(repo, "Merge side into lane\n\nGate-Edit: D-6140\n")
    assert check(base, merge, cwd=repo) == []


def test_merge_resolving_workflow_conflict_can_be_exempt(repo: Path, tmp_path: Path):
    base, merge = _conflicted_workflow_merge(repo, "Merge side into lane")
    exempt = tmp_path / "exempt.txt"
    exempt.write_text(merge + "\n", encoding="utf-8")
    assert check(base, merge, cwd=repo, exempt=exempt) == []
