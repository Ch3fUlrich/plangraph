"""Hermetic tests for the budget governor: full kind x initiator x burn x quota matrix."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plangraph.governor.policy import (
    BURN_LIMIT,
    CLAUDE,
    INITIATORS,
    START_KINDS,
    Decision,
    StaticSource,
    allow,
    parse_quota,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
DAY = "2026-10-16"


def src(burn=None, used=None, rate_limited=frozenset()):
    return StaticSource(
        burn_by_day={DAY: burn}, used={("p", DAY): used}, rate_limited=rate_limited
    )


def call(
    kind, who, source, *, quotas=None, provider=None, now=NOW, override=None,
    cb=None, fallbacks=(), fcb=None,
):
    return allow(kind, who, now, source=source, quotas=quotas, provider=provider,
                 override=override, on_override=cb, fallbacks=fallbacks,
                 on_fallback=fcb)


def test_constants():
    assert START_KINDS == ("planner", "expert", "judge", "answer", "worker_leg")
    assert INITIATORS == ("operator", "agent")
    assert BURN_LIMIT == 1.0
    assert CLAUDE == "claude"


def test_decision_as_tuple():
    d = Decision(allow=True, reason="r")
    assert d.as_tuple() == (True, "r")


def test_decision_fallback_default():
    assert Decision(allow=True, reason="r").fallback is None


@pytest.mark.parametrize("kind", START_KINDS)
def test_operator_never_blocks_any_kind(kind):
    d = call(kind, "operator", src(burn=99), provider="p", quotas={"p": 1})
    assert d.as_tuple() == (True, "operator_initiated")
    assert d.initiator == "operator" and d.start_kind == kind


def test_operator_worker_leg_without_provider_allows():
    assert call("worker_leg", "operator", src(), provider=None).as_tuple() == (
        True, "operator_initiated")


@pytest.mark.parametrize("who,exp_reason", [
    ("agent", "answer_always"),
    ("operator", "operator_initiated"),
])
def test_answer_always(who, exp_reason):
    d = call("answer", who, src(burn=99))
    assert d.allow is True
    assert d.reason == exp_reason


@pytest.mark.parametrize("burn,exp_allow,exp_reason", [
    (None, True, "burn_unknown"),
    (0.5, True, "burn_ok"),
    (1.0, True, "burn_ok"),        # boundary: equal limit allows
    (1.0001, False, "claude_burn_over_1_7"),  # boundary: just over denies
    (1.23, False, "claude_burn_over_1_7"),
])
@pytest.mark.parametrize("kind", ["planner", "expert", "judge"])
def test_agent_burn_gate(kind, burn, exp_allow, exp_reason):
    d = call(kind, "agent", src(burn=burn))
    assert d.allow is exp_allow
    assert d.reason.startswith(exp_reason)
    assert d.burn == burn  # echoes the consulted value (or None)


def test_burn_deny_message_formats_value():
    d = call("planner", "agent", src(burn=1.23))
    assert d.reason == "claude_burn_over_1_7 (burn=1.23)"


def test_claude_rate_limited_denies_planner():
    d = call("planner", "agent", src(burn=0.5, rate_limited=frozenset({CLAUDE})))
    assert d.allow is False
    assert d.reason == "claude_rate_limited"


def test_burn_over_wins_reason_over_rate_limit():
    s = src(burn=1.23, rate_limited=frozenset({CLAUDE}))
    d = call("judge", "agent", s)
    assert d.allow is False and d.reason.startswith("claude_burn_over_1_7")


def test_judge_agent_treated_like_planner():
    assert call("judge", "agent", src(burn=1.23)).allow is False
    assert call("judge", "agent", src(burn=0.5)).reason == "burn_ok"


def test_agent_worker_leg_requires_provider():
    with pytest.raises(ValueError):
        call("worker_leg", "agent", src(), provider=None)


@pytest.mark.parametrize("quotas,used,exp_allow,exp_prefix", [
    (None, 1.0, True, "no_quota_configured"),
    ({"other": 10}, 1.0, True, "no_quota_configured"),
    ({"p": 100}, None, True, "quota_unknown"),
    ({"p": 100}, 50.0, True, "quota_ok"),
    ({"p": 100}, 99.0, True, "quota_ok"),
    ({"p": 100}, 100.0, False, "provider_quota_exhausted"),  # equal is exhausted
    ({"p": 100}, 150.0, False, "provider_quota_exhausted"),
])
def test_agent_worker_leg_quota_matrix(quotas, used, exp_allow, exp_prefix):
    d = call("worker_leg", "agent", src(burn=99, used=used), provider="p", quotas=quotas)
    assert d.allow is exp_allow
    assert d.reason.startswith(exp_prefix)


def test_worker_leg_burn_never_blocks():
    d = call("worker_leg", "agent", src(burn=99, used=1.0), provider="p", quotas={"p": 100})
    assert d.as_tuple() == (True, "quota_ok")


def test_quota_deny_message():
    d = call("worker_leg", "agent", src(used=100), provider="p", quotas={"p": 100})
    assert d.reason == "provider_quota_exhausted (used=100.00,limit=100.00)"


def test_worker_leg_rate_limited_denies():
    s = src(rate_limited=frozenset({"p"}))
    d = call("worker_leg", "agent", s, provider="p", quotas={"p": 100})
    assert d.allow is False
    assert d.reason.startswith("provider_rate_limited")
    assert d.fallback is None


def test_worker_leg_rate_limited_denies_even_without_quota():
    s = src(rate_limited=frozenset({"claude"}))
    d = call("worker_leg", "agent", s, provider=CLAUDE, quotas=None)
    assert d.allow is False and d.reason.startswith("provider_rate_limited")


def test_unknown_kind_and_initiator_raise():
    with pytest.raises(ValueError):
        call("mystery", "agent", src())
    with pytest.raises(ValueError):
        call("planner", "robot", src())


def test_override_flips_deny_and_logs_once():
    seen: list[Decision] = []
    d = call("planner", "agent", src(burn=1.23), override="ops said yes", cb=seen.append)
    assert d.allow is True
    assert d.override is True
    assert d.reason == "override: ops said yes"
    assert seen and seen[0] is d and len(seen) == 1


def test_override_strips_and_reuses_context():
    seen: list[Decision] = []
    d = call("planner", "agent", src(burn=1.23), override="  go  ", cb=seen.append)
    assert d.reason == "override: go"
    assert d.start_kind == "planner" and d.initiator == "agent" and d.burn == 1.23


def test_override_on_allow_is_ignored_no_log():
    seen: list[Decision] = []
    d = call("planner", "agent", src(burn=0.5), override="x", cb=seen.append)
    assert d.as_tuple() == (True, "burn_ok")
    assert d.override is False and seen == []


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_override_ignored(blank):
    d = call("planner", "agent", src(burn=1.23), override=blank)
    assert d.allow is False and d.reason.startswith("claude_burn_over_1_7")


def test_override_survives_callback_exception():
    seen: list[Decision] = []

    def boom(_d):
        seen.append(_d)
        raise RuntimeError("callback blew up")

    d = call("planner", "agent", src(burn=1.23), override="go", cb=boom)
    assert d.allow is True and d.override is True
    assert d.reason == "override: go" and len(seen) == 1


def test_naive_now_treated_as_utc():
    naive = datetime(2026, 10, 16, 0, 30)
    d = call("planner", "agent", src(burn=1.23), now=naive)
    assert d.reason.startswith("claude_burn_over_1_7")


def test_aware_non_utc_normalizes_to_utc_day():
    now = datetime(2026, 10, 16, 1, 0, tzinfo=timezone(timedelta(hours=5)))
    d = call("planner", "agent", StaticSource(burn_by_day={"2026-10-15": 1.23}), now=now)
    assert d.reason.startswith("claude_burn_over_1_7")


def test_parse_quota_valid():
    assert parse_quota("a=100,b=2.5e6") == {"a": 100.0, "b": 2500000.0}


@pytest.mark.parametrize("empty", ["", None, "   "])
def test_parse_quota_empty(empty):
    assert parse_quota(empty) == {}


@pytest.mark.parametrize("bad", ["abc", "=5", "a=x", "a=-1", "a=", "a=1,b", "a=1,,b=2"])
def test_parse_quota_malformed_raises(bad):
    with pytest.raises(ValueError):
        parse_quota(bad)


def test_static_source_implements_protocol():
    s = StaticSource(burn_by_day={"2026-10-16": 0.5}, used={("p", "2026-10-16"): 10})
    assert s.burn("2026-10-16") == 0.5
    assert s.quota_used("p", "2026-10-16") == 10
    assert s.burn("nope") is None and s.quota_used("q", "2026-10-16") is None


def test_static_source_rate_limited_field():
    s = StaticSource(rate_limited=frozenset({"claude"}))
    assert CLAUDE in s.rate_limited and "gpt" not in s.rate_limited
    assert StaticSource().rate_limited == frozenset()


# ---- D-1533: non-Claude fallback path ----

def test_fallback_on_burn_over_allows_and_logs_once():
    seen: list[Decision] = []
    d = call("planner", "agent", src(burn=1.23), fallbacks=["gpt"], fcb=seen.append)
    assert d.allow is True and d.fallback == "gpt" and d.provider == "gpt"
    assert d.reason == "fallback: gpt (claude_burn_over_1_7 (burn=1.23))"
    assert d.burn == 1.23
    assert seen == [d] and len(seen) == 1


def test_fallback_on_rate_limited_allows():
    seen: list[Decision] = []
    s = src(burn=0.5, rate_limited=frozenset({CLAUDE}))
    d = call("expert", "agent", s, fallbacks=["gemini"], fcb=seen.append)
    assert d.allow is True and d.fallback == "gemini"
    assert d.reason == "fallback: gemini (claude_rate_limited)"
    assert seen == [d]


@pytest.mark.parametrize("kind", ["planner", "expert", "judge"])
def test_fallback_applies_to_all_claude_legs(kind):
    d = call(kind, "agent", src(burn=1.23), fallbacks=["gpt"])
    assert d.allow is True and d.fallback == "gpt"


def test_fallback_skips_exhausted_leg_picks_next():
    s = StaticSource(burn_by_day={DAY: 1.23}, used={("gpt", DAY): 100.0})
    d = call("planner", "agent", s, quotas={"gpt": 100.0, "gemini": 100.0},
             fallbacks=["gpt", "gemini"])
    assert d.allow is True and d.fallback == "gemini"
    assert d.reason == "fallback: gemini (claude_burn_over_1_7 (burn=1.23))"


def test_fallback_skips_throttled_leg():
    s = StaticSource(burn_by_day={DAY: 1.23}, rate_limited=frozenset({"gpt"}))
    d = call("planner", "agent", s, fallbacks=["gpt", "gemini"])
    assert d.allow is True and d.fallback == "gemini"


def test_fallback_none_usable_denies_as_before():
    s = StaticSource(burn_by_day={DAY: 1.23}, used={("gpt", DAY): 250.0})
    d = call("planner", "agent", s, quotas={"gpt": 100.0}, fallbacks=["gpt"])
    assert d.allow is False and d.fallback is None
    assert d.reason.startswith("claude_burn_over_1_7")


def test_fallback_ignores_claude_only_list():
    d = call("planner", "agent", src(burn=1.23), fallbacks=[CLAUDE])
    assert d.allow is False and d.fallback is None


def test_fallback_empty_list_denies():
    d = call("planner", "agent", src(burn=1.23), fallbacks=())
    assert d.allow is False and d.fallback is None
    assert d.reason.startswith("claude_burn_over_1_7")


def test_fallback_not_used_for_operator():
    seen: list[Decision] = []
    d = call("planner", "operator", src(burn=99), provider="p", quotas={"p": 1},
             fallbacks=["gpt"], fcb=seen.append)
    assert d.as_tuple() == (True, "operator_initiated")
    assert d.fallback is None and seen == []


def test_fallback_not_used_for_answer():
    seen: list[Decision] = []
    d = call("answer", "agent", src(burn=1.23), fallbacks=["gpt"], fcb=seen.append)
    assert d.as_tuple() == (True, "answer_always")
    assert d.fallback is None and seen == []


def test_worker_leg_quota_exhausted_has_no_fallback():
    s = StaticSource(burn_by_day={DAY: 0.0}, used={("claude", DAY): 100.0})
    d = call("worker_leg", "agent", s, provider=CLAUDE, quotas={CLAUDE: 100.0},
             fallbacks=["gpt"])
    assert d.allow is False and d.fallback is None
    assert d.reason.startswith("provider_quota_exhausted")


def test_fallback_chosen_before_override_and_override_not_logged():
    over: list[Decision] = []
    fb: list[Decision] = []
    d = call("planner", "agent", src(burn=1.23), override="go", cb=over.append,
             fallbacks=["gpt"], fcb=fb.append)
    assert d.allow is True and d.fallback == "gpt" and d.override is False
    assert d.reason == "fallback: gpt (claude_burn_over_1_7 (burn=1.23))"
    assert fb == [d] and over == []


def test_override_still_applies_when_no_usable_fallback():
    s = StaticSource(burn_by_day={DAY: 1.23}, used={("gpt", DAY): 999.0})
    over: list[Decision] = []
    d = call("planner", "agent", s, quotas={"gpt": 100.0}, fallbacks=["gpt"],
             override="ops", cb=over.append)
    assert d.allow is True and d.override is True and d.fallback is None
    assert d.reason == "override: ops"
    assert over == [d]


def test_fallback_callback_exception_still_allows_with_fallback():
    seen: list[Decision] = []

    def boom(_d):
        seen.append(_d)
        raise RuntimeError("callback blew up")

    d = call("planner", "agent", src(burn=1.23), fallbacks=["gpt"], fcb=boom)
    assert d.allow is True and d.fallback == "gpt"
    assert len(seen) == 1
