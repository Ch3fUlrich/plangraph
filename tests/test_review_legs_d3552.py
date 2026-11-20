"""D-3552 review legs, updated for D-3605/D-4779: GLM is not a review leg.

* ``review_allowed_families`` loads, validates like ``banned_families``, refuses kimi;
* the lift applies to the review role only: implement/orchestrate stay banned/off;
* the five opencode-go legs retain catalog metadata; GLM stays disabled.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("fixed_catalog_rows")

from plangraph.catalog_data import SEED_MODELS
from plangraph.planner.legs import DEFAULT_LEGS, Leg
from plangraph.planner.route_policy import RoutePolicy, load_route_policy
from plangraph.planner.routing import plan_spawn
from plangraph.planner.seats import seat_family

from registry_route_util import CARD, live_policy  # noqa: F401
from historical_legs import without_d4196

NEW_IDS = ("opencode-go-beam-free", "opencode-go-glm-5.3-flash", "opencode-go-qwen3.8-max",
           "opencode-go-muse-spark", "opencode-go-deepseek-v4-flash")
CATALOG_MODELS = {
    "opencode-go-beam-free": "opencode-go/beam-free",
    "opencode-go-glm-5.3-flash": "opencode-go/glm-5.3-flash",
    "opencode-go-qwen3.8-max": "opencode-go/qwen3.8-max",
    "opencode-go-muse-spark": "opencode-go/muse-spark-1.3-contributor",
    "opencode-go-deepseek-v4-flash": "opencode-go/deepseek-v4.1-flash",
}


def _clear_limiter(model, **kwargs):
    return {"state": "available", "evidence": None}


# --- policy: no deepseek/glm ban under D-6310, kimi stays banned ----------

def test_policy_loads_review_allowed_families():
    assert load_route_policy().review_allowed_families == ()
    assert load_route_policy().banned_families == ("kimi",)
    assert load_route_policy().off_families == ()


def test_review_allowed_families_validates_like_banned_families(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"review_allowed_families": "deepseek"}))
    with pytest.raises(ValueError):
        load_route_policy(path)
    path.write_text(json.dumps({"review_allowed_families": ["deepseek", ""]}))
    with pytest.raises(ValueError):
        load_route_policy(path)


def test_kimi_is_refused_in_review_allowed_families(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"review_allowed_families": ["deepseek", "kimi"]}))
    with pytest.raises(ValueError, match="kimi"):
        load_route_policy(path)


def test_kimi_stays_banned_for_review():
    kimi = Leg("kimi-seat", "subscription", "opencode", "opencode-go/kimi-k3", "kimi",
               ("128k",), 100000, False, roles=("review",))
    policy = RoutePolicy(banned_families=("kimi",), paused_harnesses={},
                         review_allowed_families=("kimi", "deepseek", "glm"))
    assert policy.skip_reason(kimi, "review") == "banned_family"


def test_deepseek_glm_skipped_for_implement_but_not_review():
    policy = load_route_policy()
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    for leg_id in ("opencode-go-deepseek-v4-flash", "opencode-deepseek-v4.1-flash-free",
                   "opencode-go-glm-5.3-flash"):
        assert policy.skip_reason(by_id[leg_id], "review") is None
        assert policy.skip_reason(by_id[leg_id], "implement") is None
        assert policy.skip_reason(by_id[leg_id], "orchestrate") is None


def test_lift_keeps_paused_and_no_verdict_checks():
    policy = RoutePolicy(review_allowed_families=("deepseek", "glm"),
                         paused_harnesses={"opencode": ("implement",)})
    by_id = {leg.id: leg for leg in DEFAULT_LEGS}
    assert policy.skip_reason(by_id["opencode-go-deepseek-v4-flash"], "review") == "paused_harness"
    no_verdict = Leg("no-verdict", "subscription", "opencode",
                     "opencode-go/qwen3.8-flash", "qwen", ("128k",), 100000, False,
                     roles=("review",))
    lifted = RoutePolicy(review_allowed_families=("qwen",))
    assert lifted.skip_reason(no_verdict, "review") == "no_verdict_seat"


# --- route: implement stays banned/off, review seats use the new legs ---------

def test_implement_route_still_skips_banned_and_off(tmp_path):
    import asyncio
    from plangraph.planner.routing import SpawnRefused
    from plangraph.spawner.service import SpawnService
    plan = plan_spawn(CARD, role="implement")
    reasons = {s["leg"]: s["reason"] for s in plan.skipped}
    assert reasons['opencode-glm-5.3-flash-free'].startswith('disabled: D-6310')
    assert plan.first_leg.id == 'cx-gpt-6.2-sol-medium'
    assert "opencode-deepseek-v4.1-flash-free" in [leg.id for leg in plan.legs]
    assert "opencode-go-deepseek-v4-flash" in [leg.id for leg in plan.legs]
    assert "opencode-go-glm-5.3-flash" in [leg.id for leg in plan.legs]
    for leg_id in ("opencode-go-qwen3.8-max", "opencode-go-muse-spark"):
        with pytest.raises(SpawnRefused, match=r"writer disabled \(D-3828\)"):
            asyncio.run(SpawnService(state=tmp_path).spawn(
                card=CARD, leg=leg_id, override_reason="policy regression", dry_run=True))


def test_review_route_keeps_lifted_families():
    plan = plan_spawn(CARD, role="review", writer_families=("xiaomi",), leg_reader=_clear_limiter,
                      legs=tuple(list(DEFAULT_LEGS)))
    ids = {leg.id for leg in plan.legs}
    assert "opencode-go-deepseek-v4-flash" in ids
    assert "opencode-go-glm-5.3-flash" in ids
    assert all(leg.disabled is None for leg in DEFAULT_LEGS if leg.id.startswith("opencode-go-"))
    reasons = {s["leg"]: s["reason"] for s in plan.skipped}
    assert reasons["opencode-go-beam-free"] == "low_risk_only"
    assert "opencode-go-muse-spark" in ids  # D-5113 lifts Muse admission.


def test_review_xiaomi_writer_yields_two_new_leg_seat_families():
    plan = plan_spawn(CARD, role="review", writer_families=("xiaomi",), leg_reader=_clear_limiter,
                      legs=tuple(list(DEFAULT_LEGS)))
    assert plan.error is None
    fams = {seat_family(leg.family) for leg in plan.legs if leg.id in NEW_IDS}
    assert len(fams) >= 2
    seat_fams = {seat_family(leg.family) for leg in plan.review_seats}
    assert len(seat_fams) >= 2 and "xiaomi" not in seat_fams


# --- the five legs: catalog models, review-only, before the paid deepseek leg --

def test_new_legs_use_catalog_models_and_sit_before_the_paid_deepseek_leg():
    rows = {r["model_id"] for r in SEED_MODELS}
    ids = [leg.id for leg in DEFAULT_LEGS]
    assert set(NEW_IDS) <= set(ids)
    for leg in (l for l in DEFAULT_LEGS if l.id in NEW_IDS):
        assert leg.model == CATALOG_MODELS[leg.id]
        if leg.id != "opencode-go-beam-free":
            assert leg.model in rows
        assert leg.harness == "opencode" and not leg.trains_on_data and leg.tool_mode == "tools"
        assert "review" in leg.roles and leg.disabled is None
    # L1 placement: the new opencode-go legs sit before the paid deepseek leg.
    assert all(ids.index(new_id) < ids.index("deepseek") for new_id in NEW_IDS)
    assert DEFAULT_LEGS[-2].id == "deepseek"  # D-3605 Haiku leg
