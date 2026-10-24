"""Multi-seat done criteria must agree with the stored review tier."""
import pytest

from keeper_util import World, defn
from plangraph.keeper.errors import Refused
from plangraph.keeper.store_tiers import seat_clause_needs_high, check_seat_clause_tier


@pytest.mark.parametrize("clause", [
    "2 seats", "two seats", "2 families", "two families",
    "2 non-openai seats", "2 non-Claude seats", "two distinct families",
    "two reviewers", "2 reviewers", "2 cross-family seats",
    "two cross-family verdicts", "2 independent families", "TWO REVIEWERS",
    "two distinct independent cross-family verdicts",
] + [f"{count} {noun}" for count in
     ("3", "4", "5", "6", "7", "8", "9", "three", "four", "five",
      "six", "seven", "eight", "nine")
     for noun in ("seat", "seats", "family", "families", "reviewer",
                  "reviewers", "verdict", "verdicts")])
def test_matching_clause(clause):
    assert seat_clause_needs_high(f"merged; {clause} approve") == clause


@pytest.mark.parametrize("text", [
    "1 seat", "one family", "the 2 files", "two files", "need=1",
    "step 2 of the families table", "", None, "12 seats", "twenty-two seats",
])
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
    assert "2 non-Claude seats" in str(exc.value)
    assert "high" in str(exc.value)
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
    assert "2 families" in str(exc.value)
    assert "standard" in str(exc.value) and "high" in str(exc.value)
    assert world.store._item(key) == before


@pytest.mark.parametrize("fields", [
    {"review_tier": "high"}, {"title": "Wire the gateway"},
])
def test_edit_uses_final_tier(world_like, fields):
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
    assert world.store._item(key)["review_tier"] == "high"


@pytest.mark.parametrize("fields", [{"priority": 5}, {"description": "Updated brief"}])
def test_unrelated_legacy_edit_allowed(world_like, fields):
    world = world_like
    key = _create(world)
    world.conn.execute("UPDATE items SET done_criteria=? WHERE key=?",
                       ("two families approve", key))
    world.conn.commit()
    world.store.edit_definition(world.p("router"), key, fields)
    item = world.store._item(key)
    assert item["review_tier"] == "standard"
    for field, value in fields.items():
        assert item[field] == value
