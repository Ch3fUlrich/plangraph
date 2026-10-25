"""D-2458: discriminating writer-source and exemption regressions."""
from types import SimpleNamespace

import pytest

from test_keeper_seat_coverage import (
    w, seed, writer, SHA_A, SHA_B, MERGE_SHA, FakeGit, make_ctx,
    to_review_with_commits, to_review_merge, merge_git, _cov,
)
from plangraph.keeper.errors import Refused
from plangraph.keeper.evidence_git import GitAdapter


@pytest.mark.parametrize("case", ["merge_empty", "all_changelog"])
def test_empty_writer_set_fails_closed(w, case):
    class ChangelogGit(FakeGit):
        def changed_paths(self, repo, sha):
            return ["CHANGELOG.md"]

        def changelog_only_edit(self, repo, sha):
            return True

    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    if case == "merge_empty":
        to_review_merge(w, key, merge_git([]))
    else:
        trailers = {SHA_A: {"Hand-Edit": "D-2458"}, SHA_B: {"Hand-Edit": "D-2458"}}
        to_review_with_commits(w, key, trailers, git=ChangelogGit(trailers_by_sha=trailers))
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic1"), key, True)
    assert exc.value.code == "writer_unknown"
    assert w.store._item(key)["state"] == "in_review"


def test_current_verified_writer_ignores_spoof_rows(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("gpt-5 run")})
    item = w.store._item(key)
    rows = w.store._evidence(key)
    w.store._evidence = lambda key: rows + [dict(rows[0], epoch=0, ref={"commit": SHA_B}),
                                           dict(rows[0], verified=False, ref={"commit": SHA_B})]
    w.store.ctx = make_ctx(git=FakeGit(trailers_by_sha={SHA_A: writer("gpt-5 run"), SHA_B: writer("mimo-v2 spoof")}))
    assert _cov().writer_families(w.store, item, w.store.ctx) == ["openai"]
    w.store._reviewer_family_ok(w.p("rXiaomi"), item)


def test_adapter_without_changed_paths_counts_hand_edit(w):
    git = FakeGit(trailers_by_sha={SHA_A: {"Hand-Edit": "D-2458"}})
    git.changed_paths = None
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: {}}, git=git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["anthropic"]
    assert w.store.verdict(w.p("rGoogle"), key, True)["state"] == "done"


def test_hand_edit_leaf_paths_and_openai_writer_are_both_resolved(w):
    seen = []
    class LeafGit(FakeGit):
        def changed_paths(self, repo, sha):
            seen.append(sha)
            return ["CHANGELOG.md"]

        def changelog_only_edit(self, repo, sha):
            assert sha == SHA_A
            return True

    git = LeafGit(branch_trailers_by_sha={MERGE_SHA: [
        {"Hand-Edit": "D-2458", "_writer_commit": SHA_A}, writer("gpt-5 run")]})
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_merge(w, key, git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["openai"]
    assert seen == [SHA_A]


@pytest.mark.parametrize("parents,status,want", [(1, "M", True), (1, "A", True), (1, "D", False), (2, "M", False)])
def test_changelog_exemption_rejects_merges_and_deletions(monkeypatch, parents, status, want):
    git = GitAdapter()
    def run(repo, args):
        out = " ".join([SHA_A] * (parents + 1)) if args[0] == "rev-list" else status + "\0CHANGELOG.md\0"
        return SimpleNamespace(returncode=0, stdout=out)
    monkeypatch.setattr(git, "_run", run)
    assert git.changelog_only_edit("repo", SHA_A) is want


def test_system_without_writer_retains_writer_unknown(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    item = dict(w.store._item(key), claimed_by="system")
    with pytest.raises(Refused) as exc:
        w.store._reviewer_family_ok(w.p("rGoogle"), item)
    assert exc.value.code == "writer_unknown"


def test_unknown_model_codes_distinguish_evidence_from_metadata(w):
    """Preserve the specified metadata family_unknown / evidence writer_unknown API."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    item = dict(w.store._item(key), writer_model="unknown-model")
    with pytest.raises(Refused) as exc:
        w.store._reviewer_family_ok(w.p("rGoogle"), item)
    assert exc.value.code == "family_unknown"


@pytest.mark.parametrize("merge,deleted", [(True, False), (False, True)])
def test_coverage_counts_hand_edit_merge_or_deleted_changelog(w, monkeypatch, merge, deleted):
    adapter = GitAdapter()
    def run(repo, args):
        output = " ".join([SHA_A] * (3 if merge else 2)) if args[0] == "rev-list" else "D\0CHANGELOG.md\0"
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr(adapter, "_run", run)
    git = FakeGit(trailers_by_sha={SHA_A: {"Hand-Edit": "D-2458", "_writer_commit": SHA_A}})
    git.changed_paths = lambda *args: ["CHANGELOG.md"]
    git.changelog_only_edit = adapter.changelog_only_edit
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: {}}, git=git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["anthropic"]
