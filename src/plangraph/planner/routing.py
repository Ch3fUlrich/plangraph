"""Keeper-side routing (design 4.3, 5; D25): pick a window class and an ordered leg plan.

``route_card`` turns a card into a :class:`Route`: the window class (the card's own, or
the smallest class that holds the effective estimate at 55% of the window), the eligible
legs (tier order, then ``legs.py`` order) and the review level.  ``next_after_failure``
walks the plan after a failed attempt (design 4.5): rotate inside a tier on a provider
problem, retry on the same leg while the tier budget lasts, then promote to the next
tier, else park.  The window class only grows on a context overflow.  Pure functions,
no I/O.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from plangraph.keeper.schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES

from .legs import DEFAULT_LEGS, TIERS, Leg

__all__ = ["RouteError", "Route", "effective_estimate", "pick_window_class",
           "route_card", "next_after_failure", "WINDOW_TOKENS", "USABLE_FRACTION"]

#: Window sizes in tokens.
WINDOW_TOKENS = {"128k": 131072, "256k": 262144, "1m": 1048576}
#: Share of a window a card may use (design number).
USABLE_FRACTION_PCT = 55
USABLE_FRACTION = USABLE_FRACTION_PCT / 100
#: Attempts on one tier before promotion, and promotions allowed.
ATTEMPT_BUDGET = 3
PROMOTIONS = 1
#: Risk tags that keep training-on-data legs away from the card.
_NO_TRAIN_TAGS = frozenset({"auth", "secrets", "money", "live-step"})
#: Exit classes where the provider, not the card, failed: rotate inside the tier.
_PROVIDER_EXITS = frozenset({"usage_limit", "outage"})
#: Exit classes that leave the routing loop (planner queue or inbox).
_PARK_EXITS = frozenset({"contract_defect", "needs_input", "flaky_check", "cancelled"})


class RouteError(ValueError):
    """No usable route (estimate too big, or no eligible leg)."""


@dataclass(frozen=True)
class Route:
    """The routing decision for one card."""

    window_class: str
    harness: str
    legs: tuple[Leg, ...]
    attempt_budget: int
    promotions: int
    review_level: str
    review_seats: int
    cross_family: bool
    reasons: tuple[str, ...]

    @property
    def first_leg(self) -> Leg:
        return self.legs[0]


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def effective_estimate(estimate_tokens: int | None, subtasks: int = 1) -> int:
    """Estimate scaled by ``1 + 0.1 * (subtasks - 1)``, rounded up.

    Raises ``ValueError`` when the estimate is missing or not a positive int, or when
    ``subtasks`` is below 1.
    """
    if estimate_tokens is None:
        raise ValueError("estimate_tokens is required for routing")
    if not _is_int(estimate_tokens) or estimate_tokens <= 0:
        raise ValueError("estimate_tokens must be a positive int")
    if not _is_int(subtasks) or subtasks < 1:
        raise ValueError("subtasks must be an int >= 1")
    # integer arithmetic: ceil(estimate * (10 + subtasks - 1) / 10), no float drift
    return -(-estimate_tokens * (9 + subtasks) // 10)


def _fits(effective: int, window_class: str) -> bool:
    return effective * 100 <= USABLE_FRACTION_PCT * WINDOW_TOKENS[window_class]


def pick_window_class(effective: int, floor: str | None = None) -> str:
    """Smallest class whose 55% share holds ``effective``, never below ``floor``.

    Raises ``RouteError("estimate too big")`` when even the 1m class is too small.
    """
    if floor is not None and floor not in WINDOW_CLASSES:
        raise ValueError(f"floor must be one of {WINDOW_CLASSES}")
    start = WINDOW_CLASSES.index(floor) if floor is not None else 0
    for window_class in WINDOW_CLASSES[start:]:
        if _fits(effective, window_class):
            return window_class
    raise RouteError("estimate too big")


def _review(tags: Sequence[str]) -> tuple[str, int]:
    return ("risky", 2) if tags else ("standard", 1)


def route_card(card: Mapping, *, legs: Sequence[Leg] = DEFAULT_LEGS, subtasks: int = 1,
               unavailable: Collection[str] = (), exclude_families: Collection[str] = (),
               overflow: bool = False) -> Route:
    """Route ``card`` (keeper card dict) over ``legs``; see module docstring.

    ``overflow`` marks a context-overflow failure: the window class grows by one.
    Raises ``RouteError`` when no leg is eligible, ``ValueError`` on a bad card.
    """
    effective = effective_estimate(card.get("estimate_tokens"), subtasks)
    tags = list(card.get("risk_tags") or [])
    bad = [t for t in tags if t not in RISK_TAGS]
    if bad:
        raise ValueError(f"unknown risk tags: {bad}")
    window_class = card.get("window_class")
    if window_class is None:
        window_class = pick_window_class(effective)
    elif window_class not in WINDOW_CLASSES:
        raise ValueError(f"window_class must be one of {WINDOW_CLASSES}")
    reasons: list[str] = []
    if overflow:
        idx = WINDOW_CLASSES.index(window_class)
        if idx + 1 >= len(WINDOW_CLASSES):
            raise RouteError(f"context overflow on {window_class}: no larger window class")
        window_class = WINDOW_CLASSES[idx + 1]
        reasons.append(f"overflow: window class upgraded to {window_class}")

    no_train = bool(_NO_TRAIN_TAGS.intersection(tags))
    window_fits = _fits(effective, window_class)
    dropped: dict[str, int] = {}
    eligible: list[tuple[int, int, Leg]] = []
    for pos, leg in enumerate(legs):
        if leg.id in unavailable:
            why = "unavailable"
        elif leg.family in exclude_families:
            why = "excluded_family"
        elif no_train and leg.trains_on_data:
            why = "trains_on_data"
        elif window_class not in leg.windows:
            why = "window_class"
        elif not window_fits:
            why = "over_window_share"
        elif effective > leg.max_request_tokens:
            why = "max_request_tokens"
        else:
            eligible.append((TIERS.index(leg.tier), pos, leg))
            continue
        dropped[why] = dropped.get(why, 0) + 1
    for why, count in dropped.items():
        reasons.append(f"excluded {count} legs: {why}")
    if not eligible:
        raise RouteError(f"no eligible leg for {window_class} (effective {effective}): "
                         + "; ".join(reasons))
    eligible.sort(key=lambda t: (t[0], t[1]))
    plan = tuple(leg for _, _, leg in eligible)
    level, seats = _review(tags)
    return Route(window_class=window_class, harness=plan[0].harness, legs=plan,
                 attempt_budget=ATTEMPT_BUDGET, promotions=PROMOTIONS,
                 review_level=level, review_seats=seats, cross_family=True,
                 reasons=tuple(reasons))


def next_after_failure(route: Route, tried: Sequence[str], exit_class: str,
                       tier_attempts: int) -> tuple[str, Leg | None]:
    """Decide the next step after a failed attempt (design 4.5).

    ``tried`` holds leg ids in attempt order (last = the failed leg; empty = the first
    leg), ``tier_attempts`` the attempts already spent on that leg's tier.  Returns
    ``(action, leg)`` with action ``retry`` / ``rotate`` / ``promote`` (leg given) or
    ``wait`` / ``park`` (leg ``None``).
    """
    if exit_class not in EXIT_CLASSES or exit_class == "ok":
        raise ValueError(f"not a failure exit class: {exit_class!r}")
    if exit_class in _PARK_EXITS:
        return ("park", None)
    by_id = {leg.id: leg for leg in route.legs}
    unknown = [t for t in tried if t not in by_id]
    if unknown:
        raise ValueError(f"tried legs not in the plan: {unknown}")
    last = by_id[tried[-1]] if tried else route.first_leg
    if exit_class in _PROVIDER_EXITS:
        for leg in route.legs:
            if leg.tier == last.tier and leg.id not in tried:
                return ("rotate", leg)
        return ("wait", None)
    if tier_attempts < route.attempt_budget:
        return ("retry", last)
    rank = TIERS.index(last.tier)
    for leg in route.legs:
        if TIERS.index(leg.tier) > rank:
            return ("promote", leg)
    return ("park", None)
