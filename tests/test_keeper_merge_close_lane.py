"""Merge-close refusals: one repository under several names, and pre-PR lane merges."""
import pytest

from keeper_util import to_claimed
from plangraph.keeper.errors import Refused

from plangraph.keeper.evidence_git import GitAdapter
from test_keeper_merge_close_auto import auto, env, git, set_states, state  # noqa: F401 - env is a fixture


def test_one_repository_registered_under_two_names_is_one_candidate(env):
    """LE-MERGE-CLOSE-SAME-REPO-REGISTERED-TWICE: two names for one path never read as ambiguous."""
    set_states(env)
    alias = env.repo.root.parent / "alias"
    alias.symlink_to(env.repo.root)
    env.store.ctx["git"] = GitAdapter({"demo": str(env.repo.root), "alias": str(alias)})
    env.repo.land("w1", "Plan-Item: demo:C", 400)
    env.store.require_seats = False
    result = auto(env)
    assert [c["key"] for c in result["closed"]] == ["demo:C"] and result["listed"] == []
    # the item's own project name wins over an earlier-sorted alias
    item = env.store._item("demo:C")
    assert env.store._scan_projects(item) == ["demo"]
    assert env.store._scan_projects(dict(item, project="alias")) == ["alias"]


def test_one_repository_under_two_names_is_one_candidate_for_a_named_close(env):
    set_states(env)
    alias = env.repo.root.parent / "alias"
    alias.symlink_to(env.repo.root)
    env.store.ctx["git"] = GitAdapter({"demo": str(env.repo.root), "alias": str(alias)})
    env.repo.land("w1", "Plan-Item: demo:C", 400)
    item = env.store._item("demo:C")
    item = dict(item, project="other")
    tagged = env.store._with_merge_repo(dict(item, project="other"))
    assert tagged["_merge_project"] == "alias"  # first sorted name; one candidate, no refusal


def lane_merge(env, rows, key="demo:C", did="D-123"):
    """A key that reached main through ``Merge lane/x into main (D-123 ...)``, with the decision rows given."""
    set_states(env)
    env.repo.land("x", f"Plan-Item: {key}", 0, subject=f"Merge lane/x into main ({did} fixture lane)")
    merge = git(env.repo.root, "rev-parse", "HEAD")
    path = env.repo.root.parent / "DECISIONS.md"
    path.write_text("".join(f"| {d} | demo | {text.format(sha=merge)} |\n" for d, text in rows), encoding="utf-8")
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    env.store.ctx["decisions"] = DecisionsAdapter(path)
    env.store.require_seats = False
    return merge


def test_a_pre_pr_lane_merge_closes_on_the_decision_row_naming_its_sha(env):
    """LE-PRE-PR-LANE-MERGES-READ-MERGED-PR-MISSING: the merge subject's D-row names the merge sha and the key."""
    merge = lane_merge(env, [("D-123", "Merged demo:C at {sha}")])
    result = auto(env)
    assert [c["key"] for c in result["closed"]] == ["demo:C"] and result["listed"] == []
    assert state(env, "demo:C")["state"] == "done"
    proof = env.store._evidence("demo:C")[-1]["ref"]["prs"][0]
    assert (proof["kind"], proof["decision_id"], proof["merge_sha"], proof["pr"]) == ("lane_merge", "D-123", merge, None)


@pytest.mark.parametrize("rows", [
    [],                                                       # the named row does not exist
    [("D-123", "Merged demo:C at 0123456789abcdef0123456789abcdef01234567")],   # row names another sha
    [("D-123", "Merged demo:C")],                             # row names no sha
    [("D-123", "Merged demo:B at {sha}")],                    # row names another key
    [("D-999", "Merged demo:C at {sha}")],                    # the subject names another decision
])
def test_a_pre_pr_lane_merge_without_a_vouching_row_is_listed_as_lane_merge_unproven(env, rows):
    lane_merge(env, rows)
    result = auto(env)
    assert result["closed"] == []
    assert "lane_merge_unproven" in result["listed"][0]["reason"]
    assert state(env, "demo:C")["state"] == "in_review"


def test_a_merge_without_pr_or_decision_in_its_subject_still_reads_merged_pr_missing(env):
    set_states(env)
    env.repo.land("x", "Plan-Item: demo:C", 0, subject="Merge lane/x into main")
    result = auto(env, require_seats=False)
    assert "merged_pr_missing" in result["listed"][0]["reason"]


def test_a_named_close_from_open_takes_a_pre_pr_lane_merge(env):
    """The full-history named close (from_open) reads the same lane_merge proof as the automatic one."""
    set_states(env)
    to_claimed(env, "demo:A")
    env.repo.land("x", "Plan-Item: demo:A", 0, subject="Merge lane/x into main (D-123 fixture lane)")
    merge = git(env.repo.root, "rev-parse", "HEAD")
    lane_head = git(env.repo.root, "rev-parse", "HEAD^2")
    path = env.repo.root.parent / "DECISIONS.md"
    path.write_text(f"| D-123 | demo | GO {lane_head} demo:A merged at {merge} |\n", encoding="utf-8")
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    env.store.ctx["decisions"] = DecisionsAdapter(path)
    result = env.store.close_merged(env.p("router"), "demo:A", "D-123", from_open=True, apply=True,
                                    require_seats=False)
    assert result["state"] == "done"
    proof = env.store._evidence("demo:A")[-1]["ref"]["prs"][0]
    assert (proof["kind"], proof["pr"], proof["merge_sha"], proof["head"]) == ("lane_merge", None, merge, lane_head)


def test_a_named_close_from_open_refuses_a_lane_merge_the_row_does_not_name(env):
    set_states(env)
    to_claimed(env, "demo:A")
    env.repo.land("x", "Plan-Item: demo:A", 0, subject="Merge lane/x into main (D-123 fixture lane)")
    lane_head = git(env.repo.root, "rev-parse", "HEAD^2")
    path = env.repo.root.parent / "DECISIONS.md"
    path.write_text(f"| D-123 | demo | GO {lane_head} demo:A |\n", encoding="utf-8")
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    env.store.ctx["decisions"] = DecisionsAdapter(path)
    with pytest.raises(Refused, match="lane_merge_unproven"):
        env.store.close_merged(env.p("router"), "demo:A", "D-123", from_open=True, apply=True,
                               require_seats=False)


def frozen_close(env):
    """What the API does: warm the scan outside the lock, then close frozen."""
    env.store.warm_merge_scan(env.store.merge_candidates())
    return env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)


def test_a_frozen_close_reads_the_lane_merge_row_from_the_warm_snapshot(env, monkeypatch):
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    lane_merge(env, [("D-123", "Merged demo:C at {sha}")])
    env.store.warm_merge_scan(env.store.merge_candidates())

    def forbidden(self, *args, **kwargs):
        pytest.fail("a frozen close never reads the decisions document")
    monkeypatch.setattr(DecisionsAdapter, "_read_document", forbidden)
    result = env.store.close_merged_auto(env.p("system"), require_seats=False, frozen=True)
    assert [c["key"] for c in result["closed"]] == ["demo:C"]


def test_a_frozen_close_without_a_captured_row_fails_closed(env):
    lane_merge(env, [("D-123", "Merged demo:C at {sha}")])
    env.store.ctx["decisions"]._path.write_text("", encoding="utf-8")  # unreadable or empty at warm time
    result = frozen_close(env)
    assert result["closed"] == [] and "lane_merge_unproven" in result["listed"][0]["reason"]
    assert state(env, "demo:C")["state"] == "in_review"


def test_a_non_frozen_close_reads_the_decisions_document_once_per_proof(env, monkeypatch):
    from plangraph.keeper.evidence_decisions import DecisionsAdapter
    lane_merge(env, [("D-123", "Merged demo:C at {sha}")])
    reads = []
    real = DecisionsAdapter._read_document
    monkeypatch.setattr(DecisionsAdapter, "_read_document",
                        lambda self, **kw: reads.append(1) or real(self, **kw))
    env.store._lane_merge_decision(state(env, "demo:C"), {
        "sha": git(env.repo.root, "rev-parse", "HEAD"), "subject": "Merge lane/x into main (D-123 fixture lane)"}, 0)
    assert len(reads) == 1
