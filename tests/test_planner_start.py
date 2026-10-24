"""Tests for the governor-backed planner start gate (D-1533)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from plangraph.governor import policy
from plangraph.planner.legs import DEFAULT_LEGS, Leg
from plangraph.planner.start import StartGate, fallback_providers, guard_start

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
DAY = NOW.date().isoformat()


def _leg(id, family, *, claude=False):
    return Leg(
        id=id,
        tier="free",
        harness="opencode",
        model=id,
        family=family,
        windows=("128k",),
        max_request_tokens=100000,
        trains_on_data=False,
        claude_family=claude,
    )


def _burn_source(**extra):
    return policy.StaticSource(burn_by_day={DAY: 2.0}, **extra)


def test_burn_over_falls_back_to_first_family():
    gate = guard_start("planner", "agent", NOW, source=_burn_source())
    assert isinstance(gate, StartGate)
    assert gate.allowed
    assert gate.fallback == "qwen"
    assert gate.leg is DEFAULT_LEGS[0]


def test_exhausted_first_family_moves_to_next():
    source = policy.StaticSource(
        burn_by_day={DAY: 2.0},
        used={("qwen", DAY): 10},
    )
    gate = guard_start("planner", "agent", NOW, source=source, quotas={"qwen": 10})
    assert gate.allowed
    assert gate.fallback == "muse"
    assert gate.leg.family == "muse"


def test_every_non_claude_exhausted_denies():
    families = [p for p in fallback_providers(DEFAULT_LEGS)]
    used = {(f, DAY): 1 for f in families}
    source = policy.StaticSource(burn_by_day={DAY: 2.0}, used=used)
    gate = guard_start(
        "planner", "agent", NOW, source=source, quotas={f: 1 for f in families}
    )
    assert not gate.allowed
    assert gate.fallback is None
    assert gate.leg is None


def test_claude_rate_limited_falls_back():
    source = policy.StaticSource(rate_limited=frozenset({"claude"}))
    gate = guard_start(
        "planner", "agent", NOW, source=source, provider="claude"
    )
    assert gate.allowed
    assert gate.fallback == "qwen"
    assert gate.leg.family == "qwen"


def test_operator_initiated_allowed_no_fallback():
    calls: list = []
    gate = guard_start(
        "planner",
        "operator",
        NOW,
        source=policy.StaticSource(),
        on_fallback=calls.append,
    )
    assert gate.allowed
    assert gate.decision.reason == "operator_initiated"
    assert gate.leg is None
    assert calls == []


def test_answer_always_allowed():
    gate = guard_start(
        "answer", "agent", NOW, source=policy.StaticSource()
    )
    assert gate.allowed


def test_worker_with_exhausted_provider_denies():
    source = policy.StaticSource(used={("qwen", DAY): 1})
    gate = guard_start(
        "worker_leg",
        "agent",
        NOW,
        source=source,
        provider="qwen",
        quotas={"qwen": 1},
    )
    assert not gate.allowed
    assert gate.leg is None


def test_claude_leg_never_selected_even_if_first():
    legs = (
        _leg("claude-first", "claude", claude=True),
        _leg("qwen-second", "qwen"),
        _leg("muse-third", "muse"),
    )
    gate = guard_start("planner", "agent", NOW, source=_burn_source(), legs=legs)
    assert gate.fallback == "qwen"
    assert gate.leg is legs[1]
    assert not gate.leg.claude_family


def test_on_fallback_called_once_with_fallback():
    calls: list = []
    gate = guard_start(
        "planner", "agent", NOW, source=_burn_source(), on_fallback=calls.append
    )
    assert gate.fallback is not None
    assert calls == [gate.decision]


def test_raising_fallback_callback_does_not_block():
    def boom(_provider):
        raise RuntimeError("callback failed")

    gate = guard_start(
        "planner", "agent", NOW, source=_burn_source(), on_fallback=boom
    )
    assert gate.allowed
    assert gate.fallback is not None


def test_fallback_providers_dedups_and_skips_claude():
    assert "claude" not in fallback_providers(DEFAULT_LEGS)
    assert fallback_providers(DEFAULT_LEGS)[:3] == ("qwen", "muse", "north")
    dup = (
        _leg("a", "qwen"),
        _leg("b", "qwen"),
        _leg("c", "claude", claude=True),
        _leg("d", "muse"),
    )
    assert fallback_providers(dup) == ("qwen", "muse")
    assert fallback_providers(()) == ()


def test_guard_start_does_not_raise_on_deny():
    source = policy.StaticSource(used={("qwen", DAY): 5})
    gate = guard_start(
        "worker_leg", "agent", NOW, source=source, provider="qwen",
        quotas={"qwen": 5},
    )
    assert isinstance(gate, StartGate)
    assert gate.allowed is gate.decision.allow
    assert gate.fallback == gate.decision.fallback


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
