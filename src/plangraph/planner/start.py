"""Wire the budget governor into the planner start path (operator D-1533).

Orchestration is model-family agnostic; Claude is just one leg. When Claude
is over burn or rate limited, an agent planner/expert/judge start falls back
to a non-Claude leg (signalled to the caller via ``on_fallback``) instead of
only being blocked. This module owns no I/O and never imports the keeper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from plangraph.governor import policy

from .legs import DEFAULT_LEGS, Leg

__all__ = ["StartGate", "fallback_providers", "guard_start"]


@dataclass(frozen=True)
class StartGate:
    """A governor verdict paired with the concrete leg to run on."""

    decision: policy.Decision
    leg: Leg | None

    @property
    def allowed(self) -> bool:
        """True when the start is permitted, by any route."""
        return self.decision.allow

    @property
    def fallback(self) -> str | None:
        """The non-Claude family chosen when the primary was blocked."""
        return self.decision.fallback


def fallback_providers(legs: Sequence[Leg]) -> tuple[str, ...]:
    """Return unique non-Claude leg families in ``legs`` order.

    The governor's provider key is a leg ``family``, so only ``claude_family``
    legs are dropped; duplicates collapse to their first occurrence.
    """
    seen: set[str] = set()
    out: list[str] = []
    for leg in legs:
        if leg.claude_family:
            continue
        if leg.family in seen:
            continue
        seen.add(leg.family)
        out.append(leg.family)
    return tuple(out)


def _leg_for_fallback(legs: Sequence[Leg], fallback: str) -> Leg | None:
    """First non-Claude leg whose family matches ``fallback`` (else None)."""
    for leg in legs:
        if not leg.claude_family and leg.family == fallback:
            return leg
    return None


def guard_start(
    start_kind: str,
    initiator: str,
    now: datetime,
    *,
    source: policy.Source,
    quotas: Mapping[str, float] | None = None,
    legs: Sequence[Leg] = DEFAULT_LEGS,
    provider: str | None = None,
    override: str | None = None,
    on_override=None,
    on_fallback=None,
) -> StartGate:
    """Gate a start, falling back to a non-Claude leg when Claude is blocked.

    Never raises on a deny: the caller reads ``.allowed``. When a fallback is
    chosen, ``.leg`` is the matching non-Claude leg; otherwise ``.leg`` is
    None and the caller keeps its normal routing.
    """
    decision = policy.allow(
        start_kind,
        initiator,
        now,
        source=source,
        quotas=quotas,
        provider=provider,
        override=override,
        on_override=on_override,
        fallbacks=fallback_providers(legs),
        on_fallback=on_fallback,
    )
    leg = _leg_for_fallback(legs, decision.fallback) if decision.fallback else None
    return StartGate(decision=decision, leg=leg)
