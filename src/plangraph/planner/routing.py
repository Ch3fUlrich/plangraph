"""Keeper routing and spawner boundary (design 4.3, 5; D25)."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, asdict, field, replace
from plangraph.routing_support.liveness import read_leg_model, unavailable
import json
import os
from pathlib import Path
import time

from plangraph.keeper.schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES
from plangraph.routing_errors import RouteError, SpawnRefused
from plangraph.retired_models import check_route_targets
from plangraph.model_admission import validate_model  # compatibility re-export

from .benchmarks import (BENCHMARK_COLUMNS, BenchDataError, BenchmarkError,  # noqa: F401
                         BenchRequired, benchmark_for_leg, benchmark_rows_from_db)
from .legs import CODEX_FALLBACK_WRITERS, DEFAULT_LEGS, READINESS_LEGS, LEAD_WRITER, TIERS, WRITER_ORDER, Leg, leg_active, explicit_only_skips  # noqa: F401  (re-export for SpawnService.route)
from .provider_stop import next_leg_after_provider_stop
from .readiness import WINDOW_TOKENS, USABLE_FRACTION
from .review_order import review_key, catalog_tiers
from .seats import _check_diff_ref, family_spellings, claude_writer_fence, pick_seats, writer_parts, seat_family  # noqa: F401

__all__ = ["RouteError", "Route", "effective_estimate", "pick_window_class",
           "route_card", "next_after_failure", "WINDOW_TOKENS", "USABLE_FRACTION",
            "SpawnRefused", "SpawnPlan", "plan_spawn",
              "hand_picked", "next_leg_after_provider_stop", "benchmark_rows_from_db"]

USABLE_FRACTION_PCT = round(USABLE_FRACTION * 100)
#: Attempts on one tier before promotion, and promotions allowed.
ATTEMPT_BUDGET = 3
PROMOTIONS = 1
#: Risk tags that keep training-on-data legs away from the card.
_NO_TRAIN_TAGS = frozenset({"auth", "secrets", "money", "live-step"})
#: Exit classes where the provider, not the card, failed: rotate inside the tier.
_PROVIDER_EXITS = frozenset({"usage_limit", "outage"})
#: Exit classes that leave the routing loop (planner queue or inbox).
_PARK_EXITS = frozenset({"contract_defect", "needs_input", "flaky_check", "cancelled"})


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


def _window_share(window_class: str) -> int:
    return USABLE_FRACTION_PCT * WINDOW_TOKENS[window_class] // 100


