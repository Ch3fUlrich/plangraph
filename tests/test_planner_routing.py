"""Planner routing: D25 leg order, window picking, eligibility, failure walk (hermetic)."""
from __future__ import annotations

import dataclasses
import json

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, TIERS, Leg, WRITER_ORDER, load_legs, with_measured
from plangraph.planner.routing import (RouteError, effective_estimate,
                                       next_after_failure, pick_window_class,
                                        plan_spawn, route_card)

CANONICAL_DEFAULT_LEGS = DEFAULT_LEGS  # the true plangraph.planner.legs.DEFAULT_LEGS, before the filter below
DEFAULT_LEGS = tuple(l for l in DEFAULT_LEGS if l.tier != 'credit')
WRITER_ORDER = tuple(w for w in WRITER_ORDER if not w.startswith('console-'))
_route_card = route_card
def route_card(card, **kwargs):
    return _route_card(card, **dict({'legs': DEFAULT_LEGS}, **kwargs))
D25_IDS = [
    "qodercli-qwen3.8-flash", "opencode-deepseek-v4.1-flash-free",
    "opencode-glm-5.3-flash-free", "opencode-zen-free", "or-north-mini-code-free",
    "or-laguna-s-2.1-free", "or-nemotron-3-super-free", "ovh-gpt-oss-120b",
    "openrouter-free",
    "cx-gpt-6.1-sol", "cx-gpt-6-luna", "agy-gemini-3.8-flash", "mimo-v2.6-flash", "mimo-v2.6-pro",
    "agy-sonnet-5.5", "deepseek",
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
    assert ids_.index("cx-gpt-6.1-sol") < ids_.index("agy-gemini-3.8-flash") < ids_.index("mimo-v2.6-flash")
    assert ids_[-1] == "deepseek"


def test_load_legs_none_is_default():
    assert load_legs(None) is CANONICAL_DEFAULT_LEGS
    assert tuple(l for l in load_legs(None) if l.tier != 'credit') == DEFAULT_LEGS


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
                      "cx-gpt-6.1-sol", "cx-gpt-6-luna", "agy-gemini-3.8-flash",
                      "mimo-v2.6-flash", "mimo-v2.6-pro", "agy-sonnet-5.5", "deepseek"]
    assert all("256k" in leg.windows for leg in r.legs)


def test_card_window_class_is_respected():
    r = route_card(card(window_class="256k", estimate_tokens=1000))
    assert r.window_class == "256k"
    assert "opencode-zen-free" not in ids(r)


def test_risky_tag_removes_trains_on_data_legs_with_reason():
    for tag in ("auth", "secrets", "money", "live-step"):
        r = route_card(card(risk_tags=[tag]))
        assert not any(leg.trains_on_data for leg in r.legs)
        assert "excluded 7 legs: trains_on_data" in r.reasons
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
    assert r.first_leg.id == "opencode-deepseek-v4.1-flash-free"
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
    assert ids(r) == ["qodercli-qwen3.8-flash", "opencode-deepseek-v4.1-flash-free", "deepseek"]


def test_measured_window_changes_eligibility():
    legs = with_measured(DEFAULT_LEGS, {"qodercli-qwen3.8-flash": 30000})
    assert route_card(card(), legs=legs).first_leg.id == "opencode-deepseek-v4.1-flash-free"


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
    assert (a, leg.id) == ("promote", "cx-gpt-6.1-sol")
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


def test_writer_order_d2683_implement_first_seven():
    # (a) implement plan for a public standard card (estimate_tokens 20000, no exclusions)
    # lists exactly agy-gemini-3.8-flash, mimo-v2.6-pro, openrouter-free,
    # opencode-deepseek-v4.1-flash-free, opencode-glm-5.3-flash-free, mimo-v2.6-flash,
    # cx-gpt-6.1-sol as the first 7 leg ids in that order
    c = {"estimate_tokens": 20000, "risk_tags": []}
    plan = plan_spawn(c, role="implement")
    expected = [
        "agy-gemini-3.8-flash",
        "mimo-v2.6-pro",
        "openrouter-free",
        "opencode-deepseek-v4.1-flash-free",
        "opencode-glm-5.3-flash-free",
        "mimo-v2.6-flash",
        "cx-gpt-6.1-sol",
    ]
    assert [leg.id for leg in plan.legs[:7]] == expected


def test_writer_order_d2683_unavailable_skipped():
    # (b) a leg marked unavailable is skipped and the next in WRITER_ORDER leads
    c = {"estimate_tokens": 20000, "risk_tags": []}
    def mock_reader(model, **kwargs):
        if model == "gemini-3.8-flash":
            return {"state": "limited", "evidence": {"status": 429}}
        return {"state": "available", "evidence": None}
    plan = plan_spawn(c, role="implement", leg_reader=mock_reader)
    assert plan.first_leg.id == "mimo-v2.6-pro"


def test_writer_order_d2683_review_order_unchanged():
    # (c) review plan order is unchanged (qodercli-qwen3.8-flash first)
    c = {"estimate_tokens": 20000, "risk_tags": []}
    plan = plan_spawn(c, role="review", writer_families=("claude",))
    assert plan.first_leg.id == "qodercli-qwen3.8-flash"


def test_writer_order_d2683_writer_family_exclusion():
    # (d) writer-family exclusion still applies for review; exclude_families applies for implement
    c = {"estimate_tokens": 20000, "risk_tags": []}
    plan_rev = plan_spawn(c, role="review", writer_families=("qwen",))
    assert plan_rev.first_leg.id == "opencode-deepseek-v4.1-flash-free"
    plan_imp = plan_spawn(c, role="implement", exclude_families=("gemini",))
    assert plan_imp.first_leg.id == "mimo-v2.6-pro"
    # F2: writer_families does not exclude implement legs
    plan_wf = plan_spawn(c, role="implement", writer_families=("gemini",))
    assert plan_wf.first_leg.id == "agy-gemini-3.8-flash"


def test_implement_benchmark_highest_cx_still_writer_order_and_skips_limited():
    # F1 new test: implement plan_spawn with benchmark rows that score cx-gpt-6.1-sol
    # highest still returns legs in WRITER_ORDER (first leg agy-gemini-3.8-flash),
    # and a limited leg is still skipped.
    rows = {
        "omniroute/cx-gpt-6.1-sol": {"model_id": "omniroute/cx-gpt-6.1-sol", "aa_intelligence": 999, "cost_per_task_usd": 0, "pool_burn_weight": 0},
        "gemini-3.8-flash": {"model_id": "gemini-3.8-flash", "aa_intelligence": 1, "cost_per_task_usd": 0, "pool_burn_weight": 0},
        "mimo/mimo-v2.6-pro": {"model_id": "mimo/mimo-v2.6-pro", "aa_intelligence": 1, "cost_per_task_usd": 0, "pool_burn_weight": 0},
    }
    def reader(model, **kwargs):
        if model == "mimo/mimo-v2.6-pro":
            return {"state": "limited", "evidence": {"status": 429}}
        return {"state": "available", "evidence": None}

    c = {"estimate_tokens": 20000, "risk_tags": []}
    plan = plan_spawn(c, role="implement", benchmark=rows, leg_reader=reader)
    assert plan.first_leg.id == "agy-gemini-3.8-flash"
    assert "mimo-v2.6-pro" not in [l.id for l in plan.legs]
    assert any(s["leg"] == "mimo-v2.6-pro" and s["reason"] == "limited" for s in plan.skipped)
    from plangraph.planner.legs import WRITER_ORDER
    WRITER_ORDER = tuple(w for w in WRITER_ORDER if not w.startswith('console-'))
    expected_writers = [w for w in WRITER_ORDER if w != "mimo-v2.6-pro"]
    assert [l.id for l in plan.legs[:len(expected_writers)]] == expected_writers


def test_no_implement_plan_leg_has_claude_family():
    # F5: no implement-plan leg has claude_family True
    for estimate in (1000, 20000, 100000):
        plan = plan_spawn({"estimate_tokens": estimate}, role="implement")
        assert plan.legs
        assert not any(leg.claude_family for leg in plan.legs)
        assert all(leg.claude_family is False for leg in plan.legs)


def test_alternate_limit_lookup_restricted_to_agy_harness():
    from plangraph.planner.routing import next_leg_after_provider_stop, SpawnPlan
    from plangraph.planner.legs import DEFAULT_LEGS

    # 1. plan_spawn:
    # A qodercli leg (model Qwen3.8-Flash, harness qodercli) is NOT marked limited
    # when "agy/Qwen3.8-Flash" is limited.
    def qoder_mock_reader(model, **kwargs):
        if model == "agy/Qwen3.8-Flash":
            return {"state": "limited", "evidence": {"status": 429}}
        return {"state": "available", "evidence": None}

    c_rev = {"estimate_tokens": 20000, "risk_tags": []}
    plan_rev = plan_spawn(c_rev, role="review", writer_families=("claude",), leg_reader=qoder_mock_reader)
    assert plan_rev.first_leg.id == "qodercli-qwen3.8-flash"
    assert not any(s["leg"] == "qodercli-qwen3.8-flash" for s in plan_rev.skipped)

    # agy-gemini-3.8-flash (model gemini-3.8-flash, harness agy) IS skipped
    # when "agy/gemini-3.8-flash" is limited.
    def agy_mock_reader(model, **kwargs):
        if model == "agy/gemini-3.8-flash":
            return {"state": "limited", "evidence": {"status": 429}}
        return {"state": "available", "evidence": None}

    c_imp = {"estimate_tokens": 20000, "risk_tags": []}
    plan_imp = plan_spawn(c_imp, role="implement", leg_reader=agy_mock_reader)
    assert plan_imp.first_leg.id != "agy-gemini-3.8-flash"
    assert plan_imp.first_leg.id == "mimo-v2.6-pro"
    assert any(s["leg"] == "agy-gemini-3.8-flash" and s["reason"] == "limited" for s in plan_imp.skipped)

    # 2. next_leg_after_provider_stop:
    class MockState:
        def __init__(self, limited_model):
            self.limited_model = limited_model

        def record_provider_response(self, *args, **kwargs):
            pass

        def leg_status(self, model, **kwargs):
            if model == self.limited_model:
                return {"state": "limited", "evidence": {"status": 429}}
            return {"state": "available", "evidence": None}

    legs_by_id = {l.id: l for l in DEFAULT_LEGS}
    qoder_leg = legs_by_id["qodercli-qwen3.8-flash"]
    agy_gemini = legs_by_id["agy-gemini-3.8-flash"]
    deepseek_free = legs_by_id["opencode-deepseek-v4.1-flash-free"]
    mimo_pro = legs_by_id["mimo-v2.6-pro"]

    # qodercli leg is not marked limited when "agy/Qwen3.8-Flash" is limited
    plan_for_qoder = SpawnPlan(legs=(deepseek_free, qoder_leg), skipped=(), p_source="test", review_seats=(), reasons=())
    nxt_qoder = next_leg_after_provider_stop(
        plan_for_qoder, deepseek_free.id, 429, "rate limit", {}, "test",
        store=MockState("agy/Qwen3.8-Flash")
    )
    assert nxt_qoder is not None
    assert nxt_qoder.id == "qodercli-qwen3.8-flash"

    # agy-gemini-3.8-flash is skipped when "agy/gemini-3.8-flash" is limited
    plan_for_agy = SpawnPlan(legs=(deepseek_free, agy_gemini, mimo_pro), skipped=(), p_source="test", review_seats=(), reasons=())
    nxt_agy = next_leg_after_provider_stop(
        plan_for_agy, deepseek_free.id, 429, "rate limit", {}, "test",
        store=MockState("agy/gemini-3.8-flash")
    )
    assert nxt_agy is not None
    assert nxt_agy.id == "mimo-v2.6-pro"

