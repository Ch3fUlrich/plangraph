"""Console Sonnet writer leg (D-3455/D-3457): subscription writer, never a seat.

The leg serves the OpenCode console's Claude Sonnet credits (measured on the host:
``opencode models --standalone`` lists ``console-anthropic/claude-sonnet-5-5``;
the background service list does not).  It writes after every free leg, sits
behind mimo-v2.6-flash, and expires at the UTC end of 2026-11-30.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from plangraph.planner.legs import DEFAULT_LEGS, WRITER_ORDER, leg_active, load_legs
from plangraph.planner.routing import plan_spawn

SONNET = "opencode-console-sonnet-5.5"
CARD = {"estimate_tokens": 20000, "risk_tags": []}


def _leg():
    return next((leg for leg in DEFAULT_LEGS if leg.id == SONNET), None)


def test_console_sonnet_leg_shape():
    leg = _leg()
    assert leg is not None
    assert (leg.tier, leg.harness, leg.model) == (
        "subscription", "opencode", "console-anthropic/claude-sonnet-5-5")
    assert (leg.family, leg.claude_family, leg.trains_on_data) == ("claude", True, False)
    assert leg.windows == ("128k", "256k", "1m")
    assert leg.max_request_tokens == 180000
    assert leg.roles == ("implement",)
    assert leg.tool_mode == "tools"
    assert leg.expires == "2026-11-30"
    assert "--standalone" in leg.note


def test_writer_order_after_free_legs_and_before_cx():
    assert SONNET in WRITER_ORDER
    free_ids = {leg.id for leg in DEFAULT_LEGS if leg.tier == "free" and leg.id in WRITER_ORDER}
    assert free_ids
    assert all(WRITER_ORDER.index(free) < WRITER_ORDER.index(SONNET) for free in free_ids)
    assert WRITER_ORDER.index(SONNET) == WRITER_ORDER.index("mimo-v2.6-flash") + 1
    assert WRITER_ORDER.index(SONNET) + 1 == WRITER_ORDER.index("cx-gpt-6.1-sol")


def test_implement_route_ranks_sonnet_after_free_legs():
    plan = plan_spawn(CARD, role="implement")
    ids = [leg.id for leg in plan.legs]
    assert SONNET in ids
    free_positions = [ids.index(leg.id) for leg in plan.legs if leg.tier == "free"]
    assert free_positions
    assert ids.index(SONNET) > max(free_positions)


def test_review_route_never_returns_sonnet():
    for writer in ("gpt", "mimo", "gemini", "deepseek"):
        plan = plan_spawn(CARD, role="review", writer_families=(writer,))
        assert SONNET not in [leg.id for leg in plan.legs]
        assert SONNET not in [leg.id for leg in plan.review_seats]
        assert SONNET not in [row["leg"] for row in plan.candidates]


def test_leg_active_utc_end_of_day():
    leg = _leg()
    assert leg_active(leg, today=date(2026, 11, 30)) is True
    assert leg_active(leg, today=date(2026, 12, 1)) is False


def test_expired_leg_absent_from_routing_candidates():
    free = next(leg for leg in DEFAULT_LEGS if leg.id == "openrouter-free")
    expired = replace(_leg(), expires="2020-01-01")
    plan = plan_spawn(CARD, role="implement", legs=(free, expired))
    assert [leg.id for leg in plan.legs] == [free.id]
    assert SONNET not in [row["leg"] for row in plan.candidates]
    assert SONNET not in [row["leg"] for row in plan.skipped]


def test_load_legs_accepts_expires_and_rejects_bad_format(tmp_path):
    raw = {"id": SONNET, "tier": "subscription", "harness": "opencode",
           "model": "console-anthropic/claude-sonnet-5-5", "family": "claude",
           "windows": ["128k", "256k", "1m"], "max_request_tokens": 180000,
           "trains_on_data": False, "claude_family": True, "roles": ["implement"],
           "tool_mode": "tools", "expires": "2026-11-30"}
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"legs": [raw]}), encoding="utf-8")
    assert load_legs(good)[0].expires == "2026-11-30"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"legs": [dict(raw, expires="30-11-2026")]}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="expires"):
        load_legs(bad)
