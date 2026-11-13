"""D-5743: four distinct free review families before subscription or paid legs."""
import dataclasses
import json
from pathlib import Path

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, READINESS_LEGS, load_legs
from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy
from plangraph.planner.routing import plan_spawn
from plangraph.planner.seats import seat_family


FREE_REVIEW = {
    "opencode-mimo-v2.6-flash-free": ("opencode/mimo-v2.6-flash-free", "mimo"),
    "opencode-big-pickle": ("opencode/big-pickle", "glm"),
    "opencode-go-longcat-2.5-free": ("opencode-go/longcat-2.5-free", "longcat"),
    "console-openrouter-free": ("console-openrouter/openrouter/free", "openrouter"),
}
CARD = {"estimate_tokens": 1000,
        "diff_ref": {"repo": str(Path.cwd()), "base": "HEAD~1", "head": "HEAD"}}


def clear_reader(*args, **kwargs):
    return {"state": "available", "evidence": None}


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
    assert len({seat_family(table[key].family) for key in FREE_REVIEW}) == 4
    positions = {leg.id: i for i, leg in enumerate(plan.legs)}
    subscriptions = [leg for leg in plan.legs if leg.tier == "subscription"]
    assert subscriptions  # The costlier comparison must not be vacuous.
    assert all(positions[key] < positions[leg.id] for key in FREE_REVIEW
               for leg in plan.legs if leg.tier in ("subscription", "paid"))
    assert all(leg.tier == "free" for leg in plan.review_seats)
    assert all(seat_family(leg.family) != "openai" for leg in plan.legs)
    # Exercise the existing paid leg without changing its registered roles.
    paid = next(leg for leg in DEFAULT_LEGS if leg.id == "deepseek")
    from plangraph.planner.routing import route_card
    route = route_card(CARD, legs=(*plan.legs, paid), role="review")
    assert route.legs[-1] == paid
    assert all(route.legs.index(table[key]) < route.legs.index(paid) for key in FREE_REVIEW)


def test_free_review_legs_roundtrip_and_do_not_widen_other_roles(tmp_path):
    added = set(FREE_REVIEW) - {"opencode-big-pickle"}
    table = {leg.id: leg for leg in DEFAULT_LEGS}
    assert added <= table.keys()
    assert added.isdisjoint(leg.id for leg in READINESS_LEGS)
    for key in added:
        assert table[key].roles == ("review",)
        assert table[key].trains_on_data is None  # Unknown, not an unsourced boolean.
    path = tmp_path / "legs.json"
    legs = tuple(table[key] for key in FREE_REVIEW)
    path.write_text(json.dumps({"legs": [dataclasses.asdict(leg) for leg in legs]}))
    assert load_legs(path) == legs
    assert {leg.id for leg in DEFAULT_LEGS if leg.tier == "paid"} == {"deepseek"}
    assert not any(leg.id == "console-deepseek-flash" for leg in DEFAULT_LEGS)


def test_free_review_availability_and_family_fences_still_apply():
    plan = plan_spawn(CARD, role="review", writer_families=("xiaomi",),
                      policy=load_route_policy(DEFAULT_POLICY_PATH), catalog_rows=(),
                      leg_reader=lambda model, **kw: {
                          "state": "down" if model == FREE_REVIEW["console-openrouter-free"][0]
                          else "available", "evidence": None})
    reasons = {row["leg"]: row["reason"] for row in plan.skipped}
    assert reasons["opencode-mimo-v2.6-flash-free"] == "excluded_family"
    assert reasons["console-openrouter-free"] == "down"
    assert {"opencode-mimo-v2.6-flash-free", "console-openrouter-free"}.isdisjoint(
        leg.id for leg in plan.legs)


def test_cli_gpt_review_lists_the_four_free_legs(tmp_path, monkeypatch, capsys):
    from plangraph.spawner.cli import main
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(DEFAULT_POLICY_PATH.resolve()))
    monkeypatch.setitem(plan_spawn.__kwdefaults__, "catalog_rows", ())
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "review", "--writer-family", "gpt", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    ids = [leg["id"] for leg in result["legs"]]
    assert FREE_REVIEW.keys() <= set(ids)
    assert all(ids.index(key) < i for key in FREE_REVIEW
               for i, leg in enumerate(result["legs"]) if leg["tier"] in ("subscription", "paid"))


def test_unknown_privacy_never_enters_sensitive_review():
    plan = plan_spawn(dict(CARD, risk_tags=["secrets"]), role="review",
                      writer_families=("gpt",), policy=load_route_policy(DEFAULT_POLICY_PATH),
                      catalog_rows=(), leg_reader=clear_reader)
    added = set(FREE_REVIEW) - {"opencode-big-pickle"}
    assert added.isdisjoint(leg.id for leg in plan.legs)
    reasons = {row["leg"]: row["reason"] for row in plan.skipped}
    assert all(reasons[key] == "trains_on_data" for key in added)
