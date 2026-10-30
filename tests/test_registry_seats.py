"""SPAWNER-REGISTRY-ROUTE seat spawns (split from test_registry_route.py, D-3198).

* inline-only legs get the diff inlined by the spawner;
* round-1 seat findings (luna run-7ebcab85, qwen run-3654dec2).
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


# --- 5. inline-only legs get the diff ----------------------------------------

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


# --- 6. round-1 seat findings (luna run-7ebcab85, qwen run-3654dec2) -----------

def test_writer_spelled_anthropic_excludes_claude_legs():
    open_policy = RoutePolicy(banned_families=(), off_families=(), paused_harnesses={})
    as_keeper = plan_spawn(RCARD, role="review", writer_families=("anthropic",), policy=open_policy)
    as_leg = plan_spawn(RCARD, role="review", writer_families=("claude",), policy=open_policy)
    assert "claude" not in _families(as_keeper)
    assert as_keeper.review_seats == as_leg.review_seats and as_keeper.skipped == as_leg.skipped


@pytest.mark.parametrize("card", [CARD, RCARD], ids=["no-diff", "diff"])
def test_live_policy_every_writer_family_routes_or_names_why(card):
    # Each family a writer can have gets seats, or one named refusal (never a crash).
    # A family the keeper cannot resolve (writer_key) refuses, fail closed, like the keeper.
    from plangraph.review.family import FamilyError, writer_key
    for fam in sorted({leg.family for leg in DEFAULT_LEGS if "implement" in leg.roles}):
        try:
            writer_key(fam)
        except FamilyError:
            with pytest.raises(SpawnRefused, match="unknown writer family"):
                plan_spawn(card, role="review", writer_families=(fam,))
            continue
        plan = plan_spawn(card, role="review", writer_families=(fam,))
        assert plan.error is None or plan.error.startswith(
            ("two distinct review families unavailable", "seat coverage unmet")), (fam, plan.error)
        assert fam not in _families(plan) or plan.error


def test_inline_diff_size_cap(tmp_path):
    from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt
    repo, base, head = _repo(tmp_path)
    ref = {"repo": str(repo), "base": base, "head": head}
    assert "+two" in inline_diff_prompt("p", ref, max_chars=10_000)
    with pytest.raises(InlineDiffError, match="too large|exceeds|cap"):
        inline_diff_prompt("p", ref, max_chars=10)


def test_seat_records_carry_own_coverage_and_group(tmp_path, monkeypatch):
    repo, base, head = _repo(tmp_path)
    card = dict(HIGH, diff_ref={"repo": str(repo), "base": base, "head": head})
    svc = _service(tmp_path / "state", monkeypatch)
    out = asyncio.run(svc.spawn(card=card, prompt="r", role="review", writer_families=("claude",),
                                seats=True, dry_run=True, review_scope="batch"))
    group = out["seat_group"]
    for run in out["seats"]:
        (own,) = run["plan"]["seat_coverage"]
        assert own["leg"] == run["chosen_leg"]["id"] and own["seat_group"] == group
    saved = json.loads((tmp_path / "state" / "seat-groups" / (group + ".json")).read_text())
    assert saved["runs"] == [r["run_id"] for r in out["seats"]]


def test_one_seat_refusal_keeps_the_other_seat(tmp_path, monkeypatch):
    from plangraph.spawner.service import SpawnService
    repo, base, head = _repo(tmp_path)
    card = dict(HIGH, diff_ref={"repo": str(repo), "base": base, "head": head})
    svc = _service(tmp_path / "state", monkeypatch)
    original = SpawnService._spawn

    async def flaky(self, *a, _plan=None, **kw):
        if _plan is not None and _plan.legs[0].family == "qwen":
            raise SpawnRefused("seat refused in test")
        return await original(self, *a, _plan=_plan, **kw)
    monkeypatch.setattr(SpawnService, "_spawn", flaky)
    out = asyncio.run(svc.spawn(card=card, prompt="r", role="review", writer_families=("claude",),
                                seats=True, dry_run=True))
    assert sorted(r["status"] for r in out["seats"]) == ["dry_run", "refused"]
    refused = next(r for r in out["seats"] if r["status"] == "refused")
    assert refused["error"] == "seat refused in test"
    saved = svc.result(refused["run_id"])
    assert saved["status"] == "refused"
    # Round-2 luna LOW: the refused seat keeps its group id and its own coverage entry.
    (own,) = saved["plan"]["seat_coverage"]
    assert own["seat_group"] == out["seat_group"] and own["leg"] == saved["seat_leg"]
    assert own["leg"] in {c["leg"] for c in out["seat_coverage"]}


def _dispatch(tmp_path, monkeypatch, runner):
    from plangraph.spawner.mcp import MCPSpawnerServer

    async def admit_all(plan, dry_run=False):
        return plan
    monkeypatch.setattr(resolver, "plan_for_launch_async", admit_all)
    return MCPSpawnerServer(runner, state=tmp_path / "state")


class _StopRunner(_Runner):
    """Every launched leg answers with a provider stop (rate limit)."""
    async def spawn(self, **kw):
        job = await super().spawn(**kw)
        job.exit_code = 0
        self.provider_responses[job.job_id] = {"status": 429, "body": "rate limited"}
        return job


def test_seat_reroute_stays_on_its_own_legs(tmp_path, monkeypatch):
    repo, base, head = _repo(tmp_path)
    runner = _StopRunner()
    server = _dispatch(tmp_path, monkeypatch, runner)
    card = dict(HIGH, diff_ref={"repo": str(repo), "base": base, "head": head})
    out = asyncio.run(server.call_tool("plangraph_agent_spawn", {
        "card": card, "prompt": "review", "role": "review", "writer_families": ["claude"],
        "seats": True, "wait_seconds": None}))
    for run in out["seats"]:
        record = server.service.result(run["run_id"])
        own = {leg["model"] for leg in run["plan"]["legs"]}
        launched = [a["leg"]["model"] for a in record["attempts"]
                    if a["status"] != "skipped" and isinstance(a.get("leg"), dict)]
        assert launched and set(launched) <= own, (own, launched)
    models = [c["model_or_combo"] for c in runner.calls]
    assert len(models) == len(set(models))   # no two seats share a launched leg


def test_inline_failure_does_not_decide_a_launched_run(tmp_path, monkeypatch):
    # luna MED: an inline_diff_failed skip must not mark a run failed when a later
    # leg launched and the run ended on route exhaustion.
    from plangraph.planner.routing import SpawnPlan
    runner = _StopRunner()
    svc = _dispatch(tmp_path, monkeypatch, runner).service
    card = dict(HIGH, diff_ref={"repo": str(tmp_path / "nope"), "base": "a1", "head": "b2"})
    seat_plan = plan_spawn(card, role="review", writer_families=("claude",))
    inline = next(l for l in seat_plan.legs if l.tool_mode == "inline-only")
    tools = next(l for l in seat_plan.legs if l.tool_mode == "tools")
    monkeypatch.setattr(svc, "_route", lambda *a, **k: SpawnPlan((), (), "operator-order", (), (), "all limited"))
    record = asyncio.run(svc._spawn(card=card, prompt="r", role="review", writer_families=("claude",),
                                    _plan=replace(seat_plan, legs=(inline, tools)), wait_seconds=None))
    record = svc.result(record["run_id"])
    assert record["attempts"][0]["skip_reason"].startswith("inline_diff_failed")
    assert record["status"] == "limited", record.get("error")


def test_all_legs_inline_failed_reports_reasons(tmp_path, monkeypatch):
    runner = _StopRunner()
    svc = _dispatch(tmp_path, monkeypatch, runner).service
    card = dict(HIGH, diff_ref={"repo": str(tmp_path / "nope"), "base": "a1", "head": "b2"})
    seat_plan = plan_spawn(card, role="review", writer_families=("claude",))
    inline = tuple(l for l in seat_plan.legs if l.tool_mode == "inline-only")
    record = asyncio.run(svc._spawn(card=card, prompt="r", role="review", writer_families=("claude",),
                                    _plan=replace(seat_plan, legs=inline), wait_seconds=None))
    record = svc.result(record["run_id"])
    assert record["status"] == "failed" and "inline_diff_failed" in record["error"]
    assert runner.calls == []


# --- 7. round-2 seat findings (luna run-a0f16445, qwen run-e7758d82) -----------

def _leg(leg_id):
    return next(l for l in DEFAULT_LEGS if l.id == leg_id)


def test_spares_use_keeper_families(tmp_path, monkeypatch):
    # luna HIGH: gpt and openai legs are one keeper family; a gpt seat's family is no spare.
    svc = _service(tmp_path / "state", monkeypatch)
    luna, qwen, oss, mimo = map(_leg, ("cx-gpt-6-luna", "qodercli-qwen3.8-flash",
                                       "ovh-gpt-oss-120b", "mimo-v2.6-flash"))
    base = plan_spawn(HIGH, role="review", writer_families=("claude",))
    plan = replace(base, legs=(luna, qwen, oss, mimo), review_seats=(luna, qwen), error=None)
    monkeypatch.setattr(svc, "_route", lambda *a, **k: plan)
    out = asyncio.run(svc.spawn(card=HIGH, prompt="r", role="review", writer_families=("claude",),
                                seats=True, dry_run=True))
    legs = [leg["id"] for run in out["seats"] for leg in run["plan"]["legs"]]
    assert "ovh-gpt-oss-120b" not in legs and legs.count("mimo-v2.6-flash") == 1


@pytest.mark.parametrize("writer", ["alibaba", " OpenAI ", "anthropic", "QWEN"])
def test_keeper_writer_names_exclude_their_legs(writer):
    # qwen MED/LOW: keeper family names and case/space variants map to leg families.
    from plangraph.review.family import writer_key
    canon = writer_key(writer)
    plan = plan_spawn(HIGH, role="review", writer_families=(writer,))
    assert plan.review_seats and canon not in {writer_key(l.family) for l in plan.review_seats}
    assert all(c["covers"] == [canon] for c in plan.seat_coverage)


def test_policy_families_match_keeper_names():
    # Pattern sweep of the qwen MED: a policy may name the keeper family.
    glm = _leg("opencode-glm-5.3-flash-free")
    assert RoutePolicy(banned_families=("zhipu",)).skip_reason(glm, "review") == "banned_family"
    assert RoutePolicy(off_families=("ZhiPu",)).skip_reason(glm, "review") == "off_family"
    assert RoutePolicy(off_families=("glm",)).skip_reason(_leg("mimo-v2.6-flash"), "review") is None


def test_unknown_writer_family_is_refused():
    with pytest.raises(SpawnRefused, match="unknown writer family"):
        plan_spawn(HIGH, role="review", writer_families=("nosuchfamily",))


@pytest.mark.parametrize("model", [None, "omniroute/cx-gpt-6-luna"])
def test_review_without_writer_family_is_refused(tmp_path, monkeypatch, model):
    # Round-3 qwen MED: no writer named = no family excluded; refuse, fail closed.
    svc = _service(tmp_path / "state", monkeypatch)
    with pytest.raises(SpawnRefused, match="writer family"):
        asyncio.run(svc.spawn(card=RCARD | HIGH, prompt="r", role="review", model=model,
                              override_reason="registry seat test" if model else None, dry_run=True))
    with pytest.raises(SpawnRefused, match="writer family"):
        svc.route(RCARD | HIGH, role="review")


def test_derive_keeps_an_operator_tool_mode():
    # qwen LOW: derive() fills only rows that lack tool_mode; an explicit value stays.
    from plangraph import registry_columns
    db = _seeded()
    db.conn.execute("UPDATE catalog_models SET tool_mode='tools' WHERE model_id='omniroute/cx-gpt-6-luna'")
    registry_columns.derive(db.conn)
    assert _row(db, "omniroute/cx-gpt-6-luna")["tool_mode"] == "tools"


def test_base_exception_seat_still_saves_the_group(tmp_path, monkeypatch):
    # qwen LOW: a seat raising a BaseException re-raises only after the group file is written.
    from plangraph.spawner.service import SpawnService

    class Stop(BaseException):
        pass
    svc = _service(tmp_path / "state", monkeypatch)
    original = SpawnService._spawn

    async def flaky(self, *a, _plan=None, **kw):
        if _plan is not None and _plan.legs[0].family == "qwen":
            raise Stop()
        return await original(self, *a, _plan=_plan, **kw)
    monkeypatch.setattr(SpawnService, "_spawn", flaky)
    with pytest.raises(Stop):
        asyncio.run(svc.spawn(card=RCARD | HIGH, prompt="r", role="review", writer_families=("claude",),
                              seats=True, dry_run=True))
    (saved,) = [json.loads(p.read_text()) for p in (tmp_path / "state" / "seat-groups").iterdir()]
    assert len(saved["runs"]) == 1
