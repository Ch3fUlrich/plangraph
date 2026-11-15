"""Importer + exporter tests: fixtures, round trip, refusals, determinism."""
from pathlib import Path

import pytest

from keeper_util import World, defn, seed, to_done
from plangraph.keeper.errors import Refused
from plangraph.keeper.importer import export_yaml, load_dir

FIX = Path(__file__).parent / "fixtures" / "import_mini"


def _imported(tmp_path):
    """Import the fixtures into a fresh keeper DB; return (world, original)."""
    world = World(tmp_path)
    original = load_dir(FIX)
    result = world.store.import_items(world.p("router"), original)
    assert any("5 stored item(s)" in warning for warning in result["warnings"])
    assert "description missing: brief will be empty" in result["warnings"]
    return world, original


def test_fixtures_load_shape():
    items = load_dir(FIX)
    assert len(items) == 12
    assert [it["key"] for it in items] == sorted(it["key"] for it in items)
    assert {it["project"] for it in items} == {"atlas", "borealis"}
    assert sum(it["kind"] == "gate" for it in items) == 1
    assert sum(it["kind"] == "goal" for it in items) == 1
    by = {it["key"]: it for it in items}
    # bare ids are qualified with their own project
    assert by["atlas:R2"]["depends_on"] == ["atlas:R1"]
    # cross-project deps are kept verbatim
    assert by["borealis:A1"]["depends_on"] == ["atlas:R2"]
    assert by["borealis:A6"]["depends_on"] == ["atlas:C1", "borealis:A5"]
    # legacy in-flight states survive the load
    assert by["atlas:C1"]["state"] == "claimed"
    assert by["atlas:IR1"]["state"] == "in_review"
    assert by["borealis:A3"]["state"] == "claimed"
    assert by["borealis:A4"]["state"] == "in_review"


def test_helper_fields_fold_into_note():
    by = {it["key"]: it for it in load_dir(FIX)}
    r1 = by["atlas:R1"]
    assert "rationale" not in r1
    assert r1["note"] == "rationale: Keeps the seam clean."
    ir1 = by["atlas:IR1"]
    assert ir1["note"].splitlines()[0] == "came from a previous keeper"
    assert "blocked_by: waiting on review" in ir1["note"]
    assert by["borealis:A3"]["note"] == "plan_ref: plan-v3 section A3"


def test_round_trip(tmp_path):
    world, original = _imported(tmp_path)
    try:
        out = tmp_path / "export"
        export_yaml(world.store, out)
        again = load_dir(out)
    finally:
        world.close()
    assert again == original


def test_export_accepts_raw_connection(tmp_path):
    world, _ = _imported(tmp_path)
    try:
        from_store = tmp_path / "from_store"
        from_conn = tmp_path / "from_conn"
        export_yaml(world.store, from_store)
        export_yaml(world.conn, from_conn)
    finally:
        world.close()
    names = sorted(p.name for p in from_store.glob("*.yaml"))
    assert names == sorted(p.name for p in from_conn.glob("*.yaml"))
    for name in names:
        assert (from_store / name).read_bytes() == (from_conn / name).read_bytes()


def test_duplicate_key_rejected(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "a.yaml").write_text("items:\n- {project: p, id: X}\n")
    (bad / "b.yaml").write_text("items:\n- {project: p, id: X}\n")
    with pytest.raises(Refused) as exc:
        load_dir(bad)
    assert exc.value.code == "validation"
    assert "duplicate" in exc.value.msg


@pytest.mark.parametrize("state", ["done", "cancelled", "weird"])
def test_non_importable_state_rejected(tmp_path, state):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "a.yaml").write_text(
        f"items:\n- {{project: p, id: X, state: {state}}}\n")
    with pytest.raises(Refused) as exc:
        load_dir(bad)
    assert exc.value.code == "validation"


def test_wrong_type_rejected(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "a.yaml").write_text(
        "items:\n- {project: p, id: X, estimate_h: soon}\n")
    with pytest.raises(Refused) as exc:
        load_dir(bad)
    assert exc.value.code == "validation"


def test_export_is_deterministic(tmp_path):
    world, _ = _imported(tmp_path)
    try:
        first, second = tmp_path / "one", tmp_path / "two"
        export_yaml(world.store, first)
        export_yaml(world.store, second)
    finally:
        world.close()
    names = sorted(p.name for p in first.glob("*.yaml"))
    assert names == sorted(p.name for p in second.glob("*.yaml"))
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_export_one_file_per_track_within_budget(tmp_path):
    world, _ = _imported(tmp_path)
    try:
        out = tmp_path / "export"
        export_yaml(world.store, out)
    finally:
        world.close()
    assert sorted(p.name for p in out.glob("*.yaml")) == \
        ["T1.yaml", "T2.yaml", "T3.yaml"]
    for path in out.glob("*.yaml"):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 400


def test_export_skips_terminal_states(tmp_path):
    world = World(tmp_path)
    try:
        seed(world, [("A", []), ("B", ["A"])])
        to_done(world, "demo:A")
        out = tmp_path / "export"
        export_yaml(world.store, out)
        again = load_dir(out)
    finally:
        world.close()
    keys = {item["key"] for item in again}
    assert "demo:A" not in keys       # done is never importable
    assert "demo:B" in keys


@pytest.mark.parametrize("tier", ["standard", "high"])
@pytest.mark.parametrize("state", ["open", "claimed", "in_review"])
def test_stored_decision_only_import_round_trip(tmp_path, tier, state):
    world = World(tmp_path)
    try:
        result = world.store.import_items(world.p("router"), [defn(
            "LEGACY", kind="goal", review_tier=tier, state=state,
            evidence_kinds=["decision"], description="Stored definition")])
        assert any("1 stored item" in warning and "decision-only" in warning
                   for warning in result["warnings"])
        out = tmp_path / "export"
        export_yaml(world.store, out)
        items = load_dir(out)
        assert items[0]["review_tier"] == tier
        assert items[0]["state"] == state
        assert items[0]["evidence_kinds"] == ["decision"]
    finally:
        world.close()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = World(other_dir)
    try:
        other.store.import_items(other.p("router"), items)
        export_yaml(other.store, tmp_path / "again")
        assert load_dir(tmp_path / "again") == items
        assert other.store.verify_integrity() == []
    finally:
        other.close()


@pytest.mark.parametrize("fields", [{"note": "Legacy note"}, {"title": "Updated title"}])
def test_unrelated_edit_of_stored_decision_only_item(tmp_path, fields):
    world = World(tmp_path)
    try:
        world.store.import_items(world.p("router"), [defn(
            "LEGACY", kind="goal", review_tier="high", evidence_kinds=["decision"])])
        world.store.edit_definition(world.p("router"), "demo:LEGACY", fields)
        for name, value in fields.items():
            assert world.store._get_or_refuse("demo:LEGACY")[name] == value
        for invalid in ({"review_tier": "high"}, {"evidence_kinds": ["decision"]}):
            with pytest.raises(Refused, match="decision-only"):
                world.store.edit_definition(world.p("router"), "demo:LEGACY", invalid)
        world.store.edit_definition(world.p("router"), "demo:LEGACY", {"review_tier": "none"})
        assert world.store._get_or_refuse("demo:LEGACY")["review_tier"] == "none"
    finally:
        world.close()


@pytest.mark.parametrize("who", ["l1main", "workerA", "reader"])
def test_stored_decision_only_import_still_requires_router(tmp_path, who):
    world = World(tmp_path)
    try:
        principal = world.p(who)
        with pytest.raises(Refused) as exc:
            world.store.import_items(principal, [defn(
                "LEGACY", kind="goal", review_tier="high", evidence_kinds=["decision"])])
        assert exc.value.code == "forbidden"
        assert world.store._item("demo:LEGACY") is None
    finally:
        world.close()
