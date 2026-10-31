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
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, HARNESSES, TOOL_MODES
from plangraph.planner.route_policy import RoutePolicy, load_route_policy
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.routing_support import resolver
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.catalog_data import SEED_MODELS

from registry_route_util import (CARD, DIFF, HIGH, RCARD, _Runner, _families, _repo, _row, _seeded, _service, live_policy)  # noqa: F401


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
        asyncio.run(svc.spawn(card=CARD, prompt="task", role="review", writer_families=("gpt",),
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
    assert writer.first_leg.id == "openrouter-free"


def test_explicit_policy_argument_wins():
    open_policy = RoutePolicy(banned_families=(), off_families=(), paused_harnesses={})
    plan = plan_spawn(CARD, role="review", writer_families=("mimo",), policy=open_policy)
    assert "opencode-glm-5.3-flash-free" in [leg.id for leg in plan.legs]


# --- 4. seats per writer part --------------------------------------------------

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
    # Coverage names the parts as the keeper does (gpt = openai, qwen = alibaba).
    assert plan.seat_coverage == ({"leg": "qodercli-qwen3.8-flash", "covers": ["openai"]},
                                  {"leg": "cx-gpt-6-luna", "covers": ["alibaba"]})


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


def test_import_json_derives_registry_columns():
    db = _seeded()
    row = dict(next(r for r in SEED_MODELS if r["model_id"] == "omniroute/cx-gpt-6-luna"))
    row.pop("harness"), row.pop("tool_mode")
    db.conn.execute("DELETE FROM catalog_models WHERE model_id=?", (row["model_id"],))
    db.import_json({"models": [row]})
    got = _row(db, "omniroute/cx-gpt-6-luna")
    assert got["harness"] == "opencode" and got["tool_mode"] == "inline-only"


def test_fallback_rows_carry_tool_mode():
    by_id = {r["id"]: r for r in resolver.DEFAULT_FALLBACK_MODELS}
    assert all(r["tool_mode"] in TOOL_MODES for r in by_id.values())
    assert by_id["mimo/mimo-v2.6-pro"]["tool_mode"] == "inline-only"
