"""Phase merge proof uses real git, including both parents and trailer parsing."""
import subprocess

import pytest

from evidence_fakes import FakeCI, make_ctx
from plangraph.keeper.evidence import CommitCiVerifier
from plangraph.keeper.evidence_git import GitAdapter, parse_trailers
from plangraph.planner import phase_finish


def git(root, *args, message=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=keeper", "-c", "user.email=keeper@example.invalid",
         *args], cwd=root, input=message, text=True).strip()


def merge_commit(root, head):
    """Create a real merge object with the phase tip as its second parent."""
    tree = git(root, "rev-parse", f"{head}^{{tree}}")
    base = git(root, "rev-parse", f"{head}^")
    return git(root, "commit-tree", tree, "-p", base, "-p", head,
               message="Merge phase demo:PH-X\n\nPlan-Item: demo:C1\nPlan-Item: demo:C2\n")


@pytest.mark.parametrize("key,ok", [("demo:C1", True), ("demo:C2", True),
                                        ("demo:C3", False)])
def test_real_merge_verifies_each_member_only(tmp_path, key, ok):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "commit", "--allow-empty", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "phase/demo-PH-X")
    git(tmp_path, "commit", "--allow-empty", "-m", "phase")
    head = git(tmp_path, "rev-parse", "HEAD")
    merge = merge_commit(tmp_path, head)
    git(tmp_path, "update-ref", "refs/heads/main", merge)
    ctx = make_ctx(git=GitAdapter({"demo": str(tmp_path)}), ci=FakeCI(mode="none"))
    result = CommitCiVerifier().verify(
        {"key": key, "id": key.split(":")[1], "project": "demo"},
        {"commit": merge}, ctx)
    assert result.ok is ok, result.detail


def test_finish_passes_sorted_deduplicated_card_trailers(tmp_path):
    class GitHub:
        def find_pr(self, branch):
            return {"number": 7, "state": "open"}

        def push_branch(self, *args):
            pass

        def ci_green(self, head):
            return True

        def merge_pr(self, number, head, *, commit_message=None):
            self.message = commit_message
            return {"number": number, "url": "u", "state": "merged"}

    gh = GitHub()
    phase_finish._finish_locked("demo:PH-X", "phase/demo-PH-X", "a" * 40,
                               tmp_path, gh, "main", ["demo:C2", "demo:C1", "demo:C2"])
    assert gh.message == "Merge phase demo:PH-X\n\nPlan-Item: demo:C1\nPlan-Item: demo:C2\n"
    assert parse_trailers(gh.message) == {"Plan-Item": ["demo:C1", "demo:C2"]}
