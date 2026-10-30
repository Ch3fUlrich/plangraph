"""SPAWNER-REGISTRY-ROUTE (OPERATOR D-3198): the spawner picks writers and seats itself.

* harness and tool mode are catalog columns (schema + seed + migration), not a
  hard-coded id list in the resolver;
* an override whose catalog row names a harness spawns; one without is refused;
* route skips legs by a policy file (banned / off families, paused harnesses);
* review seats cover every writer part (DECISIONS 3.2, keeper coverage rule);
* inline-only legs get the diff inlined by the spawner.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, HARNESSES, TOOL_MODES
from plangraph.planner.route_policy import RoutePolicy, load_route_policy
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.routing_support import resolver
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.catalog_data import SEED_MODELS

CARD = {"estimate_tokens": 20000}
DIFF = {"repo": "/srv/repo", "base": "a1b2", "head": "c3d4"}
RCARD = dict(CARD, diff_ref=DIFF)
HIGH = {"estimate_tokens": 20000, "risk": "high", "reviewer_count": 2, "diff_ref": DIFF}


@pytest.fixture(autouse=True)
def live_policy(monkeypatch):
    # conftest pins a neutral policy for mechanics tests; these pin the live file.
    monkeypatch.delenv("PLANGRAPH_ROUTE_POLICY", raising=False)


def _seeded():
    db = CatalogDB(":memory:")
    db.init_and_seed()
    return db


def _row(db, model_id):
    return next(r for r in resolver.get_catalog_models(db) if r["id"] == model_id)


# --- 1. harness/tool_mode are catalog data -----------------------------------

def test_schema_has_harness_and_tool_mode_columns():
    db = _seeded()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(catalog_models)")}
    assert {"harness", "tool_mode"} <= cols


def test_every_seed_row_declares_harness_and_tool_mode():
    for row in SEED_MODELS:
        assert "harness" in row and (row["harness"] is None or row["harness"] in HARNESSES), row["model_id"]
        assert row.get("tool_mode") in TOOL_MODES, row["model_id"]


@pytest.mark.parametrize("model_id, harness", [
    ("opencode-go/glm-5.2-max", "opencode"),
    ("opencode-go/qwen3.8-max", "opencode"),
    ("mimo/mimo-v2.6-pro", "opencode"),
    ("omniroute/cx-gpt-6-luna", "opencode"),
    # agy/qodercli take native ids (legs.py), not these catalog ids.
    ("agy/claude-sonnet-5-5-medium", None),
    ("qoder/qwen3.8-flash-next", None),
    ("claude/claude-haiku-4-5", None),   # no runner harness serves claude -p
])
def test_seed_harness_per_row(model_id, harness):
    assert _row(_seeded(), model_id)["harness"] == harness


def test_inline_only_rows():
    db = _seeded()
    assert _row(db, "omniroute/cx-gpt-6-luna")["tool_mode"] == "inline-only"
    assert _row(db, "mimo/mimo-v2.6-pro")["tool_mode"] == "inline-only"
    assert _row(db, "mimo/mimo-v2.6-flash")["tool_mode"] == "tools"


def test_resolver_no_longer_hardcodes_harness():
    row = resolver._normalize_catalog_row({"id": "mimo/mimo-v2.6-pro"})
    assert "harness" not in row


def test_migration_adds_columns_to_an_old_catalog(tmp_path):
    path = tmp_path / "old.db"
    db = CatalogDB(path)
    db.init_and_seed()
    db.conn.execute("ALTER TABLE catalog_models DROP COLUMN harness")
    db.conn.execute("ALTER TABLE catalog_models DROP COLUMN tool_mode")
    db.conn.commit()
    db.close()
    db = CatalogDB(path)
    db.init_and_seed()
    assert _row(db, "opencode-go/glm-5.2-max")["harness"] == "opencode"
    assert _row(db, "omniroute/cx-gpt-6-luna")["tool_mode"] == "inline-only"


def test_legs_carry_tool_mode():
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    assert by_id["cx-gpt-6-luna"].tool_mode == "inline-only"
    assert by_id["mimo-v2.6-pro"].tool_mode == "inline-only"
    assert by_id["qodercli-qwen3.8-flash"].tool_mode == "tools"


# --- 2. override with harness from the catalog spawns ------------------------

class _Runner:
    def __init__(self):
        self.jobs, self._processes, self._tasks, self.provider_responses = {}, {}, {}, {}
        self.calls = []

    async def spawn(self, **kw):
        from plangraph.spawner.runner import JobInfo
        self.calls.append(kw)
        job = JobInfo("job-%d" % len(self.calls), "dry_run" if kw.get("dry_run") else "completed",
                      kw["prompt"], kw["model_or_combo"])
        self.jobs[job.job_id] = job
        return job


def _service(tmp_path, monkeypatch, db=None):
    from plangraph.spawner.service import SpawnService
    db = db or _seeded()
    monkeypatch.setattr(resolver, "_get_default_catalog_db", lambda: db)
    return SpawnService(_Runner(), state=tmp_path)


def test_override_with_catalog_harness_spawns(tmp_path, monkeypatch):
    db = _seeded()
    db.conn.execute("UPDATE catalog_models SET available=1, status='active' WHERE model_id=?",
                    ("opencode-go/qwen3.8-max",))
    db.conn.commit()
    svc = _service(tmp_path, monkeypatch, db)
    record = asyncio.run(svc.spawn(card=CARD, prompt="task", role="implement",
                                   model="opencode-go/qwen3.8-max",
                                   override_reason="registry route test", dry_run=True))
    assert record["status"] == "dry_run"
    assert record["chosen_leg"]["harness"] == "opencode"
    assert svc.runner.calls[0]["client"] == "opencode"


def test_override_without_catalog_harness_is_refused(tmp_path, monkeypatch):
    db = _seeded()
    db.conn.execute("UPDATE catalog_models SET available=1, status='active' WHERE model_id=?",
                    ("claude/claude-haiku-4-5",))
    db.conn.commit()
    svc = _service(tmp_path, monkeypatch, db)
    with pytest.raises(SpawnRefused, match="override model missing registry data: harness"):
        asyncio.run(svc.spawn(card=CARD, prompt="task", role="review",
                              model="claude/claude-haiku-4-5",
                              override_reason="registry route test", dry_run=True))


# --- 3. policy file: banned / off / paused -----------------------------------

def test_default_policy_file():
    policy = load_route_policy()
    assert {"kimi", "deepseek"} <= set(policy.banned_families)
    assert "glm" in policy.off_families
    assert policy.allows("qodercli", "review") and not policy.allows("qodercli", "implement")
    assert not policy.allows("agy", "review") and policy.allows("agy", "implement")


def test_policy_file_validation(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"banned_families": "kimi"}))
    with pytest.raises(ValueError):
        load_route_policy(bad)
    bad.write_text(json.dumps({"surprise": []}))
    with pytest.raises(ValueError):
        load_route_policy(bad)


def test_policy_env_path(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"banned_families": [], "off_families": [], "paused_harnesses": {}}))
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    plan = plan_spawn(CARD, role="review", writer_families=("mimo",))
    assert "deepseek" in [leg.family for leg in plan.legs]


@pytest.mark.parametrize("role", ["review", "implement"])
def test_route_skips_banned_and_off_families(role):
    plan = plan_spawn(CARD, role=role, writer_families=("mimo",))
    families = {leg.family for leg in plan.legs}
    assert not families & {"kimi", "deepseek", "glm"}
    reasons = {s["leg"]: s["reason"] for s in plan.skipped}
    assert reasons["opencode-deepseek-v4.1-flash-free"] == "banned_family"
    assert reasons["opencode-glm-5.3-flash-free"] == "off_family"


def test_route_skips_paused_harness_per_role():
    review = plan_spawn(CARD, role="review", writer_families=("mimo",))
    ids = [leg.id for leg in review.legs]
    assert "qodercli-qwen3.8-flash" in ids
    assert "agy-gemini-3.8-flash" not in ids
    assert {s["leg"]: s["reason"] for s in review.skipped}["agy-gemini-3.8-flash"] == "paused_harness"
    writer = plan_spawn(CARD, role="implement")
    assert writer.first_leg.id == "agy-gemini-3.8-flash"


def test_explicit_policy_argument_wins():
    open_policy = RoutePolicy(banned_families=(), off_families=(), paused_harnesses={})
    plan = plan_spawn(CARD, role="review", writer_families=("mimo",), policy=open_policy)
    assert "opencode-glm-5.3-flash-free" in [leg.id for leg in plan.legs]


# --- 4. seats per writer part --------------------------------------------------

def _families(plan):
    return [leg.family for leg in plan.review_seats]


def test_claude_written_high_gets_luna_and_qwen():
    plan = plan_spawn(HIGH, role="review", writer_families=("claude",))
    assert plan.error is None
    assert {leg.id for leg in plan.review_seats} == {"qodercli-qwen3.8-flash", "cx-gpt-6-luna"}


def test_seat_never_shares_family_with_a_single_writer():
    plan = plan_spawn(RCARD, role="review", writer_families=("gpt",))
    assert "gpt" not in _families(plan)
    assert any(s["leg"] == "cx-gpt-6-luna" and s["reason"] == "excluded_family" for s in plan.skipped)


def test_seat_may_share_family_with_another_writer_part():
    # D-3084: writers gpt + qwen; a qwen seat covers the gpt part, a gpt seat
    # covers the qwen part.  Every part is covered by a family other than it.
    from plangraph.keeper.coverage import uncovered
    plan = plan_spawn(RCARD, role="review", writer_families=("gpt", "qwen"))
    assert plan.error is None
    assert set(_families(plan)) == {"qwen", "gpt"}
    assert uncovered(["gpt", "qwen"], _families(plan), need=1) == []
    assert plan.seat_coverage == ({"leg": "qodercli-qwen3.8-flash", "covers": ["gpt"]},
                                  {"leg": "cx-gpt-6-luna", "covers": ["qwen"]})


def test_high_needs_two_families_per_part_incl_non_claude():
    from plangraph.keeper.coverage import uncovered
    plan = plan_spawn(HIGH, role="review", writer_families=("gpt", "qwen"))
    assert plan.error is None
    seats = [("anthropic" if f == "claude" else f) for f in _families(plan)]
    assert uncovered(["gpt", "qwen"], seats, need=2, sensitive=True) == []
    assert len(set(seats)) >= 3


def test_uncoverable_parts_are_an_error():
    only = RoutePolicy(banned_families=("deepseek", "glm", "gpt", "mimo", "gemini", "muse",
                                        "north", "laguna", "nvidia", "openai", "openrouter", "kimi"),
                       off_families=(), paused_harnesses={})
    plan = plan_spawn(HIGH, role="review", writer_families=("claude",), policy=only)
    assert plan.error and "qwen" in _families(plan)


def test_seat_coverage_in_route_dict():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    data = plan.to_dict()
    # keeper family names: every Claude hand is "anthropic" (keeper/coverage.py)
    assert data["seat_coverage"] and all(c["covers"] == ["anthropic"] for c in data["seat_coverage"])
    json.dumps(data)


# --- 5. inline-only legs get the diff ----------------------------------------

def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two\n")
    run("commit", "-qam", "head")
    head = run("rev-parse", "HEAD").stdout.strip()
    return repo, base, head


def test_inline_only_leg_skipped_without_diff_ref():
    plan = plan_spawn(CARD, role="review", writer_families=("claude",))
    assert "cx-gpt-6-luna" not in [leg.id for leg in plan.review_seats]
    assert {s["leg"]: s["reason"] for s in plan.skipped}["cx-gpt-6-luna"] == "inline_only_needs_diff"


@pytest.mark.parametrize("ref", [
    "x", {"repo": "rel", "base": "a", "head": "b"},
    {"repo": "/r", "base": "--output=/tmp/x", "head": "b"}, {"repo": "/r", "base": "a"}])
def test_bad_diff_ref_refused(ref):
    with pytest.raises(SpawnRefused, match="diff_ref"):
        plan_spawn(dict(CARD, diff_ref=ref), role="review")


def test_inline_only_seat_gets_diff_inlined(tmp_path, monkeypatch):
    repo, base, head = _repo(tmp_path)
    card = dict(CARD, diff_ref={"repo": str(repo), "base": base, "head": head})
    svc = _service(tmp_path / "state", monkeypatch)
    out = asyncio.run(svc.spawn(card=card, prompt="review this", role="review",
                                writer_families=("claude",), seats=True, dry_run=True))
    by_model = {c["model_or_combo"]: c["prompt"] for c in svc.runner.calls}
    luna = by_model["omniroute/cx-gpt-6-luna"]
    assert luna.startswith("review this") and "-one\n+two" in luna and "INLINE DIFF" in luna
    assert "-one" not in by_model["Qwen3.8-Flash"]
    assert len(out["seats"]) == 2
    assert {r["chosen_leg"]["family"] for r in out["seats"]} == {"qwen", "gpt"}


def test_mcp_dispatch_seats_inline_diff(tmp_path, monkeypatch):
    # The production path (runner.spawn_service DispatchService) via MCP.
    from plangraph.spawner.mcp import MCPSpawnerServer
    repo, base, head = _repo(tmp_path)
    runner = _Runner()
    server = MCPSpawnerServer(runner, state=tmp_path / "state")
    card = dict(HIGH, diff_ref={"repo": str(repo), "base": base, "head": head})
    out = asyncio.run(server.call_tool("plangraph_agent_spawn", {
        "card": card, "prompt": "review", "role": "review", "writer_families": ["claude"],
        "seats": True, "dry_run": True}))
    # The benchmark may permute luna/mimo-pro inside the subscription tier.
    chosen = {r["chosen_leg"]["id"]: r["chosen_leg"] for r in out["seats"]}
    assert "qodercli-qwen3.8-flash" in chosen and len(chosen) == 2
    inline = next(leg for leg in chosen.values() if leg["tool_mode"] == "inline-only")
    prompts = {c["model_or_combo"]: c["prompt"] for c in runner.calls}
    assert "+two" in prompts[inline["model"]] and "+two" not in prompts["Qwen3.8-Flash"]
    assert sorted(c["leg"] for c in out["seat_coverage"]) == sorted(chosen)
    assert all(c["covers"] == ["anthropic"] for c in out["seat_coverage"])


def test_inline_diff_failure_falls_to_next_leg(tmp_path, monkeypatch):
    from plangraph.spawner.mcp import MCPSpawnerServer
    runner = _Runner()
    server = MCPSpawnerServer(runner, state=tmp_path / "state")
    card = dict(HIGH, diff_ref={"repo": str(tmp_path / "nope"), "base": "a1", "head": "b2"})
    out = asyncio.run(server.call_tool("plangraph_agent_spawn", {
        "card": card, "prompt": "review", "role": "review", "writer_families": ["claude"],
        "seats": True, "dry_run": True}))
    inline_run = next(r for r in out["seats"] if r["plan"]["legs"][0]["tool_mode"] == "inline-only")
    first = inline_run["attempts"][0]
    assert first["status"] == "skipped" and first["skip_reason"].startswith("inline_diff_failed")
    inline_model = inline_run["plan"]["legs"][0]["model"]
    assert inline_model not in [c["model_or_combo"] for c in runner.calls]


def test_seats_flag_needs_review_without_model(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    with pytest.raises(SpawnRefused, match="seats"):
        asyncio.run(svc.spawn(card=CARD, prompt="x", role="implement", seats=True, dry_run=True))


def test_mcp_spawn_schema_has_seats():
    from plangraph.spawner.mcp import TOOLS_METADATA
    spawn = next(t for t in TOOLS_METADATA if t["name"] == "plangraph_agent_spawn")
    assert spawn["inputSchema"]["properties"]["seats"]["type"] == "boolean"
