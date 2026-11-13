"""Merged parts do not finish an item while a named lane remains open."""
import pytest

from test_keeper_claim_guard_uncached import scenario
from plangraph.keeper import context_sources
from plangraph.keeper.errors import Refused


MERGED = ("already merged: Plan-Item commit abcdef12 (2026-11-06) via PR #42"
          " is on the default branch; attach evidence/submit instead")


@pytest.fixture
def multipart(scenario, monkeypatch):
    world, path, _ = scenario
    monkeypatch.setattr(context_sources, "plan_item_commits", lambda *a, **kw: [
        {"sha": "abcdef123456", "date": "2026-11-06", "merge": {"pr": 42}}])
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: [])
    return world, path


def test_open_part_allows_claim(multipart, monkeypatch):
    world, _ = multipart
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: ["lane/a-part2"])
    item = world.store._item("demo:A")
    before = world.store._task_context(item, {"demo:A": item})
    assert "already merged abcdef12" in before["stale"]
    world.store.claim(world.p("l1main"), "demo:A")
    assert world.store._item("demo:A")["state"] == "claimed"


@pytest.mark.parametrize("lanes", [[], None], ids=["no-open-part", "unknown-lanes"])
def test_merged_without_known_open_part_refuses(multipart, monkeypatch, lanes):
    world, _ = multipart
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: lanes)
    with pytest.raises(Refused) as exc:
        world.store.claim(world.p("l1main"), "demo:A")
    assert exc.value.code == "bad_state"
    assert str(exc.value) == ("bad_state: demo:A stale: " + MERGED
                             + " (override: force_stale=<reason>, audited)")
    assert world.store._item("demo:A")["state"] == "open"


def test_open_part_still_refuses_newer_decision(multipart, monkeypatch):
    world, path = multipart
    monkeypatch.setattr(context_sources, "open_lanes", lambda *a: ["lane/a-part2"])
    path.write_text(path.read_text().replace("demo:B", "demo:A"))
    with pytest.raises(Refused) as exc:
        world.store.claim(world.p("l1main"), "demo:A")
    assert exc.value.code == "bad_state"
    assert "premise outdated: D-9" in str(exc.value)
    assert "already merged" not in str(exc.value)
    assert world.store._item("demo:A")["state"] == "open"


def test_force_stale_without_open_lane(multipart):
    world, _ = multipart
    item = world.store._item("demo:A")
    assert world.store._claim_guard(item, " checked ") == {
        "reason": "checked", "overrode": [MERGED]}
    world.store.claim(world.p("l1main"), "demo:A", force_stale="checked")
    assert world.store._item("demo:A")["state"] == "claimed"


def test_ready_excludes_merged_and_outdated(multipart):
    world, path = multipart
    assert world.store._ready_items(n=None) == []
    rows = world.store._ready_items(n=None, needs_reconcile=True)
    assert [r["key"] for r in rows] == ["demo:A"]
    assert "already merged" in rows[0]["reconcile_reason"]
    path.write_text(path.read_text().replace("demo:B", "demo:A"))
    assert "premise outdated" in world.store._ready_items(
        n=None, needs_reconcile=True)[0]["reconcile_reason"]


def test_ready_open_pr_is_in_progress(multipart, monkeypatch):
    world, _ = multipart
    monkeypatch.setattr(world.store, "_work", lambda *a, **kw: {
        "commits": [], "lanes": [], "prs": [42]})
    assert world.store._ready_items(n=None) == []
    rows = world.store._ready_items(n=None, in_progress=True)
    assert rows[0]["prs"] == [42]


def test_ready_and_claim_share_one_repository_index(scenario, monkeypatch):
    import json
    from types import SimpleNamespace
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper import store_context
    world, _, _ = scenario
    world.store.ctx.git = GitAdapter({"demo": {"path": "/fixture", "ref": "origin/main"}})
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps({"items": {"demo:A": {
            "commits": [], "prs": [42], "lanes": []}}}))
    monkeypatch.setattr(store_context.subprocess, "run", run)
    assert world.store._ready_items(n=None) == []
    assert world.store._ready_items(in_progress=True)[0]["prs"] == [42]
    with pytest.raises(Refused, match="in progress PR"):
        world.store.claim(world.p("l1main"), "demo:A")
    assert len(calls) == 1


def test_local_ref_uses_cached_index_and_bad_shape_is_unknown(scenario, monkeypatch):
    from types import SimpleNamespace
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper import store_context
    world, _, _ = scenario
    world.store.ctx.git = GitAdapter({"demo": {"path": "/fixture", "ref": "main"}})
    monkeypatch.setattr(store_context.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout='{"items": []}'))
    rows = world.store._ready_items(n=None, needs_reconcile=True)
    assert rows[0]["reconcile_reason"] == "repository index unavailable"


def test_ready_checks_candidates_beyond_the_old_limit(tmp_path, monkeypatch):
    from keeper_util import World, defn
    world = World(tmp_path, backend="sqlite")
    try:
        keys = [f"K-{n:02}" for n in range(30)]
        world.store.create_items(world.p("router"), [*(defn(k) for k in keys),
            defn("GOAL", keys, kind="goal")])
        monkeypatch.setattr(world.store, "_work", lambda item, *args, **kw: {
            "commits": [{"sha":"a"*40,"date":"2026-11-13"}] if item["id"] == "K-29" else [],
            "lanes": [], "prs": []})
        rows = world.store._ready_items(n=None)
        assert len(rows) == 29
        assert "demo:K-29" not in {r["key"] for r in rows}
    finally:
        world.close()
