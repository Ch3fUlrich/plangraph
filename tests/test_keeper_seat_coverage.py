"""SEAT-COVERAGE-KEEPER: the keeper refuses to CLOSE an item whose writer
families are not covered by the posted reviewer families.

The checker mirrors ``tools/seat_verdict.coverage`` and lives in
``plangraph.keeper.coverage``; it runs inside the verdict write lock only on
the verdict that would close the item (the sole standard verdict, the second
high one).  The adapters are fakes, so no test touches git, the network or the
filesystem.
"""
from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

import pytest

from evidence_fakes import FakeDecisions, FakeGit, make_ctx
from keeper_util import World, seed, to_in_review
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused
from plangraph.review import family as review_family

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import seat_verdict as sv  # noqa: E402

SHA_A = "a" * 40
SHA_B = "b" * 40
DECISION = "D-2217"

# name, role, family -- real family names, all on project demo.
EXTRA_PRINCIPALS = [
    ("claimant", "l1", "meta"),
    ("workerMeta", "worker", "meta"),
    ("rAnthropic1", "reviewer", "anthropic"),
    ("rAnthropic2", "reviewer", "anthropic"),
    ("rAlibaba", "reviewer", "alibaba"),
    ("rXiaomi", "reviewer", "xiaomi"),
    ("rDeepseek", "reviewer", "deepseek"),
    ("rGoogle", "reviewer", "google"),
]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    for name, role, family in EXTRA_PRINCIPALS:
        token = auth.add_principal(world.conn, name, role, ["demo"],
                                   family=family)
        world.tokens[name] = token
        world.principals[name] = auth.authenticate(world.conn, token)
    yield world
    world.close()


def _cov():
    """The module under test; imported here so the file collects while red."""
    from plangraph.keeper import coverage
    return coverage


def writer(value):
    """A ``Writer:`` trailer value (``<model-id> <run id>``)."""
    return {"Writer": value}


def git_with(trailers_by_sha):
    return FakeGit(trailers_by_sha=trailers_by_sha)


class RaisingGit(FakeGit):
    """``git show`` fails for every commit: the check must fail closed."""

    def trailers(self, repo, sha):
        raise RuntimeError("git show failed")


def to_review_with_commits(w, key, trailers_by_sha, *, git=None):
    """Claim as ``claimant``, attach one commit+ci row per sha, and submit."""
    w.store.ctx = make_ctx(git=git or git_with(trailers_by_sha))
    epoch = w.store.claim(w.p("claimant"), key, lease_s=7200)["epoch"]
    for sha in trailers_by_sha:
        out = w.store.add_evidence(w.p("claimant"), key, epoch, "commit+ci",
                                   {"commit": sha})
        assert out["verified"] is True
    w.store.submit(w.p("claimant"), key, epoch)
    assert w.store._item(key)["state"] == "in_review"
    return epoch


def verdict_events(w, key):
    return w.conn.execute(
        "SELECT payload FROM events WHERE key=? AND type='review_verdict'"
         " ORDER BY seq", (key,)).fetchall()


@pytest.mark.parametrize("model,code", [
    ("mimo-v2", None), ("claude-haiku", "same_family"),
    ("unknown-model", "writer_unknown"),
])
def test_anthropic_claimant_uses_evidence_writer(w, model, code):
    token = auth.add_principal(w.conn, "anthropicL1", "l1", ["demo"], family="anthropic")
    w.principals["claimant"] = auth.authenticate(w.conn, token)
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(model + " run1")})
    if code:
        with pytest.raises(Refused) as exc:
            w.store.verdict(w.p("rAnthropic1"), key, True)
        assert exc.value.code == code
    else:
        assert w.store.verdict(w.p("rAnthropic1"), key, True)["state"] == "done"


@pytest.mark.parametrize("writer_model,expected", [(None, "same_family"),
                                                   ("mimo-v2", None),
                                                   ("unknown-model", "family_unknown")])
def test_no_commit_writer_model_then_legacy_fallback(w, writer_model, expected):
    item = {"key": "demo:A", "project": "demo", "epoch": 1,
            "claimed_by": "legacy-owner", "owner_family": "anthropic",
            "delegate": None, "writer_model": writer_model}
    if expected:
        with pytest.raises(Refused) as exc:
            w.store._reviewer_family_ok(w.p("rAnthropic1"), item, w.store.ctx)
        assert exc.value.code == expected
    else:
        w.store._reviewer_family_ok(w.p("rAnthropic1"), item, w.store.ctx)


def test_old_and_unverified_evidence_cannot_spoof_writer(w):
    item = {"key": "demo:A", "project": "demo", "epoch": 2,
            "claimed_by": "legacy-owner", "owner_family": "anthropic", "delegate": None}
    w.store._evidence = lambda key: [
        {"epoch": epoch, "verified": verified, "kind": "commit+ci",
         "ref": {"commit": SHA_A}} for epoch, verified in [(1, True), (2, False)]]
    with pytest.raises(Refused) as exc:
        w.store._reviewer_family_ok(w.p("rAnthropic1"), item, make_ctx(git=FakeGit()))
    assert exc.value.code == "same_family"


def test_delegate_cannot_override_verified_writer(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("claude-haiku run1")})
    item = dict(w.store._item(key), delegate="workerMeta")
    with pytest.raises(Refused) as exc:
        w.store._reviewer_family_ok(w.p("rAnthropic1"), item, w.store.ctx)
    assert exc.value.code == "same_family"


# --------------------------------------------------------------------------
# 1. mixed-writer HIGH item: every writer family must be covered
# --------------------------------------------------------------------------

def test_high_mixed_writers_close_only_when_every_family_is_covered(w):
    covered, refused = seed(
        w, [("A", []), ("B", [])],
        evidence_kinds=("commit+ci",), review_tier="high")
    mixed = {SHA_A: writer("mimo-v2 run1"),       # xiaomi
             SHA_B: writer("deepseek-r1 run2")}   # deepseek
    to_review_with_commits(w, covered, mixed)
    to_review_with_commits(w, refused, mixed)

    assert w.store.verdict(w.p("rAlibaba"), covered, True, "one")["state"] == "in_review"
    result = w.store.verdict(w.p("rAnthropic1"), covered, True, "two")
    assert result["state"] == "done"
    assert result["reviews_passed"] == 2

    assert w.store.verdict(w.p("rAlibaba"), refused, True, "one")["state"] == "in_review"
    round_no = w.store._item(refused)["round"]
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), refused, True, "two")
    assert exc.value.code == "same_family"
    assert "xiaomi" in exc.value.msg
    item = w.store._item(refused)
    assert item["state"] == "in_review"
    assert item["round"] == round_no
    assert len(verdict_events(w, refused)) == 1


# --------------------------------------------------------------------------
# 2. two Claude seats are ONE family, even with a D-217 waiver
# --------------------------------------------------------------------------

def test_two_claude_seats_are_one_family(w):
    plain, waived = seed(
        w, [("A", []), ("B", [])],
        evidence_kinds=("commit+ci",), review_tier="high")
    trailers = {SHA_A: writer("mimo-v2 run1")}   # xiaomi
    to_review_with_commits(w, plain, trailers)
    to_review_with_commits(w, waived, trailers)

    # Without a waiver the existing second-family gate refuses first.
    assert w.store.verdict(w.p("rAnthropic1"), plain, True, "one")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic2"), plain, True, "two")
    assert exc.value.code in ("same_family", "coverage")
    assert w.store._item(plain)["state"] == "in_review"

    # The waiver opens that gate; coverage still sees a single family.
    w.store.ctx = make_ctx(
        git=git_with(trailers),
        decisions=FakeDecisions(waiving={(DECISION, waived)}))
    w.store.waive_second_family(w.p("router"), waived, DECISION)
    assert w.store._item(waived)["review_waiver"] == DECISION
    assert w.store.verdict(w.p("rAnthropic1"), waived, True, "one")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic2"), waived, True, "two")
    assert exc.value.code == "coverage"
    assert "xiaomi" in exc.value.msg
    assert w.store._item(waived)["state"] == "in_review"
    assert len(verdict_events(w, waived)) == 1


# --------------------------------------------------------------------------
# 3. a sensitive (HIGH) item can never close on Claude seats alone
# --------------------------------------------------------------------------

def test_sensitive_high_cannot_close_with_claude_seats_only(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",),
                  review_tier="high")
    trailers = {SHA_A: writer("mimo-v2 run1")}
    to_review_with_commits(w, key, trailers)
    w.store.ctx = make_ctx(
        git=git_with(trailers),
        decisions=FakeDecisions(waiving={(DECISION, key)}))
    w.store.waive_second_family(w.p("router"), key, DECISION)

    assert w.store.verdict(w.p("rAnthropic1"), key, True, "one")["state"] == "in_review"
    for reviewer in ("rAnthropic2", "rAnthropic2"):
        with pytest.raises(Refused) as exc:
            w.store.verdict(w.p(reviewer), key, True, "claude only")
        assert exc.value.code == "coverage"
        assert "xiaomi" in exc.value.msg
    assert w.store._item(key)["state"] == "in_review"
    assert len(verdict_events(w, key)) == 1


# --------------------------------------------------------------------------
# 4. standard item: one seat covers the sole writer, not a mixed pair
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seat,done,blamed", [
    ("rGoogle", True, None),
    ("rAnthropic1", False, "anthropic"),
    ("rXiaomi", False, "xiaomi"),
])
def test_standard_mixed_writers_need_every_family_covered(w, seat, done, blamed):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {
        SHA_A: writer("mimo-v2 run1"),        # xiaomi
        SHA_B: writer("claude-opus run2"),    # anthropic
    })

    if done:
        assert w.store.verdict(w.p(seat), key, True, "covered")["state"] == "done"
        return
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p(seat), key, True, "not covered")
    assert exc.value.code == "same_family"
    assert blamed in exc.value.msg
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# 5. standard item: Claude may review a single non-Claude writer
# --------------------------------------------------------------------------

def test_standard_single_writer_claude_seat_is_allowed(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("deepseek-r1 run1")})
    result = w.store.verdict(w.p("rAnthropic1"), key, True, "claude seat")
    assert result["state"] == "done"
    assert result["reviews_passed"] == 1


# --------------------------------------------------------------------------
# 6. fail closed when a writer family cannot be resolved
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["missing_writer", "unknown_model",
                                  "trailers_error"])
def test_unresolvable_writer_family_refuses_the_closing_verdict(w, case):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    if case == "missing_writer":
        trailers = {SHA_A: {}}
        git = None
    elif case == "unknown_model":
        trailers = {SHA_A: writer("frob-9 run1")}
        git = None
    else:
        trailers = {SHA_A: writer("mimo-v2 run1")}
        git = RaisingGit()
    to_review_with_commits(w, key, trailers, git=git)

    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rGoogle"), key, True, "would close")
    assert exc.value.code == "writer_unknown"
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


@pytest.mark.parametrize("trailer,family", [
    ("mimo-v2 run-qwen3-max", "xiaomi"),
    ("deepseek-r1 run-claude", "deepseek"),
])
def test_writer_family_uses_only_the_first_trailer_token(w, trailer, family):
    """The run id may name another family; only ``<model-id>`` decides."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(trailer)})
    assert set(_cov().writer_families(
        w.store, w.store._item(key), w.store.ctx)) == {family}
    assert w.store.verdict(w.p("rAnthropic1"), key, True, "covered")["state"] == "done"


# --------------------------------------------------------------------------
# 7. decision-only evidence has no commit writers: behaviour unchanged
# --------------------------------------------------------------------------

def test_decision_only_item_has_no_commit_writers_and_still_closes(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("decision",))
    w.store.ctx = make_ctx(git=FakeGit())
    to_in_review(w, key, who="claimant")
    families = _cov().writer_families(
        w.store, w.store._item(key), w.store.ctx)
    assert set(families) == set()
    assert w.store.verdict(w.p("rGoogle"), key, True, "no commits")["state"] == "done"


# --------------------------------------------------------------------------
# 8. ok=False is a change request, never a coverage refusal
# --------------------------------------------------------------------------

def test_changes_requested_with_uncovered_writer_is_not_refused_for_coverage(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})

    # Family isolation applies to change requests too; another family may act.
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), key, False, "needs work")
    assert exc.value.code == "same_family"
    assert w.store._item(key)["state"] == "in_review"
    result = w.store.verdict(w.p("rGoogle"), key, False, "needs work")
    assert result["state"] == "claimed"
    assert w.store._item(key)["state"] == "claimed"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# 9. only the CLOSING high verdict is checked; a refusal appends nothing
# --------------------------------------------------------------------------

def test_first_high_verdict_is_not_checked_and_refusal_is_atomic(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",),
                  review_tier="high")
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 run1")})
    round_no = w.store._item(key)["round"]

    first = w.store.verdict(w.p("rAlibaba"), key, True, "first")
    assert first["state"] == "in_review"
    assert len(verdict_events(w, key)) == 1

    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), key, True, "second")
    assert exc.value.code == "same_family"
    item = w.store._item(key)
    assert item["state"] == "in_review"
    assert item["round"] == round_no
    assert len(verdict_events(w, key)) == 1

    # Atomic: the refused seat left no trace, so a covering family closes.
    result = w.store.verdict(w.p("rGoogle"), key, True, "covering family")
    assert result["state"] == "done"
    assert result["reviews_passed"] == 2
    assert len(verdict_events(w, key)) == 2


# --------------------------------------------------------------------------
# 10. the pure function: table plus parity with tools/seat_verdict.coverage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("writers,seats,need,sensitive,want", [
    (["xiaomi"], ["anthropic", "anthropic"], 2, False, ["xiaomi"]),
    (["xiaomi"], ["anthropic", "alibaba"], 2, False, []),
    (["xiaomi"], ["anthropic"], 1, False, []),
    (["xiaomi"], ["anthropic"], 1, True, ["xiaomi"]),
    (["xiaomi"], ["anthropic", "alibaba"], 1, True, []),
    (["xiaomi", "alibaba"], ["alibaba", "google"], 2, False, ["alibaba"]),
    (["xiaomi", "deepseek"], ["alibaba", "anthropic"], 2, True, []),
    (["xiaomi"], ["xiaomi"], 1, False, ["xiaomi"]),
    ([], ["anthropic"], 2, True, []),
    ([" Xiaomi "], [" ANTHROPIC ", "alibaba"], 2, False, []),
])
def test_uncovered_table(writers, seats, need, sensitive, want):
    assert _cov().uncovered(writers, seats, need=need,
                            sensitive=sensitive) == want


def test_uncovered_matches_the_client_coverage_grid():
    writer_sets = [[], ["xiaomi"], ["deepseek"], ["xiaomi", "deepseek"],
                   ["anthropic"]]
    seat_sets = [[], ["anthropic"], ["anthropic", "anthropic"], ["alibaba"],
                 ["xiaomi"], ["anthropic", "alibaba"],
                 ["anthropic", "xiaomi"], ["deepseek", "anthropic"],
                 ["alibaba", "google"], ["google", "google"]]
    for writers, seats, need in product(writer_sets, seat_sets, (1, 2)):
        for sensitive in (False, True):
            assert _cov().uncovered(writers, seats, need=need,
                                    sensitive=sensitive) == sv.coverage(
                writers, seats, need=need, sensitive=sensitive)


# --------------------------------------------------------------------------
# 11. one family table: the keeper imports plangraph.review.family's
# --------------------------------------------------------------------------

def test_coverage_reuses_the_review_family_table():
    import inspect

    cov = _cov()
    source = inspect.getsource(cov)
    # No second family table or regex table: both live in plangraph.review.family.
    assert "FAMILIES" not in vars(cov)
    assert "gpt|^o" not in source
    assert "family" in source
    if "model_family" in vars(cov):
        assert cov.model_family is review_family.model_family


# --------------------------------------------------------------------------
# ADDENDUM: the evidence commit is the PR MERGE commit (12-14) ...
# --------------------------------------------------------------------------

MERGE_SHA = "e" * 40
PHASE_SHA = "f" * 40


def merge_git(branch_trailers, sha=MERGE_SHA, merge_trailers=None):
    """A ``FakeGit`` whose evidence *sha* is a merge commit.

    The merge commit itself carries only ``Plan-Item`` (that is what L1
    attaches); ``writer_trailers`` yields one dict per branch commit -- the
    flattened list the adapter returns for a merge, or for a PHASE merge whose
    parent2 range holds inner PR merges (D-2308).
    """
    return FakeGit(
        trailers_by_sha={sha: merge_trailers or {"Plan-Item": "demo:MERGE"}},
        branch_trailers_by_sha={sha: [dict(d) for d in branch_trailers]},
    )


def to_review_merge(w, key, git, sha=MERGE_SHA):
    """Claim as ``claimant``, attach the merge sha as commit+ci, submit."""
    return to_review_with_commits(
        w, key, {sha: git.trailers_by_sha.get(sha, {})}, git=git)


def test_merge_evidence_takes_the_writers_from_the_branch_commits(w):
    covered, refused = seed(
        w, [("A", []), ("B", [])],
        evidence_kinds=("commit+ci",), review_tier="high")
    git = merge_git([writer("mimo-v2 run1"),       # xiaomi
                     writer("deepseek-r1 run2")])  # deepseek
    to_review_merge(w, covered, git)
    to_review_merge(w, refused, git)
    assert set(_cov().writer_families(
        w.store, w.store._item(covered), w.store.ctx)) == {"xiaomi", "deepseek"}

    assert w.store.verdict(w.p("rAlibaba"), covered, True, "one")["state"] == "in_review"
    assert w.store.verdict(w.p("rAnthropic1"), covered, True, "two")["state"] == "done"

    assert w.store.verdict(w.p("rAlibaba"), refused, True, "one")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), refused, True, "two")
    assert exc.value.code == "same_family"
    assert "xiaomi" in exc.value.msg
    assert w.store._item(refused)["state"] == "in_review"
    assert len(verdict_events(w, refused)) == 1


@pytest.mark.parametrize("branch_trailers", [
    [],                                # no branch commit at all
    [{"Plan-Item": "demo:A"}],         # branch commits, none naming a writer
], ids=["no_branch_commits", "no_writer_trailer"])
def test_merge_without_a_qualifying_writer_fails_closed(w, branch_trailers):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_merge(w, key, merge_git(branch_trailers))

    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rGoogle"), key, True, "would close")
    assert exc.value.code == "writer_unknown"
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# 14. a Hand-Edit branch commit (no Writer) is a Claude hand: anthropic
# --------------------------------------------------------------------------

def test_hand_edit_branch_commit_counts_as_anthropic(w):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_merge(w, key, merge_git([{"Hand-Edit": "D-2240"}]))
    assert set(_cov().writer_families(
        w.store, w.store._item(key), w.store.ctx)) == {"anthropic"}

    # Claude's own hand may not cover itself on a standard item...
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic1"), key, True, "hand edit")
    assert exc.value.code == "same_family"
    assert "anthropic" in exc.value.msg
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []

    # ...but any other single family closes it (the refusal left no trace).
    assert w.store.verdict(w.p("rGoogle"), key, True, "other family")["state"] == "done"


# --------------------------------------------------------------------------
# 16. (D-2308) PHASE merge: only the leaf branch commits name writers
# --------------------------------------------------------------------------

def test_phase_merge_uses_the_flattened_leaf_trailers(w):
    covered, refused = seed(
        w, [("A", []), ("B", [])],
        evidence_kinds=("commit+ci",), review_tier="high")
    # Inner PR merges (Plan-Item only) are dropped by the adapter; the leaves
    # remain, and the PHASE merge commit's own trailers are never consulted.
    leaves = [writer("mimo-v2 run1"), writer("deepseek-r1 run2")]
    git = merge_git(leaves, sha=PHASE_SHA,
                    merge_trailers={"Plan-Item": "demo:PHASE"})
    to_review_merge(w, covered, git, sha=PHASE_SHA)
    to_review_merge(w, refused, git, sha=PHASE_SHA)
    assert set(_cov().writer_families(
        w.store, w.store._item(covered), w.store.ctx)) == {"xiaomi", "deepseek"}

    assert w.store.verdict(w.p("rAlibaba"), covered, True, "one")["state"] == "in_review"
    assert w.store.verdict(w.p("rAnthropic1"), covered, True, "two")["state"] == "done"

    assert w.store.verdict(w.p("rAlibaba"), refused, True, "one")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), refused, True, "two")
    assert exc.value.code == "same_family"
    assert "xiaomi" in exc.value.msg
    assert w.store._item(refused)["state"] == "in_review"
    assert len(verdict_events(w, refused)) == 1
