"""X7a1 + X7a2 — per-item ``review_tier``: defaults, permissions, verdict logic.

D-228 risk-tiered reviews.  Part 1 covered the ``review_tier`` column, its
default at definition time, the raise/lower permissions and the event/import/
export round trip; part 2 (below, after ``world_like``) covers the verdict logic
that *consumes* the tier: a ``standard`` item passes on one cross-family
verdict, a ``high`` item still needs two distinct families (D-219) and is the
only item a D-217 waiver can name.
"""
from pathlib import Path

import pytest

from keeper_util import World, defn, to_in_review
from plangraph.keeper import importer
from plangraph.keeper import events as keeper_events
from plangraph.keeper.errors import Refused


def _mk(world, who="router", project="demo", **kw):
    """Create one open lane + a goal that reaches it; return the lane key."""
    pid = kw.pop("pid", "L1")
    lane = defn(pid, project=project, **kw)
    goal = defn("GOAL", [pid], project=project, kind="goal",
                title="plan goal", track="T0")
    world.store.create_items(world.p(who), [lane, goal])
    return f"{project}:{pid}"


def _tier(world, key):
    """Read the tier back through the ``show`` surface (proves it is exposed)."""
    return world.store.show(world.p("router"), key)["review_tier"]


# ---------------------------------------------------------------- defaults
def test_default_is_standard(world_like):
    key = _mk(world_like, title="Ordinary lane", track="T0")
    assert _tier(world_like, key) == "standard"


@pytest.mark.parametrize("field,value", [
    ("title", "Rotate the signing secret"),
    ("title", "Wire the gateway"),
    ("title", "Keeper fence health"),
    ("pid", "deploy"),
    ("track", "security"),
    ("track", "omniroute"),
])
def test_default_high_by_keyword(world_like, field, value):
    kw = {"title": "a lane", "track": "T0", "pid": "L1"}
    kw[field] = value
    key = _mk(world_like, **kw)
    assert _tier(world_like, key) == "high", (field, value)


def test_default_high_for_plangraph_project(world_like):
    key = _mk(world_like, project="plangraph", title="a lane", track="T0")
    assert _tier(world_like, key) == "high"


def test_keyword_only_whole_word(world_like):
    # "keystore" must NOT trip the bare-word "key"/"keys" pattern.
    key = _mk(world_like, title="Read the keystore", track="T0")
    assert _tier(world_like, key) == "standard"


# ---------------------------------------------------------------- explicit
def test_explicit_high_honoured_without_keyword(world_like):
    key = _mk(world_like, title="a lane", track="T0", review_tier="high")
    assert _tier(world_like, key) == "high"


def test_explicit_standard_overrides_keyword_default(world_like):
    key = _mk(world_like, title="Handle the secret", review_tier="standard")
    assert _tier(world_like, key) == "standard"


def test_invalid_value_at_create_is_validation(world_like):
    with pytest.raises(Refused) as exc:
        _mk(world_like, review_tier="urgent")
    assert exc.value.code == "validation"


# ---------------------------------------------------------------- edit perms
def test_l1_may_raise_standard_to_high(world_like):
    key = _mk(world_like, who="l1main", project="demo")
    assert _tier(world_like, key) == "standard"
    world_like.store.edit_definition(world_like.p("l1main"), key,
                                     {"review_tier": "high"})
    assert _tier(world_like, key) == "high"


def test_l1_may_not_lower_high_to_standard(world_like):
    key = _mk(world_like, title="Handle the secret")
    assert _tier(world_like, key) == "high"
    with pytest.raises(Refused) as exc:
        world_like.store.edit_definition(world_like.p("l1main"), key,
                                         {"review_tier": "standard"})
    assert exc.value.code == "forbidden"
    assert _tier(world_like, key) == "high"


def test_router_may_lower_high_to_standard(world_like):
    key = _mk(world_like, title="Handle the secret")
    assert _tier(world_like, key) == "high"
    world_like.store.edit_definition(world_like.p("router"), key,
                                     {"review_tier": "standard"})
    assert _tier(world_like, key) == "standard"


def test_invalid_value_at_edit_is_validation(world_like):
    key = _mk(world_like)
    with pytest.raises(Refused) as exc:
        world_like.store.edit_definition(world_like.p("router"), key,
                                         {"review_tier": "asap"})
    assert exc.value.code == "validation"


# ---------------------------------------------------------------- fold + integrity
def test_fold_and_verify_integrity_agree(world_like):
    key = _mk(world_like, title="Handle the secret")
    world_like.store.edit_definition(world_like.p("l1main"), key,
                                     {"review_tier": "high"})
    world_like.store.edit_definition(world_like.p("router"), key,
                                     {"review_tier": "standard"})
    folded = keeper_events.fold(keeper_events.read_events(world_like.conn))
    assert folded[key]["review_tier"] == "standard"
    assert world_like.store._item(key)["review_tier"] == "standard"
    assert world_like.store.verify_integrity() == []


# ---------------------------------------------------------------- show/snapshot
def test_show_exposes_review_tier(world_like):
    key = _mk(world_like, title="Handle the secret")
    assert world_like.store.show(world_like.p("router"), key)["review_tier"] == "high"


def test_snapshot_exposes_review_tier(world_like):
    key = _mk(world_like, title="Handle the secret")
    snap = world_like.store.snapshot(world_like.p("router"))
    by_key = {it["key"]: it for it in snap["items"]}
    assert by_key[key]["review_tier"] == "high"


# ---------------------------------------------------------------- import/export
def _write_plan(dirpath, rows):
    lines = ["items:"]
    for row in rows:
        lines.append(f"  - project: {row['project']}")
        lines.append(f"    id: {row['id']}")
        lines.append(f"    title: {row['title']}")
        lines.append(f"    kind: {row['kind']}")
        lines.append(f"    track: {row['track']}")
        lines.append(f"    owner: tester")
        lines.append("    evidence_kinds: [any]")
        lines.append("    operator: false")
        if row.get("depends_on"):
            deps = ", ".join(row["depends_on"])
            lines.append(f"    depends_on: [{deps}]")
        if "review_tier" in row:
            lines.append(f"    review_tier: {row['review_tier']}")
    Path(dirpath).mkdir(parents=True, exist_ok=True)
    (Path(dirpath) / "plan.yaml").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")


def _plan_rows():
    return [
        {"project": "demo", "id": "L1", "title": "Plain lane", "kind": "lane",
         "track": "T0"},
        {"project": "demo", "id": "SEC", "title": "Vault secret sync",
         "kind": "lane", "track": "T0"},
        {"project": "demo", "id": "EXP", "title": "Explicit lane", "kind": "lane",
         "track": "T0", "review_tier": "high"},
        {"project": "demo", "id": "GOAL", "title": "plan goal", "kind": "goal",
         "track": "T0", "depends_on": ["L1", "SEC", "EXP"]},
    ]


def test_import_honours_review_tier(tmp_path):
    src = tmp_path / "in"
    _write_plan(src, _plan_rows())
    batch = {it["key"]: it for it in importer.load_dir(src)}
    assert batch["demo:L1"]["review_tier"] == "standard"
    assert batch["demo:SEC"]["review_tier"] == "high"      # keyword default
    assert batch["demo:EXP"]["review_tier"] == "high"      # explicit


def test_import_invalid_value_is_validation(tmp_path):
    rows = _plan_rows()
    rows[0]["review_tier"] = "urgent"
    src = tmp_path / "in"
    _write_plan(src, rows)
    with pytest.raises(Refused) as exc:
        importer.load_dir(src)
    assert exc.value.code == "validation"


def test_export_import_round_trip_keeps_tier(world_like, tmp_path):
    src = tmp_path / "in"
    _write_plan(src, _plan_rows())
    original = importer.load_dir(src)
    world_like.store.import_items(world_like.p("router"), original)
    out = tmp_path / "out"
    importer.export_yaml(world_like.store, out)
    again = importer.load_dir(out)
    assert again == original
    tiers = {it["key"]: it["review_tier"] for it in again}
    assert tiers == {"demo:L1": "standard", "demo:SEC": "high",
                     "demo:EXP": "high", "demo:GOAL": "standard"}


@pytest.fixture
def world_like(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


# ========================================================== X7a2: verdict logic
# D-228 part 2: the tier decides how many passing verdicts ``done`` needs.

def _legacy_review(world, owner, tier):
    """An imported in-review row whose ``claimed_by`` is ``owner``."""
    world.store.import_items(world.p("router"), [
        {"project": "demo", "id": "SELF", "title": "self review", "kind": "lane",
         "state": "in_review", "owner": owner, "depends_on": [],
         "evidence_kinds": ["any"], "review_tier": tier},
        {"project": "demo", "id": "GOAL", "title": "g", "kind": "goal",
         "state": "open", "depends_on": ["demo:SELF"], "evidence_kinds": ["any"]}])
    return "demo:SELF"


def test_standard_one_verdict_is_done_and_locked(world_like):
    """D-228: one cross-family verdict closes a standard review."""
    key = _mk(world_like, review_tier="standard")
    to_in_review(world_like, key)
    out = world_like.store.verdict(world_like.p("reviewer"), key, True, "ok")
    assert out["state"] == "done" and out["reviews_passed"] == 1
    item = world_like.store._item(key)
    assert item["state"] == "done" and item["reviewer"] == "reviewer"
    with pytest.raises(Refused) as exc:  # review_passed locked the item
        world_like.store.verdict(world_like.p("reviewer2"), key, True, "too")
    assert exc.value.code == "locked"
    assert world_like.store.verify_integrity() == []


def test_standard_refuses_reviewer_of_writers_family(world_like):
    key = _mk(world_like, review_tier="standard")
    to_in_review(world_like, key)  # claimant l1main is teamA
    with pytest.raises(Refused) as exc:
        world_like.store.verdict(world_like.p("reviewerSame"), key, True, "cozy")
    assert exc.value.code == "same_family"
    assert world_like.store._item(key)["state"] == "in_review"


def test_standard_refuses_blank_family_reviewer(world_like):
    key = _mk(world_like, review_tier="standard")
    to_in_review(world_like, key)
    with pytest.raises(Refused) as exc:
        world_like.store.verdict(world_like.p("reviewerNoFam"), key, True, "anon")
    assert exc.value.code == "family_unknown"
    assert world_like.store._item(key)["state"] == "in_review"


def test_standard_refuses_reviewer_who_is_claimant(world_like):
    key = _legacy_review(world_like, "reviewer", "standard")
    with pytest.raises(Refused) as exc:
        world_like.store.verdict(world_like.p("reviewer"), key, True, "self")
    assert exc.value.code == "same_principal"


def test_high_one_verdict_stays_in_review(world_like):
    key = _mk(world_like, review_tier="high")
    to_in_review(world_like, key)
    out = world_like.store.verdict(world_like.p("reviewer"), key, True, "ok")
    assert out["state"] == "in_review" and out["reviews_passed"] == 1
    assert world_like.store._item(key)["state"] == "in_review"


def test_high_two_distinct_families_are_done(world_like):
    key = _mk(world_like, review_tier="high")
    to_in_review(world_like, key)
    world_like.store.verdict(world_like.p("reviewer"), key, True, "first")
    out = world_like.store.verdict(world_like.p("reviewer2"), key, True, "second")
    assert out["state"] == "done" and out["reviews_passed"] == 2
    assert world_like.store._item(key)["state"] == "done"


def test_waiver_on_standard_item_is_bad_state(world_like):
    """D-228: a standard item has no second family seat, so it cannot be waived."""
    key = _mk(world_like, review_tier="standard")
    to_in_review(world_like, key)
    with pytest.raises(Refused) as exc:
        world_like.store.waive_second_family(world_like.p("router"), key, "D-217")
    assert exc.value.code == "bad_state"
    assert world_like.store._item(key)["review_waiver"] is None
