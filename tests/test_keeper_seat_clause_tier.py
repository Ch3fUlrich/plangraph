"""Multi-seat done criteria must agree with the stored review tier."""
import copy
import time

import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.store_tiers import (
    REVIEW_TIERS, check_seat_clause_tier, seat_clause_needs_high,
)


@pytest.mark.parametrize("clause", [
    "2 seats", "two seats", "2 families", "two families",
    "2 non-openai seats", "2 non-Claude seats", "two distinct families",
    "two reviewers", "2 reviewers", "2 cross-family seats",
    "two cross-family verdicts", "2 independent families", "TWO REVIEWERS",
    "two distinct independent cross-family verdicts",
    "12 seats", "twenty-two seats", "20 reviewers", "10 seats", "ten seats",
    "both seats", "both of the families", "a second reviewers", "another families",
    "2 of the reviewers", "2 of the families", "2 of reviewers",
    "2 independent, distinct families", "2 L2 reviewers", "2 non-gpt4 seats",
    "2 gpt4 seats", "2 tier-2 reviewers", "2 cross-family verdicts",
    "2-3 reviewers", "2 to 3 seats", "1-2 seats", "2+ seats",
    "at least 2 seats", "min 2 seats", "2-seat", "two-family", "2-reviewer",
    "two-seat", "seats: 2", "reviewers = 3", "families: two", "2 sign-offs",
    "20 seats", "twenty seats", "eleven seats", "twelve seats",
    "12345678901234567890 seats", "2-3 seats", "2 to 3 reviewers",
    "2-family", "two-verdict", "2 seat", "2 reviewer", "2 family",
    "2 signoffs", "2 sign offs", "a second seat", "a second reviewer",
    "a second family", "a second verdict", "another seat", "another reviewer",
    "another family",
] + [f"{count} {noun}" for count in
     ("3", "4", "5", "6", "7", "8", "9", "three", "four", "five",
      "six", "seven", "eight", "nine")
      for noun in ("seats", "families", "reviewers", "verdicts")])
def test_matching_clause(clause):
    assert seat_clause_needs_high(f"merged; {clause} approve") == clause


@pytest.mark.parametrize("text", [
    "1 seat", "one family", "the 2 files", "two files", "need=1",
    "step 2 of the families table", "", None, "1", "one", "single",
    "2 reviewer comments", "2 family packages", "reserve 2 seat licenses",
    "addressed 2 reviewer comments and fixed lint", "publish 2 family packages",
    "each family must verdict",
    "section 3 reviewers", "2 reviewers' comments", "2 reviewers’ comments",
    "2 very very very very independent seats", "2 independent; distinct families",
    "1 seats", "one seats", "single seats", "seats: 1", "reviewers = one",
    "1-1 seats", "1 to 1 seats", "2 independent: distinct families",
] + [f"2 reviewers {word}" for word in
     ("table", "section", "list", "chart", "column", "row", "page", "doc", "docs",
      "comments", "findings", "notes", "feedback", "packages", "licenses")]
  + [f"{word} 2 seats" for word in
     ("step", "table", "section", "figure", "phase", "row", "column", "part",
       "item", "page", "chapter")]
  + ["triage 2-reviewer comments", "publish 2-family packages",
     "phase:2 reviewers", "step #2", "step #2 reviewers"]
  + [f"2{separator}{noun}{possessive} {artifact}"
     for separator, noun in (("-", "reviewer"), (" ", "reviewers"))
     for possessive in ("", "'", "’")
     for artifact in ("comments", "findings", "notes", "feedback", "packages",
                      "licenses", "table", "section", "list", "chart", "column",
                      "row", "page", "doc", "docs")])
def test_non_matching_clause(text):
    assert seat_clause_needs_high(text) is None


@pytest.mark.parametrize("tier", ["standard", "none"])
def test_check_names_mismatch(tier):
    with pytest.raises(Refused) as exc:
        check_seat_clause_tier("merged; two families approve", tier)
    assert exc.value.code == "validation"
    assert exc.value.msg == (
        f"done_criteria ask for two families but review_tier is {tier!r}; "
        "two seats/families need review_tier 'high' (raise the tier or reduce the clause)")


@pytest.fixture
def world_like(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _create(world, **kw):
    lane = defn("L1", title="Ordinary lane", track="T0", **kw)
    goal = defn("GOAL", ["L1"], kind="goal")
    world.store.create_items(world.p("router"), [goal, lane])
    return "demo:L1"


def test_create_refuses_entire_batch(world_like):
    world = world_like
    with pytest.raises(Refused) as exc:
        _create(world, done_criteria="merged; 2 non-Claude seats approve")
    assert exc.value.code == "validation"
    assert "done_criteria ask for 2 non-Claude seats but review_tier is 'standard';" in str(exc.value)
    assert world.store._item("demo:L1") is None
    assert world.store._item("demo:GOAL") is None


@pytest.mark.parametrize("criteria,tier", [
    ("merged; 2 non-Claude seats approve", "high"),
    ("merged; 1 seat approves", "standard"),
])
def test_create_consistent_tier(world_like, criteria, tier):
    key = _create(world_like, done_criteria=criteria, review_tier=tier)
    assert world_like.store._item(key)["review_tier"] == tier


def test_edit_refusal_leaves_item_unchanged(world_like):
    world = world_like
    key = _create(world)
    before = copy.deepcopy(world.store._item(key))
    with pytest.raises(Refused) as exc:
        world.store.edit_definition(world.p("router"), key,
                                    {"done_criteria": "merged; 2 families approve"})
    assert exc.value.code == "validation"
    assert "done_criteria ask for 2 families but review_tier is 'standard';" in str(exc.value)
    assert world.store._item(key) == before


@pytest.mark.parametrize("fields", [
    {"review_tier": "high"}, {"title": "Wire the gateway"},
])
def test_edit_escalation_uses_final_tier(world_like, fields):
    # A gateway rename escalates via the rename keyword mechanism, not criteria.
    world = world_like
    key = _create(world)
    assert world.store._item(key)["review_tier"] == "standard"
    world.store.edit_definition(world.p("router"), key,
                                {"done_criteria": "two families approve", **fields})
    assert world.store._item(key)["review_tier"] == "high"


def test_tier_only_edit_checks_existing_criteria(world_like):
    world = world_like
    key = _create(world, review_tier="high", done_criteria="two families approve")
    before = copy.deepcopy(world.store._item(key))
    with pytest.raises(Refused) as exc:
        world.store.edit_definition(world.p("router"), key, {"review_tier": "standard"})
    assert exc.value.code == "validation"
    assert "done_criteria ask for two families but review_tier is 'standard';" in str(exc.value)
    assert world.store._item(key)["review_tier"] == "high"
    assert world.store._item(key) == before


@pytest.mark.parametrize("fields", [{"priority": 5}, {"description": "Updated brief"},
                                   {"done_criteria": "two families approve", "priority": 5}])
def test_unrelated_legacy_edit_allowed(world_like, fields):
    world = world_like
    world.store.import_items(world.p("router"), [
        defn("GOAL", ["L1"], kind="goal"),
        defn("L1", done_criteria="two families approve", review_tier="standard"),
    ])
    key = "demo:L1"
    world.store.edit_definition(world.p("router"), key, fields)
    item = world.store._item(key)
    assert item["review_tier"] == "standard"
    for field, value in fields.items():
        assert item[field] == value


def test_review_tier_set_unchanged():
    assert REVIEW_TIERS == ("standard", "high", "none")


def test_goal_multi_seat_refused(world_like):
    with pytest.raises(Refused, match="done_criteria ask for 2 seats but review_tier is 'standard';"):
        world_like.store.create_items(world_like.p("router"), [
            defn("GOAL", kind="goal", done_criteria="2 seats", review_tier="standard")])


def test_create_uses_escalated_tier(world_like):
    world_like.store.create_items(world_like.p("router"), [
        defn("GOAL", kind="goal", title="Wire the gateway", done_criteria="2 seats")])
    assert world_like.store._item("demo:GOAL")["review_tier"] == "high"


@pytest.mark.parametrize("principal,extra", [
    ("l1other", {}), ("l1main", {"depends_on": ["other:LANE"]}),
])
def test_create_authorization_precedes_seat_check(world_like, principal, extra):
    with pytest.raises(Refused) as exc:
        world_like.store.create_items(world_like.p(principal), [
            defn("GOAL", kind="goal", done_criteria="2 seats", **extra)])
    assert exc.value.code == "forbidden"


@pytest.mark.parametrize("text,clause", [
    ("2 seat review", "2 seat"),
    ("2 reviewer approval", "2 reviewer"),
    ("2 family approval", "2 family"),
    ("2 family verdicts", "2 family"),
    ("2 reviewers' verdicts approve", "2 reviewers"),
    ("a second reviewer signs off", "a second reviewer"),
    ("another family reviews", "another family"),
    ("second reviewer signs off", "second reviewer"),
    ("tier 2 reviewers", "2 reviewers"),
    ("level 2 reviewers", "2 reviewers"),
])
def test_review_phrase(text, clause):
    assert seat_clause_needs_high(text) == clause


@pytest.mark.parametrize("text", ["2 " + "a" * 598, "2 " + "x " * 150, "2" * 600])
def test_adversarial_length(text):
    start = time.perf_counter()
    assert seat_clause_needs_high(text) is None
    assert time.perf_counter() - start < 0.5


def test_import_exempt_is_intentional(world_like):
    world_like.store.import_items(world_like.p("router"), [
        defn("GOAL", ["L1"], kind="goal"),
        defn("L1", state="open", done_criteria="2 seats", review_tier="standard"),
    ])
    item = world_like.store._item("demo:L1")
    assert item["review_tier"] == "standard"
    assert item["done_criteria"] == "2 seats"
    assert item["state"] == "open"


@pytest.mark.parametrize("principal,extra", [
    ("l1other", {}), ("l1main", {"depends_on": ["other:LANE"]}),
])
def test_edit_authorization_precedes_seat_check(world_like, principal, extra):
    key = _create(world_like)
    with pytest.raises(Refused) as exc:
        world_like.store.edit_definition(world_like.p(principal), key,
                                        {"done_criteria": "2 seats", **extra})
    assert exc.value.code == "forbidden"


def test_edit_missing_legacy_tier_defaults_to_standard(world_like, monkeypatch):
    key = _create(world_like)
    original = world_like.store._get_or_refuse

    def legacy_item(key):
        item = copy.deepcopy(original(key))
        item.pop("review_tier", None)
        return item

    monkeypatch.setattr(world_like.store, "_get_or_refuse", legacy_item)
    with pytest.raises(Refused) as exc:
        world_like.store.edit_definition(world_like.p("router"), key,
                                        {"done_criteria": "2 seats"})
    assert exc.value.code == "validation"
    assert "review_tier is 'standard'" in exc.value.msg
