"""D-2458: discriminating writer-source and exemption regressions."""
from types import SimpleNamespace

import pytest

from evidence_fakes import FakeGit, make_ctx
from keeper_seat_helpers import (
    MERGE_SHA,
    SHA_A,
    SHA_B,
    _cov,
    merge_git,
    to_review_merge,
    to_review_with_commits,
    verdict_events,
    w,  # noqa: F401 - the review world fixture
    writer,
)
from keeper_util import seed, to_in_review
from plangraph.keeper import auth
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
            seen.append(("paths", sha))         # never consulted now
            return ["CHANGELOG.md"]

        def changelog_only_edit(self, repo, sha):
            seen.append(("proof", sha))         # the ONE source of truth
            assert sha == SHA_A                 # proven on the LEAF, not the merge
            return True

    git = LeafGit(branch_trailers_by_sha={MERGE_SHA: [
        {"Hand-Edit": "D-2458", "_writer_commit": SHA_A}, writer("gpt-5 run")]})
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_merge(w, key, git)
    assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["openai"]
    assert seen == [("proof", SHA_A)]


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


# --------------------------------------------------------------------------
# D-2458 HIGH: an ACCEPT seat never comes from a commit-writer family
# --------------------------------------------------------------------------

def test_first_high_accept_seat_from_a_commit_writer_family_is_refused(w):
    """The HIGH first seat runs no coverage check, so the family gate must.

    ``claimed_by=l1main``, current-epoch verified ``commit+ci`` evidence whose
    ``Writer:`` trailer is a Claude model, ``verdict(rAnthropic1, ok=True)``:
    the verdict is refused and no seat is consumed.
    """
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",),
                  review_tier="high")
    to_review_with_commits(w, key, {SHA_A: writer("claude-sonnet-4-5 run1")},
                           who="l1main")
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic1"), key, True, "first")
    assert exc.value.code == "same_family"
    item = w.store._item(key)
    assert item["state"] == "in_review"
    assert verdict_events(w, key) == []


def test_later_high_accept_seat_from_a_commit_writer_family_is_refused(w):
    """Same rule for a LATER seat: the exemption never covers ``ok=True``."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",),
                  review_tier="high")
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})  # xiaomi
    first = w.store.verdict(w.p("rAlibaba"), key, True, "one")
    assert first["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), key, True, "two")
    assert exc.value.code == "same_family"
    item = w.store._item(key)
    assert item["state"] == "in_review"
    assert len(verdict_events(w, key)) == 1


def test_change_request_still_passes_the_commit_tier_family_gate(w):
    """``ok=False`` is the ONLY thing the commit-tier exemption covers."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("claude-sonnet-4-5 run1")})
    result = w.store.verdict(w.p("rAnthropic1"), key, False, "needs work")
    assert result["state"] == "claimed"
    assert w.store._item(key)["state"] == "claimed"


def test_direct_call_and_verdict_agree_on_an_accept_seat(w):
    """Pattern sweep: no flag lets the verdict path diverge from a direct
    ``_reviewer_family_ok`` call for an ACCEPT seat (the defect was a caller
    flag that silenced the gate only on one of the two paths)."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("claude-sonnet-4-5 run1")})
    with pytest.raises(Refused) as direct:
        w.store._reviewer_family_ok(w.p("rAnthropic1"), w.store._item(key))
    assert direct.value.code == "same_family"
    with pytest.raises(Refused) as via_verdict:
        w.store.verdict(w.p("rAnthropic1"), key, True, "accept")
    assert via_verdict.value.code == "same_family"
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# D-2458 MEDIUM: the change-request allowance is not a commit-tier privilege
# --------------------------------------------------------------------------

@pytest.mark.parametrize("owner_family,delegate,writer_model,reviewer", [
    (None, None, "claude-haiku", "rAnthropic1"),   # writer_model tier
    ("anthropic", None, None, "rAnthropic1"),      # legacy claimant tier
    ("meta", "workerMeta", None, "rMeta"),         # delegate tier
])
def test_change_request_is_allowed_on_every_family_tier(
        w, owner_family, delegate, writer_model, reviewer):
    """The writer's own family may push the item back, on every tier ..."""
    token = auth.add_principal(w.conn, "rMeta", "reviewer", ["demo"],
                               family="meta")
    w.principals["rMeta"] = auth.authenticate(w.conn, token)
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    to_in_review(w, key, who="claimant")
    w.conn.execute(
        "UPDATE items SET claimed_by='legacy-owner', owner_family=?,"
        " writer_model=?, delegate=? WHERE key=?",
        (owner_family, writer_model, delegate, key))

    # ... while an ACCEPT seat from that same family stays refused.
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p(reviewer), key, True, "accept")
    assert exc.value.code == "same_family"
    assert w.store._item(key)["state"] == "in_review"

    result = w.store.verdict(w.p(reviewer), key, False, "needs work")
    assert result["state"] == "claimed"
    assert w.store._item(key)["state"] == "claimed"


# --------------------------------------------------------------------------
# D-2458 LOW: one source of truth for the CHANGELOG exemption
# --------------------------------------------------------------------------

def test_changelog_proof_alone_decides_the_hand_edit_exemption(w):
    """The adapter's own proof decides, whatever ``changed_paths`` says."""
    class ProofGit(FakeGit):
        def changed_paths(self, repo, sha):
            return ["src/example.py"]      # contradicting, and ignored

        def changelog_only_edit(self, repo, sha):
            return True

    git = ProofGit(trailers_by_sha={SHA_A: {"Hand-Edit": "D-2458"},
                                    SHA_B: writer("gpt-5 run1")})
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: {}, SHA_B: {}}, git=git)
    assert _cov().writer_families(
        w.store, w.store._item(key), w.store.ctx) == ["openai"]
    assert w.store.verdict(
        w.p("rAnthropic1"), key, True, "changelog")["state"] == "done"


def test_hand_edit_without_the_adapter_proof_counts_anthropic(w):
    """An adapter that cannot prove a changelog-only edit counts the hand."""
    class PathsGit(FakeGit):
        def changed_paths(self, repo, sha):
            return ["CHANGELOG.md"]        # not a proof; not consulted

    git = PathsGit(trailers_by_sha={SHA_A: {"Hand-Edit": "D-2458"}})
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: {}}, git=git)
    assert _cov().writer_families(
        w.store, w.store._item(key), w.store.ctx) == ["anthropic"]
