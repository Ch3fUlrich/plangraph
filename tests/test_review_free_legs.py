"""D-5768: retain Big Pickle; defer unsourced additions without weakening admission."""
import dataclasses
import json
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, READINESS_LEGS, load_legs
from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy
from plangraph.planner.routing import plan_spawn, route_card
from plangraph.planner.seats import seat_family


FREE_REVIEW = {
    "opencode-big-pickle": ("opencode/big-pickle", "glm"),
}
DEFERRED = {"opencode-mimo-v2.6-flash-free", "opencode-go-longcat-2.5-free",
            "console-openrouter-free"}
CARD = {"estimate_tokens": 1000,
        "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}


def clear_reader(*args, **kwargs):
    return {"state": "available", "evidence": None}


def test_review_lift_and_deferrals_are_documented():
    docs = Path("docs/routing.md").read_text()
    changelog = Path("changelog.d/REVIEW-LEGS-FREE-S1-FREE-LEGS.md").read_text()
    for text in (docs, changelog):
        assert all(term in text for term in ("D-5648", "GLM", "Big Pickle", "review_allowed_families"))
    assert all(leg in docs for leg in DEFERRED)
    assert "Slice 1 validation gaps" not in docs


@pytest.mark.parametrize("measured", [False, True])
def test_gpt_review_lists_all_four_free_families_before_every_costlier_leg(measured):
    # Use the real policy, not the suite's neutral policy or an amended leg table.
    benchmark = {leg.id: {"aa_intelligence": 1 if leg.tier == "free" else 100,
                         "cost_per_task_usd": 0, "pool_burn_weight": 0}
                 for leg in DEFAULT_LEGS} if measured else None
    plan = plan_spawn(CARD, role="review", writer_families=("gpt",),
                      policy=load_route_policy(DEFAULT_POLICY_PATH), catalog_rows=(),
                      leg_reader=clear_reader, benchmark=benchmark)
    assert plan.error is None
    table = {leg.id: leg for leg in plan.legs}
    assert FREE_REVIEW.keys() <= table.keys()
    for key, (model, family) in FREE_REVIEW.items():
        assert (table[key].model, table[key].family, table[key].tier) == (model, family, "free")
    assert {seat_family(table[key].family) for key in FREE_REVIEW} == {"zhipu"}
    assert DEFERRED.isdisjoint(leg.id for leg in DEFAULT_LEGS)
    positions = {leg.id: i for i, leg in enumerate(plan.legs)}
    subscriptions = [leg for leg in plan.legs if leg.tier == "subscription"]
    assert subscriptions  # The costlier comparison must not be vacuous.
    assert all(positions[key] < positions[leg.id] for key in FREE_REVIEW
               for leg in plan.legs if leg.tier in ("subscription", "paid"))
    assert all(leg.tier == "free" for leg in plan.review_seats)
    assert all(seat_family(leg.family) != "openai" for leg in plan.legs)
    # Exercise the existing paid leg without changing its registered roles.
    paid = next(leg for leg in DEFAULT_LEGS if leg.id == "deepseek")
    route = route_card(CARD, legs=(*plan.legs, paid), role="review")
    assert route.legs[-1] == paid
    assert all(route.legs.index(table[key]) < route.legs.index(paid) for key in FREE_REVIEW)


def test_free_review_legs_roundtrip_and_do_not_widen_other_roles(tmp_path):
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    assert DEFERRED.isdisjoint(table)
    assert DEFERRED.isdisjoint(leg.id for leg in READINESS_LEGS)
    path = tmp_path / "legs.json"
    legs = tuple(table[key] for key in FREE_REVIEW)
    path.write_text(json.dumps({"legs": [dataclasses.asdict(leg) for leg in legs]}))
    assert load_legs(path) == legs
    unknown = (dataclasses.replace(legs[0], trains_on_data=None),)
    path.write_text(json.dumps({"legs": [dataclasses.asdict(leg) for leg in unknown]}))
    assert load_legs(path) == unknown
    assert {leg.id for leg in DEFAULT_LEGS if leg.tier == "paid"} == {"deepseek"}
    assert not any(leg.id == "console-deepseek-flash" for leg in DEFAULT_LEGS)


def test_free_review_availability_and_family_fences_still_apply():
    from plangraph.routing_support.liveness import unavailable
    for writers, state, reason in (("zhipu", "available", "excluded_family"),
                                   ("gpt", "down", "down")):
        assert unavailable({"state": state}) == (state == "down")
        plan = plan_spawn(CARD, role="review", writer_families=(writers,),
                          policy=load_route_policy(DEFAULT_POLICY_PATH), catalog_rows=(),
                          leg_reader=lambda model, **kw: {"evidence": None, "state":
                              state if model == "opencode/big-pickle" else "available"})
        reasons = {row["leg"]: row["reason"] for row in plan.skipped}
        assert reasons["opencode-big-pickle"] == reason
        assert "opencode-big-pickle" not in {leg.id for leg in plan.legs}


def test_cli_gpt_review_lists_the_four_free_legs(tmp_path, monkeypatch, capsys):
    from functools import partial
    from plangraph.spawner import cli
    from plangraph.spawner.service import SpawnService
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(DEFAULT_POLICY_PATH.resolve()))
    monkeypatch.setattr(cli, "SpawnService", partial(SpawnService, leg_reader=clear_reader))
    monkeypatch.setattr("plangraph.routing_support.resolver.get_catalog_models", lambda: [])
    assert cli.main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "gpt", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    ids = [leg["id"] for leg in result["legs"]]
    assert FREE_REVIEW.keys() <= set(ids)
    assert DEFERRED.isdisjoint(ids)
    assert any(leg["tier"] == "subscription" for leg in result["legs"])
    assert all(ids.index(key) < i for key in FREE_REVIEW
               for i, leg in enumerate(result["legs"]) if leg["tier"] in ("subscription", "paid"))


@pytest.mark.parametrize("sensitive", [{"risk_tags": [tag]} for tag in
    ("auth", "secrets", "money", "live-step")] + [{key: [path]} for key in ("paths", "write_set")
    for path in ("src/secrets.py", "src/auth.py", "src/gateway.py", "site/data/records.json",
                 "SRC\\AUTH.PY", "src/secret_store.py")])
def test_unknown_privacy_never_enters_sensitive_review(sensitive):
    # Inject an existing model, not an unsourced new registration.
    unknown = dataclasses.replace(next(leg for leg in DEFAULT_LEGS
                                      if leg.id == "opencode-big-pickle"), trains_on_data=None)
    legs = (unknown, next(leg for leg in DEFAULT_LEGS if leg.id == "agy-gemini-3.8-flash"))
    public = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs,
                        policy=load_route_policy(DEFAULT_POLICY_PATH), catalog_rows=(),
                        leg_reader=clear_reader)
    assert unknown in public.legs
    plan = plan_spawn(dict(CARD, **sensitive), role="review", legs=legs,
                      writer_families=("gpt",), policy=load_route_policy(DEFAULT_POLICY_PATH),
                      catalog_rows=(), leg_reader=clear_reader)
    assert unknown not in plan.legs and plan.legs == legs[1:]
    reasons = {row["leg"]: row["reason"] for row in plan.skipped}
    assert reasons[unknown.id] == "trains_on_data"
    route = route_card(dict(CARD, **sensitive), legs=legs, role="review")
    assert route.legs == legs[1:]
    assert "excluded 1 legs: trains_on_data" in route.reasons
