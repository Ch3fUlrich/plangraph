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


def verify_ci(root, merge, key, runs):
    ctx = make_ctx(git=GitAdapter({"demo": str(root)}), ci=FakeCI(mode="owner/repo", runs=runs))
    return CommitCiVerifier().verify(
        {"key": key, "id": key.split(":")[1], "project": "demo"}, {"commit": merge}, ctx), ctx.ci


def test_a_merge_without_trailers_needs_green_ci_on_the_merge_sha_itself(tmp_path):
    merge = merged(tmp_path, "work\n\nPlan-Item: demo:A\n", "Merge pull request #8")
    green = [{"name": "build", "head_sha": merge, "conclusion": "success"}]
    result, ci = verify_ci(tmp_path, merge, "demo:A", green)
    assert result.ok and ci.calls == [("owner/repo", merge)]
    red = [{"name": "build", "head_sha": merge, "conclusion": "failure"}]
    assert not verify_ci(tmp_path, merge, "demo:A", red)[0].ok
    other = [{"name": "build", "head_sha": "a" * 40, "conclusion": "success"}]
    assert "another sha" in verify_ci(tmp_path, merge, "demo:A", other)[0].detail


def test_only_the_commits_a_merge_introduced_bind_and_nested_merges_count(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "commit", "--allow-empty", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "inner")
    git(tmp_path, "commit", "--allow-empty", "-q", "-m", "inner work")
    git(tmp_path, "checkout", "-q", "-b", "outer", "main")
    git(tmp_path, "merge", "--no-ff", "-q", "-m", "Merge inner\n\nPlan-Item: demo:INNER\n", "inner")
    git(tmp_path, "checkout", "-q", "main")
    git(tmp_path, "merge", "--no-ff", "-q", "-m", "Merge outer", "outer")
    outer = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "--allow-empty", "-q", "-m", "later\n\nPlan-Item: demo:LATER\n")
    assert verify(tmp_path, outer, "demo:INNER").ok          # the nested merge's trailer binds the outer merge
    assert not verify(tmp_path, outer, "demo:LATER").ok      # a later commit outside the range does not
    inner = git(tmp_path, "rev-parse", f"{outer}^2")
    assert not verify(tmp_path, inner, "demo:LATER").ok      # nor does it bind the inner merge


REAL_SHAPE = (
    "Keeper refuses a new lane without done criteria\n\n"
    "create_items refuses kind lane with a blank done_criteria for every principal.\n\n"
    "Plan-Item: demo:A\n"
    "Writer: claude-sonnet-5-5 session_011g7R2JfBWCfYiFimg4hcfN (Claude Code writer child, batch batch8)\n\n"
    "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>\n")


def test_plan_item_trailers_are_read_when_a_later_trailer_paragraph_follows():
    from plangraph.keeper.evidence_git import parse_trailers, plan_items, trailer_lines

    assert plan_items(REAL_SHAPE) == ["demo:A"]
    assert parse_trailers("\n".join(trailer_lines(REAL_SHAPE)))["Plan-Item"] == "demo:A"
    assert plan_items("subject\n\nPlan-Item: demo:A\nWriter: x y\n") == ["demo:A"]


@pytest.mark.parametrize("message", [
    "Plan-Item: demo:A\n",                                    # the subject is never a trailer
    "subject\n\nSee Plan-Item: demo:A in the notes.\n",       # a mention in prose
    "subject\n\nPlan-Item: demo:A and more words\nand prose that is no trailer\n",
    "subject\n\nPlan-Item: demo:A\n\nclosing prose paragraph\n"])  # a prose paragraph ends the block
def test_a_plan_item_outside_a_trailer_paragraph_is_not_read(message):
    from plangraph.keeper.evidence_git import plan_items

    assert plan_items(message) == []


def test_the_real_shape_verifies_on_the_commit_and_on_the_merge_of_its_branch(tmp_path):
    merge = merged(tmp_path, REAL_SHAPE, "Merge pull request #1080 from x/pr")
    assert verify(tmp_path, merge, "demo:A").ok
    assert verify(tmp_path, git(tmp_path, "rev-parse", f"{merge}^2"), "demo:A").ok
    assert not verify(tmp_path, merge, "demo:B").ok


def test_git_trailer_shapes_are_read_as_git_reads_them():
    from plangraph.keeper.evidence_git import plan_items

    assert plan_items("subject\n\nPlan-Item:demo:C\n") == ["demo:C"]                # no space after the colon
    assert plan_items("subject\n\nWriter: x\n  Plan-Item: demo:C\n") == []           # a continuation is no trailer
    assert plan_items("subject\n\nPlan-Item: demo:A\n  continued value\n") == ["demo:A continued value"]


def test_a_continued_or_indented_plan_item_never_binds_as_commit_ci_evidence(tmp_path):
    for message in ("work\n\nWriter: x y\n  Plan-Item: demo:A\n", "work\n\nPlan-Item: demo:A\n  and more words\n"):
        git(tmp_path, "init", "-q", "-b", "main")
        git(tmp_path, "commit", "--allow-empty", "-q", "-m", message)
        commit = git(tmp_path, "rev-parse", "HEAD")
        ctx = make_ctx(git=GitAdapter({"demo": str(tmp_path)}), ci=FakeCI(mode="none"))
        assert not CommitCiVerifier().verify({"key": "demo:A", "id": "A", "project": "demo"},
                                             {"commit": commit}, ctx).ok
        git(tmp_path, "update-ref", "-d", "refs/heads/main")
        for sub in (tmp_path / ".git").glob("refs/heads/*"):
            sub.unlink()
