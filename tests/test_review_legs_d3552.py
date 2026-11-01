"""OPERATOR D-3552: opencode-go review legs; deepseek/glm lifted for review seats only.

* ``review_allowed_families`` loads, validates like ``banned_families``, refuses kimi;
* the lift applies to the review role only: implement/orchestrate stay banned/off;
* the five opencode-go review legs carry the catalog models and route review seats.
"""
from __future__ import annotations

import json

import pytest

from plangraph.catalog_data import SEED_MODELS
from plangraph.planner.legs import DEFAULT_LEGS, Leg
from plangraph.planner.route_policy import RoutePolicy, load_route_policy
from plangraph.planner.routing import plan_spawn
from plangraph.planner.seats import seat_family

from registry_route_util import CARD, live_policy  # noqa: F401

NEW_IDS = ("opencode-go-gemini-4-argon", "opencode-go-glm-5.2", "opencode-go-qwen3.8-max",
           "opencode-go-muse-spark", "opencode-go-deepseek-v4-flash")
CATALOG_MODELS = {
    "opencode-go-gemini-4-argon": "opencode-go/gemini-4-argon-contributor",
    "opencode-go-glm-5.2": "opencode-go/glm-5.2-max",
    "opencode-go-qwen3.8-max": "opencode-go/qwen3.8-max",
    "opencode-go-muse-spark": "opencode-go/muse-spark-1.3-contributor",
    "opencode-go-deepseek-v4-flash": "opencode-go/deepseek-v4-flash-max",
}


def _clear_limiter(model, **kwargs):
    return {"state": "available", "evidence": None}


# --- policy: the key loads, kimi is refused, the lift is review-only ----------

def test_policy_loads_review_allowed_families():
    assert load_route_policy().review_allowed_families == ("deepseek", "glm")


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
    for leg_id in ("opencode-go-deepseek-v4-flash", "opencode-deepseek-v4.1-flash-free"):
        assert policy.skip_reason(by_id[leg_id], "review") is None
        assert policy.skip_reason(by_id[leg_id], "implement") == "banned_family"
        assert policy.skip_reason(by_id[leg_id], "orchestrate") == "banned_family"
    for leg_id in ("opencode-go-glm-5.2", "opencode-glm-5.3-flash-free"):
        assert policy.skip_reason(by_id[leg_id], "review") is None
        assert policy.skip_reason(by_id[leg_id], "implement") == "off_family"


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

def test_implement_route_still_skips_banned_and_off():
    plan = plan_spawn(CARD, role="implement")
    reasons = {s["leg"]: s["reason"] for s in plan.skipped}
    assert reasons["opencode-deepseek-v4.1-flash-free"] == "banned_family"
    assert reasons["opencode-glm-5.3-flash-free"] == "off_family"
    assert not {leg.family for leg in plan.legs} & {"deepseek", "glm", "kimi"}
    assert not {leg.id for leg in plan.legs} & set(NEW_IDS)


def test_review_route_keeps_lifted_families():
    plan = plan_spawn(CARD, role="review", writer_families=("xiaomi",), leg_reader=_clear_limiter)
    ids = {leg.id for leg in plan.legs}
    assert {"opencode-go-deepseek-v4-flash", "opencode-go-glm-5.2"} <= ids
    reasons = {s["leg"]: s["reason"] for s in plan.skipped}
    assert not set(NEW_IDS) & set(reasons)


def test_review_xiaomi_writer_yields_two_new_leg_seat_families():
    plan = plan_spawn(CARD, role="review", writer_families=("xiaomi",), leg_reader=_clear_limiter)
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
        assert leg.model == CATALOG_MODELS[leg.id] and leg.model in rows
        assert (leg.harness, leg.tier, leg.roles) == ("opencode", "subscription", ("review",))
        assert not leg.trains_on_data and leg.tool_mode == "tools"
        assert leg.windows == ("128k",) and leg.max_request_tokens == 100000
    # L1 placement: the new review legs sit just before the paid deepseek leg,
    # which itself stays last in DEFAULT_LEGS.
    assert all(ids.index(new_id) < ids.index("deepseek") for new_id in NEW_IDS)
    assert DEFAULT_LEGS[-1].id == "deepseek"
