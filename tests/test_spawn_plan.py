import dataclasses
import json
import re

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, load_legs
from plangraph.planner.routing import (SpawnRefused, hand_picked, plan_spawn,
                                      next_leg_after_provider_stop)
from plangraph.spawner.leg_state import LegStateStore

CARD = {"estimate_tokens": 1000}


def test_writer_live_limit_reset_and_scope(tmp_path, monkeypatch):
    from mechanics_legs import install_fallthrough_legs
    install_fallthrough_legs(monkeypatch)
    store = LegStateStore(tmp_path / "state.sqlite")
    p = plan_spawn(CARD, role="implement", leg_reader=store, now=100)
    assert p.first_leg.model == "omniroute/cx-gpt-6.1-sol"  # D-3745
    nxt = next_leg_after_provider_stop(p, p.first_leg.id, 429, "quota exhausted",
                                       {"Retry-After": "60"}, "test", store=store,
                                       now=100, account="A")
    assert nxt.id == "opencode-deepseek-v4.1-flash-free"  # Synthetic WRITER_ORDER fallback.
    limited = plan_spawn(dict(CARD, account="A"), role="implement", leg_reader=store, now=101)
    assert limited.first_leg == nxt
    assert next(s for s in limited.skipped if s["leg"] == "cx-gpt-6.1-sol")["evidence"]["status"] == 429
    assert plan_spawn(dict(CARD, account="B"), role="implement", leg_reader=store, now=101).first_leg == p.first_leg
    assert plan_spawn(dict(CARD, account="A"), role="implement", leg_reader=store, now=160).first_leg == p.first_leg
    json.dumps(limited.to_dict())


@pytest.mark.parametrize("card", [None, {}, "missing"])
def test_card_required(card):
    with pytest.raises(SpawnRefused, match="card required"):
        plan_spawn(card, role="implement")


def test_override(tmp_path):
    with pytest.raises(SpawnRefused, match="spawn by card; --override-reason required"):
        hand_picked("x", CARD)
    log = tmp_path / "overrides.jsonl"
    hand_picked("mimo/mimo-v2.6-flash", override_reason="explicit test override", log_path=log, caller="test")
    assert json.loads(log.read_text())["reason"] == "explicit test override"
    with pytest.raises(ValueError):
        hand_picked("omniroute/mimo-v2.6-flash", override_reason="explicit test override", log_path=log)


def test_seats_and_impossible_fence():
    legs = tuple(dataclasses.replace(l, disabled=None) if l.family == "qwen" else l
                 for l in DEFAULT_LEGS)
    p = plan_spawn(CARD, role="review", writer_families=("gpt",), legs=legs)
    # No diff_ref: Go is inline-only; Zen is disabled; funded DeepSeek is writer-only.
    assert [l.family for l in p.review_seats] == ["qwen", "gemini"]
    assert all(l.family != "claude" for l in p.legs if l.id != "claude-haiku-5.5")  # D-3605 Haiku leg
    assert plan_spawn(CARD, role="review", writer_families=("qwen",)).first_leg.family == "gemini"
    # With all automatic families fenced, only Haiku remains; Grok is manual-only.
    p = plan_spawn(CARD, role="review",
                   writer_families=("qwen", "deepseek", "glm", "gpt", "gemini", "mimo"), legs=legs)
    assert [l.family for l in p.review_seats] == ["claude"]
    assert p.error == "two distinct review families unavailable"
    # Impossible fence: every family but the single writer's is excluded.
    p = plan_spawn(CARD, role="review", writer_families=("qwen",),
                   exclude_families=("deepseek", "glm", "gpt", "gemini", "mimo", "muse", "claude", "grok"))  # D-3605 Haiku leg
    assert not p.review_seats and p.error


def test_operator_order_and_roles_roundtrip(tmp_path):
    assert [l.id for l in DEFAULT_LEGS if "implement" in l.roles] == [
        "cx-gpt-6.1-sol"]
    assert [l.id for l in DEFAULT_LEGS if "review" in l.roles] == [
        "qodercli-qwen3.8-flash", "opencode-deepseek-v4.1-flash-free",
        "opencode-glm-5.3-flash-free", "cx-gpt-6-luna", "omniroute-agy-gemini-3.8-flash", "agy-gemini-3.8-flash",
        "mimo-v2.6-flash", "mimo-v2.6-pro", "opencode-go-gemini-4-argon",
        "opencode-go-glm-5.2", "opencode-go-qwen3.8-max",
        "omniroute-grok-4.7", "opencode-go-muse-spark", "opencode-console-muse-spark",
        "opencode-go-deepseek-v4-flash", "omniroute-gemini-3.8-flash", "claude-haiku-5.5"]  # D-3605 Haiku leg
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [dataclasses.asdict(l) for l in DEFAULT_LEGS]}))
    assert load_legs(path) == DEFAULT_LEGS


def test_benchmark_within_tier_only(monkeypatch):
    from mechanics_legs import install_fallthrough_legs
    writer = tuple(l for l in install_fallthrough_legs(monkeypatch) if "implement" in l.roles)
    rows = {l.model: {"aa_intelligence": 40 + i * 10, "cost_per_task_usd": 0,
                      "pool_burn_weight": 0} for i, l in enumerate(writer)}
    p = plan_spawn(CARD, role="implement", benchmark=rows)
    assert p.first_leg.id == "cx-gpt-6.1-sol"  # D-3745
    assert [l.id for l in p.legs] == [
        'cx-gpt-6.1-sol', 'opencode-deepseek-v4.1-flash-free',
        'opencode-glm-5.3-flash-free', 'openrouter-free', 'agy-gemini-3.8-flash',
        'mimo-v2.6-flash', 'mimo-v2.6-pro', 'deepseek']
    assert p.legs[-1].tier == 'paid'
    assert p.p_source == "operator-order"
    assert "no benchmark p" in plan_spawn(CARD, role="implement").reasons
    review_rows = {l.model: {"aa_intelligence": 40, "cost_per_task_usd": 0,
                           "pool_burn_weight": 0} for l in DEFAULT_LEGS if 'review' in l.roles}
    p_rev = plan_spawn(CARD, role="review", writer_families=("claude",), benchmark=review_rows)
    assert p_rev.p_source == "benchmark"


@pytest.mark.parametrize("status,body", [(500, "outage"), (200, "ok"), (429, "")])
def test_non_provider_stops_do_not_mark(tmp_path, status, body):
    store = LegStateStore(tmp_path / "state.sqlite")
    p = plan_spawn(CARD, role="implement", leg_reader=store)
    assert next_leg_after_provider_stop(p, p.first_leg.id, status, body, {}, "test",
                                        store=store) == p.first_leg
    assert not store.path.exists()


@pytest.mark.parametrize("roles", [[], ["worker"], "implement", [["review"]], ["review", "review"]])
def test_load_legs_bad_roles(tmp_path, roles):
    raw = dataclasses.asdict(DEFAULT_LEGS[0])
    raw["disabled"] = None
    raw["roles"] = roles
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [raw]}))
    with pytest.raises(ValueError, match="roles"):
        load_legs(path)


@pytest.mark.parametrize("roles", [[], ["implement"], ["review"], ["orchestrate"]])
def test_disabled_leg_stays_listed_but_never_routes(tmp_path, roles):
    raw = dict(dataclasses.asdict(DEFAULT_LEGS[0]), roles=roles, disabled="operator pause")
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [raw]}))
    legs = load_legs(path)
    assert len(legs) == 1 and legs[0].disabled == "operator pause"
    for role in ("implement", "review", "orchestrate"):
        assert not plan_spawn(CARD, role=role, legs=legs, writer_families=("claude",)).legs


@pytest.mark.parametrize("reason", [False, 1, "", "   "])
def test_load_legs_refuses_invalid_disabled_reason(tmp_path, reason):
    raw = dict(dataclasses.asdict(DEFAULT_LEGS[0]), disabled=reason)
    path = tmp_path / "legs.json"
    path.write_text(json.dumps({"legs": [raw]}))
    with pytest.raises(ValueError, match="disabled"):
        load_legs(path)


def test_orchestrate_role_routes_opencode_first(tmp_path, capsys):
    # OPENCODE-ORCHESTRATOR-FALLBACK G1/G2: an orchestrator leg off Claude, mimo-pro first
    plan = plan_spawn(CARD, role="orchestrate")
    assert plan.first_leg.id == "mimo-v2.6-pro"
    # fallthrough: agy-sonnet runs on Antigravity quota, not Claude Max; never deepseek (D-2744)
    assert [l.id for l in plan.legs] == ["mimo-v2.6-pro", "agy-sonnet-5.5"]
    with pytest.raises(ValueError, match=re.escape("role must be implement|review|orchestrate")):
        plan_spawn(CARD, role="drive")
    from plangraph.spawner.cli import main
    assert main(["--state", str(tmp_path), "route", "--card", json.dumps(CARD),
                 "--role", "orchestrate"]) == 0
    assert "mimo-v2.6-pro" in capsys.readouterr().out


def test_agy_leg_models_use_agy_model_ids():
    # `agy models` ids: agy refuses the bare "sonnet-5.5" (workst run-1897ba45, 2026-10-26)
    agy = {l.id: l.model for l in DEFAULT_LEGS if l.harness == "agy"}
    assert agy == {"agy-gemini-3.8-flash": "gemini-3.8-flash",
                   "agy-sonnet-5.5": "claude-sonnet-5-5-medium"}
