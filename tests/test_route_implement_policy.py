"""Implement-only writer admission, family fences and visible operator refusals."""
import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.cli import main

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")
CARD = {"estimate_tokens": 20000, "risk_tags": [],
        "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}
WRITERS = ["cx-gpt-6.1-sol-medium", "agy-gemini-3.8-flash", "openrouter-free",
           "opencode-big-pickle", "agy-gemini-4-argon", "mimo-v2.6-flash", "mimo-v2.6-pro",
           "omniroute-deepseek-flash", "omniroute-muse-spark", "deepseek"]
LIVE = [leg for leg in WRITERS if leg != "agy-gemini-3.8-flash"]  # D-5696: fenced on the live route


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, tmp_path):
    monkeypatch.setattr('plangraph.spawner.seat_spawn.admit', lambda *a, **k: None)
    # Routing tests use a fake fresh checkout; never inspect the real .git.
    monkeypatch.setattr('plangraph.spawner.base_fresh._check', lambda *a, **k:
                       dict(ok=True, origin_sha='a' * 40, base_sha='a' * 40, behind=0, reason=None))
    from plangraph.routing_support import resolver, catalog_db
    async def admit(*args, **kwargs):
        return True
    monkeypatch.setattr('plangraph.spawner.service.admit_base', admit)
    monkeypatch.setattr('plangraph.spawner.service.SpawnService._annotate_spawn', lambda *a, **k: None)
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [])
    monkeypatch.setattr(catalog_db, "DEFAULT_DB_PATH", tmp_path / "absent.db")
    monkeypatch.setenv("PLANGRAPH_CATALOG_DB", str(tmp_path / "absent.db"))


@pytest.mark.parametrize("card", [CARD, {"estimate_tokens": 1000}, {"estimate_tokens": 20000},
    {"estimate_tokens": 20000, "risk_tags": []}, {"estimate_tokens": 20000, "risk_tags": ["secrets"]}])
@pytest.mark.parametrize("state", ["available", "limited", "down", "expired"])
def test_implement_defaults_effort_and_fallback_without_override(tmp_path, card, state):
    from plangraph.spawner.service import SpawnService
    def reader(model, **kwargs):
        return {"state": state if model == "omniroute/cx-gpt-6.1-sol-medium" else "available", "evidence": None}
    service = SpawnService(state=tmp_path, leg_reader=reader)
    plan = service.route(card, role="implement")
    expected = LIVE if state == "available" else LIVE[1:]
    if card.get("risk_tags"):
        expected = [lid for lid in expected if not next(l for l in DEFAULT_LEGS if l.id == lid).trains_on_data]
    assert [leg.id for leg in plan.legs] == expected
    assert all(not leg.claude_family and leg.family not in ("claude", "anthropic")
               and not any(name in leg.model.lower() for name in ("claude", "fable", "opus", "sonnet", "haiku"))
               for leg in plan.legs)
    chosen = next(l for l in plan.legs if l.effort not in ('high', 'xhigh', 'max'))
    record = asyncio.run(service.spawn(card=card, prompt="Implement public code", dry_run=True))
    assert record["chosen_leg"]["id"] == chosen.id
    assert record["chosen_leg"]["effort"] == chosen.effort
    assert not (tmp_path / "spawn-overrides.jsonl").exists()


def test_implement_family_exclusion_over_all_catalog_rows():
    from plangraph.catalog_data import SEED_MODELS
    from plangraph.planner.routing import route_card
    from plangraph.routing_support.resolver import DEFAULT_FALLBACK_MODELS
    template, fallback = (next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
                          for leg_id in WRITERS[:2])
    for row in [*SEED_MODELS, *DEFAULT_FALLBACK_MODELS]:
        # Make every catalog row otherwise eligible, including inactive rows.
        model = row.get("model_id", row.get("id"))
        candidate = dataclasses.replace(template, id="catalog:" + model, model=model,
                                        family=row["family"], claude_family=False)
        route = route_card(CARD, legs=(candidate, fallback), role="implement")
        assert (candidate in route.legs) == (row["family"] != "claude"), model


@pytest.mark.parametrize("alias", ["claude", "anthropic", "fable", "opus", "sonnet", "haiku"])
@pytest.mark.parametrize("source", ["family", "model"])
@pytest.mark.parametrize("effort", ["", "-5.2-high"])
def test_implement_claude_aliases_use_canonical_family(monkeypatch, alias, source, effort):
    from plangraph.planner.route_policy import RoutePolicy, implement_model_family
    from plangraph.planner.routing import route_card
    from plangraph.routing_support import resolver
    template, fallback = (next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
                          for leg_id in WRITERS[:2])
    fields = dict(id="catalog-writer", model="catalog/opaque-model",
                  family="distinct-family", claude_family=False)
    fields[source] = alias.upper() + effort if source == "family" else "catalog/" + alias + "-next" + effort
    candidate = dataclasses.replace(template, **fields)
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [
        {"id": candidate.model, "available": 1, "active": True}])
    # Family aliases and model identities both work without the legacy flag.
    assert implement_model_family(fields[source]) == "claude"
    assert route_card(CARD, legs=(candidate, fallback), role="implement").legs == (fallback,)
    assert route_card(dict(CARD, role="implement"), legs=(candidate, fallback)).legs == (fallback,)
    plan = plan_spawn(CARD, role="implement", legs=(candidate, fallback), policy=RoutePolicy(), catalog_rows=None,
                      leg_reader=lambda *a, **kw: {"state": "available", "evidence": None})
    assert plan.legs == (fallback,)
    assert {skip["leg"]: skip["reason"] for skip in plan.skipped}[candidate.id] == "excluded_family"


@pytest.mark.parametrize("identity", ["id", "harness", "flag", "unknown"])
def test_implement_renamed_operator_claude_fails_closed(tmp_path, monkeypatch, capsys, identity):
    from plangraph.planner.legs import load_legs
    from plangraph.planner.route_policy import RoutePolicy
    from plangraph.planner.routing import RouteError, route_card
    template, fallback = (next(leg for leg in DEFAULT_LEGS if leg.id == leg_id)
                          for leg_id in LIVE[:2])
    row = dict(dataclasses.asdict(template), id="operator-writer", model="provider/some-model",
               family="unidentified-high" if identity == "unknown" else "gpt", claude_family=identity == "flag",
               harness="claude" if identity == "harness" else "opencode")
    if identity == "id":
        row["id"] = "fable-5.2-high"
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [row]}))
    candidate, = load_legs(path)
    assert route_card(CARD, legs=(candidate,), role="review").legs == (candidate,)
    for card, role in ((CARD, "implement"), (dict(CARD, role="implement"), None)):
        assert route_card(card, legs=(candidate, fallback), role=role).legs == (fallback,)
        with pytest.raises(RouteError) as exc:
            route_card(card, legs=(candidate,), role=role)
        assert exc.value.exclusion_reasons == {"excluded_family": 1}
    plan = plan_spawn(CARD, role="implement", legs=(candidate, fallback), policy=RoutePolicy(),
                      catalog_rows=({"id": candidate.model, "available": 1, "active": True},),
                      leg_reader=lambda *a, **kw: {"state": "available", "evidence": None})
    assert plan.legs == (fallback,)
    assert {skip["leg"]: skip["reason"] for skip in plan.skipped}[candidate.id] == "excluded_family"
    # The operator must see both a partial-route skip and an exhausted-route error.
    from plangraph.planner import legs, routing
    from plangraph.routing_support import resolver
    monkeypatch.setattr(resolver, "get_catalog_models", lambda: [
        {"id": candidate.model, "available": 1, "active": True}])
    monkeypatch.setattr("plangraph.planner.route_policy.load_route_policy", lambda: RoutePolicy())
    for table, code in (((candidate, fallback), 0), ((candidate,), 2)):
        monkeypatch.setattr(legs, "DEFAULT_LEGS", table)
        monkeypatch.setitem(routing.plan_spawn.__kwdefaults__, "legs", table)
        assert main(["--state", str(tmp_path), "route", "--role", "implement",
                     "--card", json.dumps(CARD), "--json"]) == code
        output = capsys.readouterr()
        assert f"skip {candidate.id}: excluded_family" in output.err
        if code == 2:
            assert not output.out
            assert "no eligible leg" in output.err
            continue
        result = json.loads(output.out)
        skip, = [row for row in result["skipped"] if row["leg"] == candidate.id]
        assert skip["reason"] == "excluded_family"
        assert "excluded_family" in skip["evidence"]["error"]
        detail, = [row for row in result["candidates"] if row["leg"] == candidate.id]
        assert detail["reason"] == "excluded_family"
        assert result["error"] is None
        assert [row["id"] for row in result["legs"]] == [fallback.id]


@pytest.mark.parametrize("alias", ["anthropic", "fable"])
def test_implement_alias_detection_does_not_change_shared_resolver(alias):
    from plangraph.routing_support.resolver import detect_model_family
    assert detect_model_family(alias) == alias
    assert detect_model_family("catalog/" + alias + "-next-high") == "catalog"


@pytest.mark.parametrize("family", ["pickle", "claude", "fable"])
def test_registered_model_proves_family_without_lifting_claude_aliases(family):
    from plangraph.planner.routing import route_card
    template = next(leg for leg in DEFAULT_LEGS if leg.id == "opencode-big-pickle")
    fallback = next(leg for leg in DEFAULT_LEGS if leg.id == WRITERS[1])
    candidate = dataclasses.replace(template, id="renamed", family=family, roles=("implement",))
    route = route_card(CARD, legs=(candidate, fallback), role="implement")
    assert (candidate in route.legs) == (family == "pickle")


@pytest.mark.parametrize("refusal", ['{"type":"error","error":{"status":402}}',
    '{"type":"error","error":{"status":429}}', 'not_served_on_host: refused'])
def test_default_writer_refusal_falls_through_to_agy(tmp_path, refusal):
    from types import SimpleNamespace as NS
    from plangraph.spawner.service import SpawnService
    runner = NS(_processes={}, _tasks={}, provider_responses={}, jobs={})
    calls = []
    async def spawn(**kwargs):
        model, first = kwargs["model_or_combo"], not calls
        calls.append((model, kwargs["client"]))
        job = NS(job_id=str(len(calls)), status="failed" if first else "completed",
                 exit_code=1 if first else 0, stdout="", stderr=refusal if first else "",
                 served_model=model, served_family="gpt" if first else "gemini", events=[], served_witness_reason="test")
        runner.jobs[job.job_id] = job
        return job
    runner.spawn = spawn
    service = SpawnService(runner, state=tmp_path)
    record = asyncio.run(service.spawn(card=CARD, prompt="Implement public code", wait_seconds=None))
    assert calls == [("omniroute/cx-gpt-6.1-sol-medium", "opencode"), ("openrouter/free", "opencode")]
    assert record["status"] == "completed"
    assert record["chosen_leg"]["id"] == "openrouter-free"
    assert record["chosen_leg"]["effort"] is None
    assert not (tmp_path / "spawn-overrides.jsonl").exists()

"""The live default route (SpawnService.route) fences legacy and argon legs."""
import pytest

from plangraph.planner import legs
from plangraph.routing_support import resolver
from plangraph.spawner.service import SpawnService

ROLES = ('implement', 'review', 'orchestrate')
LIVE_CARD = {'estimate_tokens': 1000}
LIVE_DIFF = dict(LIVE_CARD, diff_ref={'repo': '/repo', 'base': 'HEAD~1', 'head': 'HEAD'})


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setattr(resolver, 'get_catalog_models', lambda: ())
    return SpawnService(state=tmp_path, leg_reader=lambda *a, **kw: {'state': 'ok', 'evidence': None})


def routed(plan):
    return [leg.id + leg.model for leg in (*plan.legs, *plan.review_seats)]


@pytest.mark.parametrize('role', ROLES)
def test_live_default_route_never_routes_legacy(service, role):
    plan = service.route(LIVE_DIFF, role, writer_families=('claude',))
    assert plan.legs
    assert not [i for i in routed(plan) if 'gemini-3.8-flash' in i.lower() or 'gpt-6-luna' in i.lower()]


@pytest.mark.parametrize('card', [LIVE_CARD, LIVE_DIFF], ids=['no_diff', 'diff'])
@pytest.mark.parametrize('family', ['anthropic', 'claude'])
def test_live_default_review_route_never_uses_argon(service, card, family):
    plan = service.route(card, 'review', writer_families=(family,))
    assert plan.legs and plan.review_seats
    assert not [i for i in routed(plan) if 'argon' in i.lower()]


def test_live_default_legs_keep_pinnable_legs_in_registry():
    ids = {leg.id for leg in legs.DEFAULT_LEGS}
    assert {'agy-gemini-3.8-flash', 'cx-gpt-6-luna-medium', 'agy-gemini-4-argon'} <= ids
