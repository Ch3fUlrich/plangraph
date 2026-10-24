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


@pytest.mark.parametrize("value,family", [
    ("deepseek-v4 codex", None),
    ("codex/gpt-6.1", "openai"),
    ("CODEX/gpt-6.1", "openai"),
    ("deepseek-v4 openai", None),
    ("DeepSeek-v4 OpenAI", None),
])
def test_fix11_codex_and_family_names_cannot_hide_second_writer(w, value, family):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    if family is None:
        with pytest.raises(Refused, match="writer_mixed_family"):
            _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    else:
        assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == [family]


@pytest.mark.parametrize("family", sorted({family for _, family in review_family.FAMILIES}))
def test_fix11_family_name_tokens_resolve_to_themselves(w, family):
    """Public path, not the table: an upper-case family name still resolves."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(family.upper())})
    assert _cov().writer_families(
        w.store, w.store._item(key), w.store.ctx) == [family]


# --------------------------------------------------------------------------
# FIX12 (V1): family/alias tokens resolve ONLY in the model/provider
# position -- the token(s) before the run id -- and never inside a run-id
# slug, where "meta", "google" and "nvidia" are words, not writers.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,family", [
    ("omniroute/cx-gpt-6.1-sol 20261024-061038-meta-review-18b0fc", "openai"),
    ("omniroute/cx-gpt-6.1-sol 20261024-061038-google-sync-abc", "openai"),
    ("mimo/mimo-v2.6-flash 20261024-1-nvidia-driver-x", "xiaomi"),
    ("omniroute/cx-gpt-6.1-sol 20261024-061038-keeper-trailer-multi-fix-18b0fc",
     "openai"),
])
def test_fix12_run_id_slug_never_resolves_a_second_writer_family(w, value, family):
    """The run id is a slug, not a model: only the model/provider tokens count."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    assert _cov().writer_families(
        w.store, w.store._item(key), w.store.ctx) == [family]


@pytest.mark.parametrize("value", ["deepseek-v4 codex", "cx/gpt-6.1-sol mimo"])
def test_fix12_two_model_tokens_refuse_as_mixed(w, value):
    """Two genuine model tokens are two writers: refused, and as MIXED."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_mixed_family"
    assert w.store._item(key)["state"] == "in_review"


# --------------------------------------------------------------------------
# FIX12 (V2): an unknown provider/model token in the provider position is a
# distinct fail-closed reason: writer_unknown_family, not the blank-writer
# writer_unknown.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["grok-4 test-run-1", "kimi-k2 test-run-1"])
def test_fix12_unknown_family_in_provider_position_fails_closed(w, value):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == "writer_unknown_family"
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# FIX12 (V3): three distinct refusal reasons, asserted exactly through the
# seat-coverage check on the verdict that would close the item.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,reason", [
    ("", "writer_unknown"),                          # blank attribution
    ("grok-4 test-run-1", "writer_unknown_family"),  # no known family token
    ("deepseek-v4 codex", "writer_mixed_family"),    # two writer families
])
def test_fix12_refusal_reason_is_exact_on_the_closing_verdict(w, value, reason):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rGoogle"), key, True, "would close")
    assert exc.value.code == reason
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


# --------------------------------------------------------------------------
# FIX13 (strict Writer-value grammar): G1 the model/provider position is the
# token(s) before the run id; G2 a date-stamped run-id token ENDS that
# position, so trailing text after the run id is slug text, never a second
# writer; G3 a masked id segment (`<hex>`, `<uuid>`) is run-id shaped and
# contributes no model token; G4 the first word of the first word-bearing
# token -- and of any token that names a family -- must be a word the
# alias/family table knows, else the value fails closed as
# writer_unknown_family even when a later token resolves.  Every code is
# asserted exactly through the public ``writer_families`` path.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,reason", [
    ("kimi-k2 codex", "writer_unknown_family"),
    ("mimo-v2 run-claude", "writer_unknown_family"),
])
def test_fix13_unresolved_model_head_fails_closed(w, value, reason):
    """G4: a later family token (``codex``, ``claude``) never rescues an
    unresolvable model head -- the reason code is exact, not the catch-all."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    with pytest.raises(Refused) as exc:
        _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    assert exc.value.code == reason
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


def test_fix13_text_after_the_run_id_is_not_a_second_writer(w):
    """G2: ``codex`` after the date-stamped run id is slug text, ignored."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(
        w, key, {SHA_A: writer("mimo-v2.6-flash 20261024-072324-x codex")})
    assert _cov().writer_families(
        w.store, w.store._item(key), w.store.ctx) == ["xiaomi"]


@pytest.mark.parametrize("value,family", [
    ("cx/gpt-6.1-sol mimo", None),
    ("deepseek-v4 xiaomi-mimo", None),
    ("mimo/mimo-v2.6-flash", "xiaomi"),
    ("xiaomi-mimo/mimo-v2.6-flash", "xiaomi"),
    ("cx/gpt-6.1-sol 20261024-050222-x-a64465", "openai"),
    ("omniroute/cx-gpt-6.1-sol", "openai"),
    ("agy/gemini-3.8-flash", "google"),
    ("deepseek-v4 cx", None),
])
def test_fix10_single_family_alias_cannot_hide_second_writer(w, value, family):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    if family is None:
        with pytest.raises(Refused) as exc:
            _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
        assert exc.value.code == "writer_mixed_family"
    else:
        assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == [family]


@pytest.mark.parametrize("value,family", [
    ("deepseek.mimo", None), ("mimo_deepseek", None),
    ("deepseek-r1-L2", None), ("gpt-4o.qwen3", None),
    ("deepseek_l2", None), ("deepseek.o3", None),
    ("qwen3.8-flash", "alibaba"), ("cx-gpt-6.1-sol", "openai"),
    ("xiaomi-mimo/mimo-v2.6-flash", "xiaomi"),
    ("deepseek-v4.1-flash-free", "deepseek"),
])
def test_fix9_interior_punctuation_preserves_every_family(w, value, family):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    if family is None:
        with pytest.raises(Refused, match="writer_mixed_family"):
            _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
    else:
        assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == [family]


@pytest.mark.parametrize("blank", [None, "", " \t "])
@pytest.mark.parametrize("kind", ["hand-only", "hand-and-writer", "writer-and-hand", "writers"])
def test_fix9_blank_attribution_never_supplies_a_family(w, blank, kind):
    trailer = {
        "hand-only": {"Hand-Edit": blank},
        "hand-and-writer": {"Hand-Edit": blank, "Writer": "deepseek-r1"},
        "writer-and-hand": {"Writer": blank, "Hand-Edit": "manual"},
        "writers": {"Writer": ["deepseek-r1", blank]},
    }[kind]
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: trailer})
    if kind == "hand-and-writer":
        assert _cov().writer_families(w.store, w.store._item(key), w.store.ctx) == ["deepseek"]
    else:
        with pytest.raises(Refused, match="writer_unknown"):
            _cov().writer_families(w.store, w.store._item(key), w.store.ctx)


@pytest.mark.parametrize("value", ["", " \t ", ":;,/()[]...---"])
def test_fix8_empty_writer_values_refuse_closing_verdict(w, value):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(value)})
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rGoogle"), key, True, "would close")
    assert exc.value.code == "writer_unknown"
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


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
    mixed = {SHA_A: writer("mimo-v2 test-run-1"),       # xiaomi
             SHA_B: writer("deepseek-r1 test-run-2")}   # deepseek
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
    trailers = {SHA_A: writer("mimo-v2 test-run-1")}   # xiaomi
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
    trailers = {SHA_A: writer("mimo-v2 test-run-1")}
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


@pytest.mark.parametrize("with_writer", [False, True])
@pytest.mark.parametrize("keys", [("Writer", "Hand-Edit"), ("writer", "hand-edit")])
def test_fix6_hand_edit_case_and_writer_are_additive(w, with_writer, keys):
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",), review_tier="high")
    trailer = {keys[1]: "manual correction"}
    if with_writer:
        trailer[keys[0]] = "mimo-v2"
    to_review_with_commits(w, key, {SHA_A: trailer})
    assert set(_cov().writer_families(w.store, w.store._item(key), w.store.ctx)) == (
        {"anthropic", "xiaomi"} if with_writer else {"anthropic"})
    assert w.store.verdict(w.p("rGoogle"), key, True, "first")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rAnthropic1"), key, True, "self cannot cover")
    assert exc.value.code == "coverage"
    assert w.store.verdict(w.p("rAlibaba"), key, True, "both covered")["state"] == "done"


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
        SHA_A: writer("mimo-v2 test-run-1"),        # xiaomi
        SHA_B: writer("claude-opus test-run-2"),    # anthropic
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
    to_review_with_commits(w, key, {SHA_A: writer("deepseek-r1 test-run-1")})
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
        trailers = {SHA_A: writer("frob-9 test-run-1")}
        git = None
    else:
        trailers = {SHA_A: writer("mimo-v2 test-run-1")}
        git = RaisingGit()
    to_review_with_commits(w, key, trailers, git=git)

    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rGoogle"), key, True, "would close")
    # FIX12 (V2): a model token the table cannot map is writer_unknown_family;
    # a missing trailer or a git error keeps the plain writer_unknown.
    want = ("writer_unknown_family" if case == "unknown_model"
            else "writer_unknown")
    assert exc.value.code == want
    assert w.store._item(key)["state"] == "in_review"
    assert verdict_events(w, key) == []


@pytest.mark.parametrize("trailer,family", [
    ("mimo-v2 test-task-abc123", "xiaomi"),
    ("deepseek-r1 test-task-def456", "deepseek"),
    ("deepseek-r1 run1", "deepseek"),
    ("deepseek-r1 run-claude", None),
])
def test_writer_family_counts_model_mentions_in_every_token(w, trailer, family):
    """Every model-position token counts.  ``run-claude`` names anthropic,
    but its head word ``run`` is no model the table knows, so FIX13 (G4)
    fails the value closed as ``writer_unknown_family`` -- the mention is
    never dropped and the family is never guessed."""
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",))
    to_review_with_commits(w, key, {SHA_A: writer(trailer)})
    if family is None:
        with pytest.raises(Refused) as exc:
            _cov().writer_families(w.store, w.store._item(key), w.store.ctx)
        assert exc.value.code == "writer_unknown_family"
        return
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
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 test-run-1")})

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
    to_review_with_commits(w, key, {SHA_A: writer("mimo-v2 test-run-1")})
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
    assert cov.review_family.FAMILIES is review_family.FAMILIES
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
    git = merge_git([writer("mimo-v2 test-run-1"),       # xiaomi
                     writer("deepseek-r1 test-run-2")])  # deepseek
    to_review_merge(w, covered, git)
    to_review_merge(w, refused, git)
    assert set(_cov().writer_families(
        w.store, w.store._item(covered), w.store.ctx)) == {"xiaomi", "deepseek"}

    assert w.store.verdict(w.p("rAlibaba"), covered, True, "one")["state"] == "in_review"
    assert w.store.verdict(w.p("rAnthropic1"), covered, True, "two")["state"] == "done"

    assert w.store.verdict(w.p("rAlibaba"), refused, True, "one")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rXiaomi"), refused, True, "two")
    assert exc.value.code == "coverage"
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
    assert exc.value.code == "coverage"
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
    leaves = [writer("mimo-v2 test-run-1"), writer("deepseek-r1 test-run-2")]
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
    assert exc.value.code == "coverage"
    assert "xiaomi" in exc.value.msg
    assert w.store._item(refused)["state"] == "in_review"
    assert len(verdict_events(w, refused)) == 1


# --------------------------------------------------------------------------
# 17. (KEEPER-TRAILER-MULTI) a SQUASHED commit: several ``Writer:`` lines on
#     ONE leaf commit -- every family must be covered, not the last one alone
# --------------------------------------------------------------------------

SQUASH_SHA = "9" * 40


def squash_trailers(writers):
    """One leaf commit carrying several ``Writer:`` lines (a squashed branch)."""
    return {SQUASH_SHA: {"Plan-Item": "demo:SQUASH",
                         "Writer": list(writers)}}


def test_squash_commit_with_several_writer_families_needs_all_covered(w):
    """Every ``Writer:`` line of one leaf commit is a family that needs a seat.

    A last-value-wins trailer parse (or a lookup that sees only the first
    value) exposes ONE of ``mimo-v2`` (xiaomi) / ``deepseek-r1`` (deepseek);
    a seat pair covering that one family must not close the item.
    """
    covered, refused = seed(
        w, [("A", []), ("B", [])],
        evidence_kinds=("commit+ci",), review_tier="high")
    trailers = squash_trailers(["mimo-v2 test-run-1", "deepseek-r1 test-run-2"])
    to_review_with_commits(w, covered, trailers, git=git_with(trailers))
    to_review_with_commits(w, refused, trailers, git=git_with(trailers))
    assert set(_cov().writer_families(
        w.store, w.store._item(refused), w.store.ctx)) == {"xiaomi", "deepseek"}

    # Seats covering BOTH families close the item.
    assert w.store.verdict(w.p("rAlibaba"), covered, True, "one")["state"] == "in_review"
    assert w.store.verdict(w.p("rAnthropic1"), covered, True, "two")["state"] == "done"

    # alibaba + deepseek cover xiaomi (two other families) but a deepseek seat
    # never covers the deepseek writer itself: only the SECOND Writer line
    # makes deepseek a family that needs covering.
    assert w.store.verdict(w.p("rAlibaba"), refused, True, "one")["state"] == "in_review"
    round_no = w.store._item(refused)["round"]
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rDeepseek"), refused, True, "two")
    assert exc.value.code == "coverage"
    assert "deepseek" in exc.value.msg
    item = w.store._item(refused)
    assert item["state"] == "in_review"
    assert item["round"] == round_no
    assert len(verdict_events(w, refused)) == 1


# --------------------------------------------------------------------------
# 18. (FIX2 r2) git trailer keys are case-insensitive: a lower-case
#     ``writer:`` line next to ``Writer:`` names a second family
# --------------------------------------------------------------------------

def test_lower_case_writer_key_counts_alongside_the_capitalised_one(w):
    """``writer:`` and ``Writer:`` are the same trailer key to git.

    A case-sensitive ``trailer.get("Writer")`` sees only one of them, so the
    second writer family would never need a seat.
    """
    (key,) = seed(w, [("A", [])], evidence_kinds=("commit+ci",),
                  review_tier="high")
    trailers = {SHA_A: {"Plan-Item": "demo:A",
                        "Writer": "mimo-v2 test-run-1",        # xiaomi
                        "writer": "deepseek-r1 test-run-2"}}   # deepseek
    to_review_with_commits(w, key, trailers, git=git_with(trailers))

    assert set(_cov().writer_families(
        w.store, w.store._item(key), w.store.ctx)) == {"xiaomi", "deepseek"}

    # Seats [deepseek, google] cover the xiaomi writer alone, but a deepseek
    # seat never covers the deepseek writer itself (and google is its only
    # other family): the case-sensitive view would have closed the item.
    assert w.store.verdict(w.p("rDeepseek"), key, True, "one")["state"] == "in_review"
    with pytest.raises(Refused) as exc:
        w.store.verdict(w.p("rGoogle"), key, True, "two")
    assert exc.value.code == "coverage"
    assert "deepseek" in exc.value.msg
    assert w.store._item(key)["state"] == "in_review"
    assert len(verdict_events(w, key)) == 1
