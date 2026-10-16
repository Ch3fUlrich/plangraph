"""Planner routing: D25 leg order, window picking, eligibility, failure walk (hermetic)."""
from __future__ import annotations

import dataclasses
import json

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS, Leg, load_legs, with_measured
from plangraph.planner.routing import (RouteError, effective_estimate,
                                       next_after_failure, pick_window_class,
                                       route_card)

D25_IDS = [
    "qodercli-qwen3.8-flash", "opencode-zen-free", "or-north-mini-code-free",
    "or-laguna-s-2.1-free", "or-nemotron-3-super-free", "ovh-gpt-oss-120b",
    "mimo-v2.6-flash", "agy-gemini-3.8-flash", "agy-sonnet-5.5", "deepseek",
]


def card(**kw):
    base = {"outcome": "do it", "estimate_tokens": 40000, "risk_tags": [],
            "window_class": None}
    base.update(kw)
    return base


def ids(route):
    return [leg.id for leg in route.legs]


def good_leg(**kw):
    base = {"id": "x", "tier": "free", "harness": "opencode", "model": "m",
            "family": "f", "windows": ["128k"], "max_request_tokens": 1000,
            "trains_on_data": False}
    base.update(kw)
    return base


# --- legs -------------------------------------------------------------------------

def test_default_legs_exact_d25_order():
    assert [leg.id for leg in DEFAULT_LEGS] == D25_IDS
    assert "trial" not in {leg.tier for leg in DEFAULT_LEGS}
    tiers = [TIERS.index(leg.tier) for leg in DEFAULT_LEGS]
    assert tiers == sorted(tiers)
    ids_ = [leg.id for leg in DEFAULT_LEGS]
    assert ids_.index("mimo-v2.6-flash") < ids_.index("agy-gemini-3.8-flash")
    assert ids_[-1] == "deepseek"


def test_load_legs_none_is_default():
    assert load_legs(None) is DEFAULT_LEGS


def test_load_legs_good_file_replaces(tmp_path):
    p = tmp_path / "legs.json"
    p.write_text(json.dumps({"legs": [good_leg(id="a"), good_leg(id="b", tier="paid")]}))
    legs = load_legs(p)
    assert [leg.id for leg in legs] == ["a", "b"]
    assert legs[0].windows == ("128k",) and isinstance(legs[0], Leg)
    assert legs[0].claude_family is False and legs[0].note == ""


def test_load_legs_bad_file_lists_problems(tmp_path):
    p = tmp_path / "legs.json"
    p.write_text(json.dumps({"legs": [
        good_leg(tier="gold"), good_leg(harness="vim"), good_leg(windows=["512k"]),
        good_leg(max_request_tokens=0), good_leg(max_request_tokens=True),
        good_leg(windows=[]), {"id": "y"}]}))
    with pytest.raises(ValueError) as err:
        load_legs(p)
    msg = str(err.value)
    for needle in ("legs[0].tier", "legs[1].harness", "legs[2].windows",
                   "legs[3].max_request_tokens", "legs[4].max_request_tokens",
                   "legs[5].windows", "legs[6]: missing field"):
        assert needle in msg


def test_load_legs_rejects_bad_shapes(tmp_path):
    p = tmp_path / "legs.json"
    for text in ("not json", "[]", '{"legs": []}', '{"legs": {}}',
                 json.dumps({"legs": [good_leg(), good_leg()]})):
        p.write_text(text)
        with pytest.raises(ValueError):
            load_legs(p)
    with pytest.raises(ValueError):
        load_legs(tmp_path / "missing.json")


def test_with_measured_overrides_by_id_and_ignores_unknown():
    out = with_measured(DEFAULT_LEGS, {"deepseek": 5000, "nope": 1})
    assert [leg.id for leg in out] == D25_IDS
    assert out[-1].max_request_tokens == 5000
    assert out[0].max_request_tokens == DEFAULT_LEGS[0].max_request_tokens
    assert DEFAULT_LEGS[-1].max_request_tokens == 100000  # original untouched
    with pytest.raises(ValueError):
        with_measured(DEFAULT_LEGS, {"deepseek": 0})


# --- estimate / window ------------------------------------------------------------

def test_effective_estimate_rounds_up_and_requires_value():
    assert effective_estimate(40000) == 40000
    assert effective_estimate(40000, 3) == 48000
    assert effective_estimate(1001, 2) == 1102  # 1101.1 rounds up
    with pytest.raises(ValueError):
        effective_estimate(None)
    with pytest.raises(ValueError):
        effective_estimate(0)
    with pytest.raises(ValueError):
        effective_estimate(10, 0)


def test_pick_window_class_boundaries_floor_and_too_big():
    assert pick_window_class(72089) == "128k"   # 0.55 * 131072 = 72089.6
    assert pick_window_class(72090) == "256k"
    assert pick_window_class(10, floor="256k") == "256k"
    assert pick_window_class(576716) == "1m"    # 0.55 * 1048576 = 576716.8
    with pytest.raises(RouteError, match="estimate too big"):
        pick_window_class(576717)
    with pytest.raises(ValueError):
        pick_window_class(10, floor="2m")


# --- route_card -------------------------------------------------------------------

def test_small_estimate_goes_128k_qodercli_first():
    r = route_card(card())
    assert r.window_class == "128k"
    assert r.first_leg.id == "qodercli-qwen3.8-flash"
    assert r.harness == "qodercli"
    assert (r.attempt_budget, r.promotions, r.cross_family) == (3, 1, True)
    assert ids(r) == D25_IDS  # all legs eligible, D25 order, tiers ascending


def test_90000_goes_256k_and_only_legs_with_that_window():
    r = route_card(card(estimate_tokens=90000))
    assert r.window_class == "256k"
    assert ids(r) == ["qodercli-qwen3.8-flash", "or-nemotron-3-super-free",
                      "mimo-v2.6-flash", "agy-gemini-3.8-flash",
                      "agy-sonnet-5.5", "deepseek"]
    assert all("256k" in leg.windows for leg in r.legs)


def test_card_window_class_is_respected():
    r = route_card(card(window_class="256k", estimate_tokens=1000))
    assert r.window_class == "256k"
    assert "opencode-zen-free" not in ids(r)


def test_risky_tag_removes_trains_on_data_legs_with_reason():
    for tag in ("auth", "secrets", "money", "live-step"):
        r = route_card(card(risk_tags=[tag]))
        assert not any(leg.trains_on_data for leg in r.legs)
        assert "excluded 4 legs: trains_on_data" in r.reasons
    # migration / public-contract keep the free trainers
    r = route_card(card(risk_tags=["migration"]))
    assert any(leg.trains_on_data for leg in r.legs)


def test_over_leg_max_request_drops_leg():
    r = route_card(card(estimate_tokens=95000, window_class="256k"))
    assert "or-north-mini-code-free" not in ids(r)
    r2 = route_card(card(estimate_tokens=95000))  # picks 256k; 90k legs lack 256k anyway
    assert r2.window_class == "256k"
    legs = with_measured(DEFAULT_LEGS, {"or-north-mini-code-free": 50000,
                                        "ovh-gpt-oss-120b": 50000})
    r3 = route_card(card(estimate_tokens=60000), legs=legs)
    assert r3.window_class == "128k"
    assert ids(r3) == [i for i in D25_IDS
                       if i not in ("or-north-mini-code-free", "ovh-gpt-oss-120b")]
    assert "excluded 2 legs: max_request_tokens" in r3.reasons


def test_over_window_share_for_fixed_class_has_no_route():
    with pytest.raises(RouteError, match="over_window_share"):
        route_card(card(window_class="128k", estimate_tokens=80000))


def test_unavailable_first_leg_falls_to_next_free():
    r = route_card(card(), unavailable={"qodercli-qwen3.8-flash"})
    assert r.first_leg.id == "opencode-zen-free"
    assert r.harness == "opencode"
    assert "excluded 1 legs: unavailable" in r.reasons


def test_exclude_families():
    r = route_card(card(), exclude_families={"claude", "qwen"})
    assert all(leg.family not in ("claude", "qwen") for leg in r.legs)


def test_subtasks_bump_pushes_window_class():
    assert route_card(card(estimate_tokens=70000)).window_class == "128k"
    assert route_card(card(estimate_tokens=70000), subtasks=2).window_class == "256k"


def test_overflow_upgrades_one_class_only():
    r = route_card(card(), overflow=True)
    assert r.window_class == "256k"
    assert any("overflow" in x for x in r.reasons)
    assert route_card(card(window_class="256k"), overflow=True).window_class == "1m"
    with pytest.raises(RouteError):
        route_card(card(window_class="1m"), overflow=True)


def test_no_estimate_or_no_leg_raises():
    with pytest.raises(ValueError):
        route_card(card(estimate_tokens=None))
    with pytest.raises(RouteError, match="no eligible leg"):
        route_card(card(), legs=())
    with pytest.raises(RouteError):
        route_card(card(), unavailable=set(D25_IDS))
    with pytest.raises(RouteError, match="estimate too big"):
        route_card(card(estimate_tokens=900000))
    with pytest.raises(ValueError):
        route_card(card(risk_tags=["bogus"]))


def test_review_levels():
    r = route_card(card())
    assert (r.review_level, r.review_seats) == ("standard", 1)
    for tag in ("auth", "secrets", "live-step", "money", "migration", "public-contract"):
        r = route_card(card(risk_tags=[tag]))
        assert (r.review_level, r.review_seats, r.cross_family) == ("risky", 2, True)


def test_tier_order_beats_list_order_in_custom_legs():
    paid_first = (dataclasses.replace(DEFAULT_LEGS[-1]),) + DEFAULT_LEGS[:2]
    r = route_card(card(), legs=paid_first)
    assert ids(r) == ["qodercli-qwen3.8-flash", "opencode-zen-free", "deepseek"]


def test_measured_window_changes_eligibility():
    legs = with_measured(DEFAULT_LEGS, {"qodercli-qwen3.8-flash": 30000})
    assert route_card(card(), legs=legs).first_leg.id == "opencode-zen-free"


# --- next_after_failure -----------------------------------------------------------

@pytest.fixture
def route():
    return route_card(card())


def test_usage_limit_rotates_in_tier_then_waits(route):
    free = [leg.id for leg in route.legs if leg.tier == "free"]
    action, leg = next_after_failure(route, [free[0]], "usage_limit", 1)
    assert (action, leg.id) == ("rotate", free[1])
    action, leg = next_after_failure(route, free[:2], "outage", 1)
    assert leg.id == free[2]
    assert next_after_failure(route, free, "usage_limit", 1) == ("wait", None)
    sub = [leg.id for leg in route.legs if leg.tier == "subscription"]
    assert next_after_failure(route, free + sub[:1], "outage", 1)[1].id == sub[1]
    assert next_after_failure(route, free + sub, "outage", 1) == ("wait", None)


def test_usage_limit_empty_tried_excludes_implicit_first_leg(route):
    free = [leg.id for leg in route.legs if leg.tier == "free"]
    assert route.first_leg.id == free[0]
    for ec in ("usage_limit", "outage"):
        action, leg = next_after_failure(route, [], ec, 1)
        assert (action, leg.id) == ("rotate", free[1])


def test_usage_limit_empty_tried_single_leg_tier_waits():
    free = [leg for leg in DEFAULT_LEGS if leg.tier == "free"]
    legs = tuple(free[:1]) + tuple(leg for leg in DEFAULT_LEGS if leg.tier != "free")
    r = route_card(card(), legs=legs)
    assert r.first_leg.id == free[0].id
    assert next_after_failure(r, [], "usage_limit", 1) == ("wait", None)


def test_retry_until_budget_then_promote_free_sub_paid_park(route):
    q = "qodercli-qwen3.8-flash"
    for ec in ("check_fail", "stall", "timeout", "rebase_conflict", "worker_crash"):
        a, leg = next_after_failure(route, [q], ec, 2)
        assert (a, leg.id) == ("retry", q)
    a, leg = next_after_failure(route, [q], "check_fail", 3)
    assert (a, leg.id) == ("promote", "mimo-v2.6-flash")
    a, leg = next_after_failure(route, [q, leg.id], "check_fail", 3)
    assert (a, leg.id) == ("promote", "deepseek")
    assert next_after_failure(route, [q, "mimo-v2.6-flash", "deepseek"],
                              "stall", 3) == ("park", None)
    assert next_after_failure(route, [q], "check_fail", 0)[0] == "retry"


def test_promote_skips_absent_tier():
    r = route_card(card(), legs=tuple(l for l in DEFAULT_LEGS if l.tier != "subscription"))
    a, leg = next_after_failure(r, ["qodercli-qwen3.8-flash"], "timeout", 3)
    assert (a, leg.id) == ("promote", "deepseek")


def test_park_classes(route):
    for ec in ("contract_defect", "needs_input", "flaky_check", "cancelled"):
        assert next_after_failure(route, ["deepseek"], ec, 0) == ("park", None)


def test_empty_tried_uses_first_leg_and_bad_input(route):
    assert next_after_failure(route, [], "check_fail", 1)[1] is route.first_leg
    with pytest.raises(ValueError):
        next_after_failure(route, ["ghost"], "check_fail", 1)
    with pytest.raises(ValueError):
        next_after_failure(route, [], "ok", 1)
    with pytest.raises(ValueError):
        next_after_failure(route, [], "weird", 1)
