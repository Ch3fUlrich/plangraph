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
    assert exc.value.code == "coverage"
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
    assert exc.value.code == "coverage"
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

    # The reviewer's own family is the uncovered writer family, but ok=False
    # still just returns the item to claimed.
    result = w.store.verdict(w.p("rXiaomi"), key, False, "needs work")
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
    assert exc.value.code == "coverage"
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

