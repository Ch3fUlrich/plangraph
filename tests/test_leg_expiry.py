"""Generic leg expiry and Sol role regressions, preserved after D-3817 removal."""
import json
from dataclasses import replace
from datetime import date
import pytest
from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER, leg_active, load_legs
from plangraph.planner.routing import plan_spawn
LEG_ID = "cx-gpt-6.1-sol"
CARD = {"estimate_tokens": 20000, "risk_tags": []}
def _leg():
    return replace(next(leg for leg in DEFAULT_LEGS if leg.id == LEG_ID), expires="2026-11-30")

def test_d3605_sol_routes_implement_never_review():
    assert WRITER_ORDER[0] == 'cx-gpt-6.1-sol'
    card = {"estimate_tokens": 20000, "risk_tags": []}
    implement = plan_spawn(card, role="implement")
    review = plan_spawn(card, role="review", writer_families=("mimo",))
    assert "cx-gpt-6.1-sol" in [leg.id for leg in implement.legs]
    assert "cx-gpt-6.1-sol" not in [leg.id for leg in review.legs]
    sol = next(leg for leg in DEFAULT_LEGS if leg.id == "cx-gpt-6.1-sol")
    assert leg_active(sol) and not sol.expires   # D-3605: operator implement leg again
    ids = [leg.id for leg in implement.legs]
    subscriptions = [l.id for l in DEFAULT_LEGS if l.id in WRITER_ORDER and l.tier == 'subscription']
    assert set(subscriptions) <= set(ids)
    assert [i for i in ids if i in subscriptions] == [i for i in WRITER_ORDER if i in subscriptions]
    for free_id in ('opencode-deepseek-v4.1-flash-free', 'opencode-glm-5.3-flash-free', 'openrouter-free'):
        for sub_id in WRITER_ORDER:
            if next(l for l in DEFAULT_LEGS if l.id == sub_id).tier == 'subscription':
                assert free_id in ids and sub_id in ids
                assert ids.index(free_id) < ids.index(sub_id)


def test_leg_active_utc_end_of_day():
    leg = _leg()
    assert leg_active(leg, today=date(2026, 11, 30)) is True
    assert leg_active(leg, today=date(2026, 12, 1)) is False


def test_expired_leg_absent_from_routing_candidates():
    free = next(leg for leg in DEFAULT_LEGS if leg.id == "openrouter-free")
    expired = replace(_leg(), expires="2020-01-01")
    plan = plan_spawn(CARD, role="implement", legs=(free, expired))
    assert [leg.id for leg in plan.legs] == [free.id]
    assert LEG_ID not in [row["leg"] for row in plan.candidates]
    assert LEG_ID not in [row["leg"] for row in plan.skipped]


def test_load_legs_accepts_expires_and_rejects_bad_format(tmp_path):
    raw = {"id": LEG_ID, "tier": "subscription", "harness": "opencode",
           "model": "omniroute/cx-gpt-6.1-sol", "family": "gpt",
           "windows": ["128k", "256k", "1m"], "max_request_tokens": 180000,
           "trains_on_data": False, "claude_family": False, "roles": ["implement"],
           "tool_mode": "tools", "expires": "2026-11-30"}
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"legs": [raw]}), encoding="utf-8")
    assert load_legs(good)[0].expires == "2026-11-30"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"legs": [dict(raw, expires="30-11-2026")]}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="expires"):
        load_legs(bad)


def test_override_cannot_spawn_an_expired_leg(tmp_path, monkeypatch):
    """D-3505 override refusal kept after D-3605 un-expired Sol (seat run-932dbd17 LOW)."""
    import asyncio
    from plangraph.planner import legs as legs_mod
    from plangraph.spawner.leg_state import LegStateStore
    from plangraph.spawner.runner import SpawnerRunner
    from plangraph.spawner.service import SpawnService
    expired = replace(_leg(), expires="2020-01-01")
    monkeypatch.setattr(legs_mod, "DEFAULT_LEGS",
                        tuple(expired if leg.id == LEG_ID else leg for leg in legs_mod.DEFAULT_LEGS))
    svc = SpawnService(SpawnerRunner(), tmp_path, LegStateStore(tmp_path / "legs.sqlite"))
    from plangraph.planner.routing import SpawnRefused
    with pytest.raises(SpawnRefused, match="^leg expired: " + LEG_ID.replace(".", r"\.") + "$"):
        asyncio.run(svc.spawn(card=CARD, prompt="x", role="implement", leg=LEG_ID,
                              override_reason="operator selection", dry_run=True))
