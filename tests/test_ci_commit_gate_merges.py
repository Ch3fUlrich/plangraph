"""Merge commits that edit a gate file themselves need a Gate-Edit trailer."""
from __future__ import annotations

import subprocess
from pathlib import Path

from test_ci_commit_gate import BASE_MSG, OK_MSG, _commit, _git, _git_env, _head, check, repo  # noqa: F401

BASE_MSG_SEED = BASE_MSG
OK_LANE_MSG = OK_MSG


GATE_MSG = "chore: gate\n\nWriter: mimo-v2.6-flash run-g\n\nGate-Edit: D-2209\n"


def _conflicted_workflow_merge(repo: Path, message: str, keep: str | None = None) -> tuple[str, str]:
    """A main-into-lane merge that resolves a conflict in a workflow file; returns (base, merge sha)."""
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: x\n"})
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: side\n"})
    _git(repo, "checkout", "-q", "-")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: lane\n"})
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", "side"], cwd=repo, env=_git_env(), capture_output=True)
    if keep:  # resolve by keeping one parent's version whole: the combined diff then shows nothing
        _git(repo, "checkout", f"--{keep}", ".github/workflows/x.yml")
    else:
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


def test_conflict_resolved_by_keeping_one_side_still_needs_gate_edit(repo: Path):
    from importlib import util

    for keep in ("ours", "theirs"):
        sub = repo.parent / keep
        sub.mkdir()
        _git(sub, "init", "-q")
        _commit(sub, BASE_MSG_SEED)
        base, merge = _conflicted_workflow_merge(sub, "Merge side into lane", keep=keep)
        # The resolution equals a parent, so the combined diff alone would not list the workflow.
        assert ".github/workflows/x.yml" not in _git(sub, "diff-tree", "--cc", "--name-only", "-r", merge)
        assert check(base, merge, cwd=sub) == [f"{merge}: gate edit without Gate-Edit trailer"]
        # The batch land reads the same detection.
        path = Path(__file__).resolve().parents[1] / "tools/gate_wiring_check.py"
        spec = util.spec_from_file_location("gate_wiring_check", path)
        gate = util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        assert gate.commits_missing_gate_edit(
            base, merge, ".github/workflows/x.yml", cwd=sub, include_merges=True) == [merge]


def test_clean_main_merge_of_a_workflow_edit_needs_no_trailer(repo: Path):
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: x\n"})
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": "name: side\n"})
    _git(repo, "checkout", "-q", "-")
    _commit(repo, OK_LANE_MSG, files={"lane.txt": "lane\n"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    assert check(base, _head(repo), cwd=repo) == []


SEVEN = "".join(f"line{n}\n" for n in range(1, 8))


def _disjoint_edit_merge(repo: Path, message: str = "Merge side", extra: str | None = None) -> tuple[str, str]:
    """Both sides edit different hunks of one workflow (each edit authorized): git merges it cleanly."""
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": SEVEN})
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": SEVEN.replace("line1", "side1")})
    _git(repo, "checkout", "-q", "-")
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": SEVEN.replace("line7", "lane7")})
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", "side"], cwd=repo, env=_git_env(), capture_output=True)
    if extra is not None:
        (repo / ".github/workflows/x.yml").write_text(extra, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return base, _head(repo)


def test_clean_merge_of_disjoint_authorized_workflow_edits_needs_no_trailer(repo: Path):
    base, merge = _disjoint_edit_merge(repo)
    # The combined diff lists the file (it differs from both parents) although git merged it alone.
    assert ".github/workflows/x.yml" in _git(repo, "diff-tree", "--cc", "--name-only", "-r", merge)
    assert check(base, merge, cwd=repo) == []


def test_manual_edit_on_top_of_a_clean_merge_needs_a_trailer(repo: Path):
    merged = SEVEN.replace("line1", "side1").replace("line7", "lane7")
    base, merge = _disjoint_edit_merge(repo, extra=merged + "sneaked: in\n")
    assert check(base, merge, cwd=repo) == [f"{merge}: gate edit without Gate-Edit trailer"]


def _rename_conflict_merge(tmp_path: Path) -> tuple[Path, str, str]:
    """Both sides rename one workflow differently; the merge keeps main's side whole."""
    repo = tmp_path / "rename"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, BASE_MSG)
    _commit(repo, GATE_MSG, files={".github/workflows/x.yml": SEVEN})
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "mv", ".github/workflows/x.yml", ".github/workflows/y.yml")
    _git(repo, "commit", "-q", "-m", GATE_MSG)
    _git(repo, "checkout", "-q", "-")
    _git(repo, "mv", ".github/workflows/x.yml", ".github/workflows/z.yml")
    _git(repo, "commit", "-q", "-m", GATE_MSG)
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", "side"], cwd=repo, env=_git_env(), capture_output=True)
    # Keep main's side whole: y.yml present, z.yml gone. The combined diff then lists nothing.
    _git(repo, "checkout", "--theirs", ".github/workflows/y.yml")
    _git(repo, "rm", "-q", "-f", ".github/workflows/z.yml")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Merge side")
    return repo, base, _head(repo)


def test_without_merge_tree_a_rename_conflict_still_needs_a_trailer(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ci_commit_gate_module(), "_auto_merge", lambda parents, cwd: None)
    repo, base, head = _rename_conflict_merge(tmp_path)
    assert check(base, head, cwd=repo) == [f"{head}: gate edit without Gate-Edit trailer"]


def test_workflow_gate_reads_the_shared_detector_for_a_rename_conflict(tmp_path: Path, tools_importable):
    from merge_batch_util import MergeBatchError, workflow_gate

    repo, base, head = _rename_conflict_merge(tmp_path)
    assert ".github/workflows" not in _git(repo, "diff-tree", "--cc", "--name-only", "-r", head)
    # A base that already holds both parents leaves the merge as the only commit in range, so only
    # the shared conflict detector can name the workflow paths it touched.
    p1, p2 = (_git(repo, "rev-parse", f"{head}^{n}").strip() for n in (1, 2))
    tree = _git(repo, "rev-parse", f"{p1}^{{tree}}").strip()
    base = _git(repo, "commit-tree", tree, "-p", p1, "-p", p2, "-m", "base holding both parents").strip()
    try:
        workflow_gate(repo, base, head)
    except MergeBatchError as exc:
        assert "Gate-Edit" in str(exc) and head in str(exc)
    else:
        raise AssertionError("a rename conflict resolved by keeping one side passed the gate")


def test_without_merge_tree_a_kept_side_still_needs_a_trailer(repo: Path, monkeypatch):
    # A git lacking `merge-tree --write-tree`: the fallback must stay at least as strict as before.
    monkeypatch.setattr(ci_commit_gate_module(), "_auto_merge", lambda parents, cwd: None)
    for keep in ("ours", "theirs"):
        sub = repo.parent / f"fallback-{keep}"
        sub.mkdir()
        _git(sub, "init", "-q")
        _commit(sub, BASE_MSG_SEED)
        base, merge = _conflicted_workflow_merge(sub, "Merge side into lane", keep=keep)
        assert check(base, merge, cwd=sub) == [f"{merge}: gate edit without Gate-Edit trailer"]


def ci_commit_gate_module():
    from test_ci_commit_gate import ci_commit_gate

    return ci_commit_gate


def test_without_merge_tree_a_rename_into_the_workflow_directory_needs_a_trailer(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ci_commit_gate_module(), "_auto_merge", lambda parents, cwd: None)
    repo = tmp_path / "into"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, BASE_MSG)
    _commit(repo, OK_LANE_MSG, files={"ci/x.yml": SEVEN})
    base = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / ".github/workflows").mkdir(parents=True)
    _git(repo, "mv", "ci/x.yml", ".github/workflows/y.yml")
    _git(repo, "commit", "-q", "-m", GATE_MSG)
    _git(repo, "checkout", "-q", "-")
    (repo / ".github/workflows").mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "ci/x.yml", ".github/workflows/z.yml")
    _git(repo, "commit", "-q", "-m", GATE_MSG)
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", "side"], cwd=repo, env=_git_env(), capture_output=True)
    _git(repo, "checkout", "--theirs", ".github/workflows/y.yml")
    _git(repo, "rm", "-q", "-f", ".github/workflows/z.yml")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Merge side")
    head = _head(repo)
    assert check(base, head, cwd=repo) == [f"{head}: gate edit without Gate-Edit trailer"]


def test_a_fatal_merge_tree_error_is_not_an_unsupported_fallback(repo: Path):
    import pytest

    module = ci_commit_gate_module()
    with pytest.raises(module.GateError, match="merge-tree failed"):
        module._auto_merge(["0" * 40, "1" * 40], repo)
