"""LE-MERGE-COMMIT-NO-PLAN-ITEM-TRAILER: commit+ci on a merge whose branch range carries the trailer."""
import subprocess

import pytest

from evidence_fakes import FakeCI, make_ctx
from plangraph.keeper.evidence import CommitCiVerifier
from plangraph.keeper.evidence_git import GitAdapter


def git(root, *args, message=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=keeper", "-c", "user.email=keeper@example.invalid",
         *args], cwd=root, input=message, text=True).strip()


def merged(root, branch_message, merge_message):
    """A main with a merge of one branch commit; returns the merge sha."""
    git(root, "init", "-q", "-b", "main")
    git(root, "commit", "--allow-empty", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "batch")
    git(root, "commit", "--allow-empty", "-q", "-m", branch_message)
    head = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", f"{head}^{{tree}}")
    merge = git(root, "commit-tree", tree, "-p", git(root, "rev-parse", "main"), "-p", head,
                message=merge_message)
    git(root, "update-ref", "refs/heads/main", merge)
    return merge


def verify(root, merge, key):
    ctx = make_ctx(git=GitAdapter({"demo": str(root)}), ci=FakeCI(mode="none"))
    return CommitCiVerifier().verify(
        {"key": key, "id": key.split(":")[1], "project": "demo"}, {"commit": merge}, ctx)


def test_a_merge_commit_with_the_trailer_of_every_item_verifies_each(tmp_path):
    merge = merged(tmp_path, "work", "Merge pull request #7\n\nPlan-Item: demo:A\nPlan-Item: demo:B\n")
    assert [verify(tmp_path, merge, f"demo:{k}").ok for k in "AB"] == [True, True]


@pytest.mark.parametrize("key,ok", [("demo:A", True), ("demo:B", True), ("demo:C", False)])
def test_a_merge_without_trailers_verifies_when_its_branch_range_carries_them(tmp_path, key, ok):
    merge = merged(tmp_path, "work\n\nPlan-Item: demo:A\nPlan-Item: demo:B\n", "Merge pull request #8")
    result = verify(tmp_path, merge, key)
    assert result.ok is ok, result.detail
    if not ok:
        assert f"does not carry trailer 'Plan-Item: {key}'" in result.detail


def test_a_commit_with_neither_is_refused_with_the_named_reason(tmp_path):
    merge = merged(tmp_path, "work", "Merge pull request #9")
    result = verify(tmp_path, merge, "demo:A")
    assert result.ok is False
    assert result.detail == f"commit {merge} does not carry trailer 'Plan-Item: demo:A'"


def test_every_branch_commit_of_a_merge_binds_its_own_item(tmp_path):
    """The shape of a hosted merge: no trailers on the merge, one key per branch commit."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "commit", "--allow-empty", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    git(tmp_path, "commit", "--allow-empty", "-q", "-m", "one\n\nPlan-Item: demo:A\n")
    git(tmp_path, "commit", "--allow-empty", "-q", "-m", "two\n\nPlan-Item: demo:B\n")
    git(tmp_path, "checkout", "-q", "main")
    git(tmp_path, "merge", "--no-ff", "-q", "-m", "Merge pull request #1080 from x/pr", "pr")
    merge = git(tmp_path, "rev-parse", "HEAD")
    assert [verify(tmp_path, merge, f"demo:{k}").ok for k in "ABC"] == [True, True, False]
