"""Utility eligibility and score rules of the route (plangraph:ROUTE-RANK-RULES).

Card needs, the effort channel, imputed rows, the quality source and stale or canary rows.
"""
import json
import logging
from dataclasses import replace

import pytest

from plangraph.planner import legs as legs_module
from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.routing_support.resolver import IMPUTED_DEFAULT_SD, rank_candidates, score_model

NOW = 1_800_000_000.0
DAY = 86400
BY_ID = {leg.id: leg for leg in DEFAULT_LEGS}
CARD = {"estimate_tokens": 1000}
OK = lambda model, **_scope: {"state": "available", "evidence": None}   # noqa: E731


def iso(age_days):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - age_days * DAY, timezone.utc).strftime("%Y-%m-%d")


def pool(*ids):
    return tuple(replace(BY_ID[i], roles=("orchestrate",), disabled=None) for i in ids)


def plan(legs, card=None, benchmark=None, catalog_rows=()):
    return plan_spawn(dict(CARD, **(card or {})), role="orchestrate", legs=legs, leg_reader=OK, now=NOW,
                      benchmark=benchmark, catalog_rows=catalog_rows)


def skips(result):
    return {s["leg"]: s["reason"] for s in result.skipped}


# --- card needs -------------------------------------------------------------------------------

MIMO, DEEPSEEK, SONNET = "mimo-v2.6-pro", "omniroute-deepseek-flash", "agy-sonnet-5.5"


@pytest.mark.parametrize("card, rejected", [
    ({"needs": ["tools"]}, {MIMO: "needs_tools"}),
    ({"needs": ["shell"]}, {MIMO: "needs_shell"}),
    ({"needs": ["long_context"]}, {DEEPSEEK: "needs_long_context"}),
    ({"needs": ["vision"]}, {MIMO: "needs_vision", DEEPSEEK: "needs_vision"}),
    ({"tool_mode": "tools"}, {MIMO: "needs_tool_mode"}),
])
def test_a_leg_lacking_a_card_need_is_rejected_with_its_reason(card, rejected):
    result = plan(pool(MIMO, DEEPSEEK, SONNET), card)
    assert {k: v for k, v in skips(result).items() if k in rejected} == rejected
    assert SONNET in [leg.id for leg in result.legs]          # the tools + vision + 1m leg passes every need
    assert not set(rejected) & {leg.id for leg in result.legs}


def test_every_need_together_leaves_only_the_leg_that_has_them_all():
    result = plan(pool(MIMO, DEEPSEEK, SONNET), {"needs": ["tools", "shell", "long_context", "vision"],
                                                 "tool_mode": "tools"})
    assert [leg.id for leg in result.legs] == [SONNET]


def test_a_bad_need_is_refused():
    for card in ({"needs": ["telepathy"]}, {"needs": "vision"}, {"tool_mode": "psychic"}):
        with pytest.raises(SpawnRefused):
            plan(pool(SONNET), card)


def test_fallback_legs_pass_the_same_filter(monkeypatch):
    codex_down = lambda model, **_scope: {"state": "limited" if "cx-gpt" in model else "available", "evidence": None}  # noqa: E731
    run = lambda card: plan_spawn(dict(CARD, **card), role="implement", leg_reader=codex_down, now=NOW,
                                  catalog_rows=())
    vision = run({"needs": ["vision"]})
    assert vision.first_leg.id == "claude-sonnet-5-5-high"       # a vision card: the Claude fallback passes
    monkeypatch.setattr(legs_module, "VISION_FAMILIES", frozenset({"gpt"}))   # a text-only fallback family
    text_only = run({"needs": ["vision"]})
    assert skips(text_only)["claude-sonnet-5-5-high"] == "needs_vision"
    assert "claude-sonnet-5-5-high" not in [leg.id for leg in text_only.legs]
    assert run({}).first_leg.id == "claude-sonnet-5-5-high"       # no need: the same leg is fine


# --- effort channel ---------------------------------------------------------------------------

def row(**kw):
    return dict({"aa_intelligence": 50.0, "cost_per_task_usd": 0.1}, **kw)


@pytest.mark.parametrize("channel, effort, leg_effort, row_effort, ok", [
    ("none", "high", None, None, False),         # no channel carries an effort
    ("parameter", "high", None, None, True),
    ("parameter", "max", None, None, False),     # Sonnet clamps max to xhigh: refused, never lowered
    ("fixed", "high", "medium", "medium", False),  # measured at medium only
    ("fixed", "medium", "medium", "medium", True),
    (None, "high", None, None, True),             # an unknown channel proves nothing
])
def test_an_effort_the_channel_cannot_pass_is_ineligible_never_lowered(channel, effort, leg_effort, row_effort, ok):
    legs = tuple(replace(leg, effort=leg_effort) for leg in pool(SONNET))
    bench = {legs[0].model: row(effort_channel=channel, effort=row_effort)}
    result = plan(legs, {"effort": effort}, bench)
    assert (legs[0].id in [leg.id for leg in result.legs]) is ok
    if not ok:
        assert skips(result)[legs[0].id] == "effort_channel"
        assert result.skipped[0]["evidence"]["effort"] == effort    # the reason is logged with its evidence
    assert all(leg.effort == leg_effort for leg in result.legs)    # the effort is never rewritten


def test_no_requested_effort_means_no_channel_check():
    bench = {BY_ID[SONNET].model: row(effort_channel="none")}
    assert SONNET in [leg.id for leg in plan(pool(SONNET), None, bench).legs]


# --- imputed rows -----------------------------------------------------------------------------

def cand(name, **kw):
    return dict({"model_id": name, "tier": "subscription", "available": 1, "active": True,
                 "aa_intelligence": 50.0, "cost_per_task_usd": 0.1, "verified_tok_per_s": 50.0}, **kw)


def test_an_imputed_row_ranks_at_mean_minus_one_sd():
    imputed = cand("i", imputed=1, score_se=4.0)
    assert score_model(imputed) == score_model(cand("m", aa_intelligence=46.0))
    assert score_model(cand("i", imputed=1)) == score_model(cand("m", aa_intelligence=50.0 - IMPUTED_DEFAULT_SD))
    assert score_model(cand("m", imputed=0)) == score_model(cand("m"))


@pytest.mark.parametrize("imputed_speed", [10.0, 50.0, 5000.0])
def test_an_imputed_row_never_ranks_above_a_measured_row_of_equal_mean(imputed_speed):
    ranked = rank_candidates([cand("imputed", imputed=1, score_se=0.0, verified_tok_per_s=imputed_speed),
                              cand("measured", verified_tok_per_s=50.0)])
    assert [c["model_id"] for c in ranked] == ["measured", "imputed"]


def test_the_route_orders_an_imputed_row_behind_a_measured_one():
    legs = pool(MIMO, DEEPSEEK)
    bench = {BY_ID[MIMO].model: row(imputed=1, score_se=2.0), BY_ID[DEEPSEEK].model: row()}
    assert [leg.id for leg in plan(legs, None, bench).legs][:2] == [DEEPSEEK, MIMO]
    assert [leg.id for leg in plan(tuple(reversed(legs)), None, bench).legs][:2] == [DEEPSEEK, MIMO]


# --- quality source ---------------------------------------------------------------------------

def test_quality_is_coding_index_else_aa_index_else_own_p_hat_at_n_20():
    as_quality = lambda value: score_model(cand("q", aa_intelligence=value))   # noqa: E731
    assert score_model(cand("a", aa_intelligence=50.0, coding_index=60.0)) == as_quality(60.0)
    assert score_model(cand("a", aa_intelligence=50.0, coding_index=None)) == as_quality(50.0)
    own = dict(origin="own", score=70.0, aa_intelligence=50.0, coding_index=60.0)
    assert score_model(cand("a", n_runs=20, **own)) == as_quality(70.0)       # own p_hat from n >= 20
    assert score_model(cand("a", n_runs=19, **own)) == as_quality(60.0)       # below 20 runs: the index
    assert score_model(cand("a", n_runs=40, **dict(own, origin="aa-index"))) == as_quality(60.0)


# --- stale rows -------------------------------------------------------------------------------

LEG = SONNET
HIGH = {"risk": "high"}


def fresh(**kw):
    return row(measured_at=iso(1), index_version="v2", **kw)


def eligible(result):
    return LEG in [leg.id for leg in result.legs]


def catalog(**kw):
    model = BY_ID[LEG].model
    return [dict({"id": f"agy/{model}", "model_id": f"agy/{model}", "available": 1, "active": True}, **kw)]


@pytest.mark.parametrize("risk", ["high", "critical"])
def test_a_row_older_than_45_days_is_ineligible_for_high_and_critical(risk):
    card = {"risk": risk}
    for age, ok in ((44, True), (46, False)):
        result = plan(pool(LEG), card, {BY_ID[LEG].model: row(measured_at=iso(age))})
        assert eligible(result) is ok, age
    assert skips(result)[LEG] == "stale_row" and result.skipped[0]["evidence"]["why"] == "age"
    assert eligible(plan(pool(LEG), {"risk": "normal"}, {BY_ID[LEG].model: row(measured_at=iso(400))}))


def test_a_row_after_an_index_version_change_is_ineligible_for_high_and_critical():
    legs = pool(LEG, DEEPSEEK)
    bench = {BY_ID[LEG].model: row(measured_at=iso(10), index_version="v1"),
             BY_ID[DEEPSEEK].model: row(measured_at=iso(2), index_version="v2")}   # v2 is the current index
    result = plan(legs, HIGH, bench)
    assert not eligible(result) and skips(result)[LEG] == "stale_row"
    assert [s for s in result.skipped if s["leg"] == LEG][0]["evidence"]["why"] == "index_version"
    assert eligible(plan(legs, {"risk": "normal"}, bench))
    assert eligible(plan(legs, HIGH, dict(bench, **{BY_ID[LEG].model: row(measured_at=iso(10), index_version="v2")})))


def test_a_row_after_a_price_change_is_ineligible_for_high_and_critical():
    bench = {BY_ID[LEG].model: fresh(source_record=json.dumps({"price": 1.0}))}
    result = plan(pool(LEG), HIGH, bench, catalog(price=2.0))
    assert not eligible(result) and result.skipped[0]["evidence"]["why"] == "price"
    assert eligible(plan(pool(LEG), HIGH, bench, catalog(price=1.0)))
    assert eligible(plan(pool(LEG), {"risk": "normal"}, bench, catalog(price=2.0)))


def test_a_row_after_a_served_model_change_is_ineligible_for_high_and_critical():
    bench = {BY_ID[LEG].model: fresh(served_model="claude-sonnet-5-5-20261001")}
    result = plan(pool(LEG), {"risk": "critical"}, bench, catalog(upstream_id="claude-sonnet-5-5-20261101"))
    assert not eligible(result) and result.skipped[0]["evidence"]["why"] == "served_model"
    assert eligible(plan(pool(LEG), {"risk": "critical"}, bench, catalog(upstream_id="claude-sonnet-5-5-20261001")))


def test_the_stale_reason_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger="plangraph.planner.benchmarks"):
        plan(pool(LEG), HIGH, {BY_ID[LEG].model: row(measured_at=iso(90))})
    assert any("route_rank_ineligible" in r.message and LEG in r.message for r in caplog.records)


# --- canary -----------------------------------------------------------------------------------

def test_a_new_own_row_needs_a_five_card_canary_for_high_and_critical():
    for runs, ok in ((4, False), (5, True)):
        bench = {BY_ID[LEG].model: fresh(origin="own", n_runs=runs)}
        result = plan(pool(LEG), HIGH, bench)
        assert eligible(result) is ok, runs
    assert skips(result) == {} and skips(plan(pool(LEG), HIGH, {BY_ID[LEG].model: fresh(origin="own", n_runs=4)})) == {
        LEG: "canary_pending"}
    # A normal card may still run on the new row: that run is its canary.
    assert eligible(plan(pool(LEG), {"risk": "normal"}, {BY_ID[LEG].model: fresh(origin="own", n_runs=0)}))
