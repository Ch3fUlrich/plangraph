"""Multi-seat done criteria must agree with the stored review tier."""
import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.store_tiers import REVIEW_TIERS, seat_clause_needs_high, check_seat_clause_tier


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
    "a second reviewer signs off", "another family reviews", "each family must verdict",
    "section 3 reviewers", "2 reviewers' comments", "2 reviewers’ comments",
    "2 very very very very independent seats", "2 independent; distinct families",
    "1 seats", "one seats", "single seats", "seats: 1", "reviewers = one",
    "1-1 seats", "1 to 1 seats", "2 independent: distinct families",
] + [f"2 reviewers {word}" for word in
     ("table", "section", "list", "chart", "column", "row", "page", "doc", "docs",
      "comments", "findings", "notes", "feedback", "packages", "licenses")]
  + [f"{word} 2 seats" for word in
     ("step", "table", "section", "figure", "phase", "row", "column", "part",
      "item", "level", "tier", "page", "chapter")])
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
    before = world.store._item(key)
    with pytest.raises(Refused) as exc:
        world.store.edit_definition(world.p("router"), key,
                                    {"done_criteria": "merged; 2 families approve"})
    assert exc.value.code == "validation"
    assert "done_criteria ask for 2 families but review_tier is 'standard';" in str(exc.value)
    assert world.store._item(key) == before


@pytest.mark.parametrize("fields", [
    {"review_tier": "high"}, {"title": "Wire the gateway"},
])
def test_edit_uses_final_tier(world_like, fields):
    # A gateway rename escalates via the rename keyword mechanism, not criteria.
    world = world_like
    key = _create(world)
    world.store.edit_definition(world.p("router"), key,
                                {"done_criteria": "two families approve", **fields})
    assert world.store._item(key)["review_tier"] == "high"


def test_tier_only_edit_checks_existing_criteria(world_like):
    world = world_like
    key = _create(world, review_tier="high", done_criteria="two families approve")
    with pytest.raises(Refused) as exc:
        world.store.edit_definition(world.p("router"), key, {"review_tier": "standard"})
    assert exc.value.code == "validation"
    assert "done_criteria ask for two families but review_tier is 'standard';" in str(exc.value)
    assert world.store._item(key)["review_tier"] == "high"


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
