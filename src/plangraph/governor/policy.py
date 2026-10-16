"""Budget governor: decide whether a run may start based on burn and quota.

Pure and hermetic: every decision comes from the injected ``Source`` and the
caller-supplied clock. Unknown data fails OPEN (allow) so the governor never
strands a run on a data gap. It NEVER blocks answering questions, an
operator-initiated session, or an operator-requested judge. ``None`` is used as
the "no override" sentinel; a blank/whitespace override string is ignored.

Operator D-1533: orchestration works with any model family, Claude is only one
leg. When a Claude-only gate (burn/rate-limit) would block an agent planner,
expert or judge, the governor first tries the caller-supplied ``fallbacks`` for a
usable non-Claude leg and logs the substitution; only if none is usable does the
deny stand (so overrides can still apply on top of it).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol

__all__ = [
    "START_KINDS",
    "INITIATORS",
    "BURN_LIMIT",
    "CLAUDE",
    "Decision",
    "Source",
    "StaticSource",
    "parse_quota",
    "allow",
]

START_KINDS = ("planner", "expert", "judge", "answer", "worker_leg")
INITIATORS = ("operator", "agent")
BURN_LIMIT = 1.0  # burn is weighted-tokens / (weekly-cap / 7); 1.0 is the gate.
CLAUDE = "claude"  # the single leg the burn gate guards; fallbacks avoid it.

# planner / expert / judge share the burn gate; these are the kinds that may fall
# back to another leg. Worker legs are chosen by the router and never fall back.
_FALLBACK_KINDS = ("planner", "expert", "judge")


@dataclass(frozen=True)
class Decision:
    """Immutable verdict for one start request.

    Trailing fields carry defaults so the documented field order is valid.
    """

    allow: bool
    reason: str
    override: bool = False
    start_kind: str = ""
    initiator: str = ""
    provider: str | None = None
    burn: float | None = None
    fallback: str | None = None

    def as_tuple(self) -> tuple[bool, str]:
        """Return ``(allow, reason)`` only."""
        return (self.allow, self.reason)


class Source(Protocol):
    """Day is a UTC ``YYYY-MM-DD`` string."""

    def burn(self, day: str) -> float | None: ...

    def quota_used(self, provider: str, day: str) -> float | None: ...

    def rate_limited(self, provider: str) -> bool | None: ...


@dataclass(frozen=True)
class StaticSource:
    """In-memory ``Source`` for tests and the CLI.

    ``rate_limited`` is the set of currently throttled providers; it is the
    optional surface the governor reads back for the burn/fallback logic.
    """

    burn_by_day: Mapping[str, float | None] = field(default_factory=dict)
    used: Mapping[tuple[str, str], float | None] = field(default_factory=dict)
    rate_limited: frozenset = frozenset()

    def burn(self, day: str) -> float | None:
        return self.burn_by_day.get(day)

    def quota_used(self, provider: str, day: str) -> float | None:
        return self.used.get((provider, day))


def parse_quota(text: str | None) -> dict[str, float]:
    """Parse ``"a=100,b=2.5e6"`` to ``{"a": 100.0, "b": 2500000.0}``.

    Empty/None input yields ``{}``; a malformed pair or a negative value raises.
    """
    if text is None or not text.strip():
        return {}
    result: dict[str, float] = {}
    for pair in text.split(","):
        key, sep, value = pair.partition("=")
        if not sep:
            raise ValueError(f"malformed quota pair: {pair!r}")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"malformed quota pair: {pair!r}")
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"malformed quota value: {pair!r}") from None
        if number < 0:
            raise ValueError(f"negative quota: {pair!r}")
        result[key] = number
    return result


def _utc_day(now: datetime) -> str:
    """Normalize an aware or naive datetime to its UTC calendar day (naive = UTC)."""
    if now.tzinfo is None:
        return now.date().isoformat()
    return now.astimezone(timezone.utc).date().isoformat()


def _rate_limited(source: Source, provider: str) -> bool | None:
    """Whether ``provider`` is throttled: True only for a definite signal.

    Protocol sources expose a callable ``rate_limited``; ``StaticSource`` instead
    carries the throttled providers as a frozenset, so the same accessor works for
    either without forcing a method onto the in-memory source.
    """
    attr = getattr(source, "rate_limited", None)
    if attr is None:
        return None
    if callable(attr):
        return attr(provider)
    return provider in attr


def _usable_fallback(p: str, day: str, source: Source, quotas: Mapping[str, float] | None) -> bool:
    """A non-Claude fallback leg is usable when not throttled and within quota."""
    if p == CLAUDE:
        return False
    if _rate_limited(source, p) is True:
        return False
    limit = (quotas or {}).get(p)
    if limit is None:
        return True
    used = source.quota_used(p, day)
    return used is None or used < limit


def _pick_fallback(
    fallbacks: Sequence[str], day: str, source: Source, quotas: Mapping[str, float] | None
) -> str | None:
    """First provider in ``fallbacks`` that is a usable non-Claude leg, else None."""
    for p in fallbacks:
        if _usable_fallback(p, day, source, quotas):
            return p
    return None


def _decide(
    start_kind: str,
    initiator: str,
    now: datetime,
    source: Source,
    quotas: Mapping[str, float] | None,
    provider: str | None,
) -> Decision:
    """Produce the base verdict (never a fallback, never an override)."""
    if start_kind not in START_KINDS:
        raise ValueError(f"unknown start_kind: {start_kind!r}")
    if initiator not in INITIATORS:
        raise ValueError(f"unknown initiator: {initiator!r}")

    day = _utc_day(now)
    ctx = {"start_kind": start_kind, "initiator": initiator, "provider": provider}

    # Operator-initiated is never blocked, for any kind.
    if initiator == "operator":
        return Decision(allow=True, reason="operator_initiated", burn=None, **ctx)
    # Answering is never blocked, for any initiator.
    if start_kind == "answer":
        return Decision(allow=True, reason="answer_always", burn=None, **ctx)

    # Remaining paths are agent-initiated.
    if start_kind == "worker_leg":
        if provider is None:
            raise ValueError("provider is required for worker_leg")
        if _rate_limited(source, provider) is True:
            reason = f"provider_rate_limited (provider={provider})"
            return Decision(allow=False, reason=reason, burn=None, **ctx)
        limit = (quotas or {}).get(provider)
        if limit is None:
            return Decision(allow=True, reason="no_quota_configured", burn=None, **ctx)
        used = source.quota_used(provider, day)
        if used is None:
            return Decision(allow=True, reason="quota_unknown", burn=None, **ctx)
        if used >= limit:
            reason = f"provider_quota_exhausted (used={used:.2f},limit={limit:.2f})"
            return Decision(allow=False, reason=reason, burn=None, **ctx)
        return Decision(allow=True, reason="quota_ok", burn=None, **ctx)

    # planner / expert / judge share the Claude burn gate; judge is not privileged.
    # A block here is a Claude-only limitation, so allow() retries it on a fallback.
    burn = source.burn(day)
    blocked: str | None = None
    if burn is not None and burn > BURN_LIMIT:
        blocked = f"claude_burn_over_1_7 (burn={burn:.2f})"
    elif _rate_limited(source, CLAUDE) is True:
        blocked = "claude_rate_limited"
    if blocked is not None:
        return Decision(allow=False, reason=blocked, burn=burn, **ctx)
    if burn is None:
        return Decision(allow=True, reason="burn_unknown", burn=None, **ctx)
    return Decision(allow=True, reason="burn_ok", burn=burn, **ctx)


def allow(
    start_kind: str,
    initiator: str,
    now: datetime,
    *,
    source: Source,
    quotas: Mapping[str, float] | None = None,
    provider: str | None = None,
    override: str | None = None,
    on_override=None,
    fallbacks: Sequence[str] = (),
    on_fallback=None,
) -> Decision:
    """Return a ``Decision`` for whether ``start_kind`` may start right now.

    ``now`` may be timezone-aware or naive (treated as UTC). A blocked agent
    planner/expert/judge first tries the non-Claude ``fallbacks`` (logging once via
    ``on_fallback``); if none is usable the deny stands. ``override`` is a non-empty
    reason that flips a (fallback-less) deny into an allow (logged once via
    ``on_override``); an allow base ignores it.
    """
    base = _decide(start_kind, initiator, now, source, quotas, provider)

    if (
        not base.allow
        and base.initiator == "agent"
        and base.start_kind in _FALLBACK_KINDS
    ):
        leg = _pick_fallback(fallbacks, _utc_day(now), source, quotas)
        if leg is not None:
            d = Decision(
                allow=True,
                reason=f"fallback: {leg} ({base.reason})",
                start_kind=base.start_kind,
                initiator=base.initiator,
                provider=leg,
                burn=base.burn,
                fallback=leg,
            )
            if on_fallback is not None:
                try:
                    on_fallback(d)  # a logging failure must never block the start.
                except Exception:
                    pass
            return d

    text = override.strip() if override else ""
    if base.allow or not text:
        return base
    final = replace(base, allow=True, reason=f"override: {text}", override=True)
    if on_override is not None:
        try:
            on_override(final)  # exceptions must never block the start itself.
        except Exception:
            pass
    return final
