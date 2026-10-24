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
from dataclasses import asdict
import json
import os
import sqlite3
from pathlib import Path
import time

from plangraph.keeper.schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES

from .legs import DEFAULT_LEGS, TIERS, Leg

__all__ = ["RouteError", "Route", "effective_estimate", "pick_window_class",
           "route_card", "next_after_failure", "WINDOW_TOKENS", "USABLE_FRACTION",
            "SPAWNER_SELECTION_ENABLED", "SpawnRefused", "SpawnPlan", "plan_spawn",
             "hand_picked", "next_leg_after_provider_stop", "benchmark_rows_from_db"]

#: Window sizes in tokens.
WINDOW_TOKENS = {"128k": 131072, "256k": 262144, "1m": 1048576}
#: Share of a window a card may use (design number).
USABLE_FRACTION_PCT = 55
USABLE_FRACTION = USABLE_FRACTION_PCT / 100
#: Attempts on one tier before promotion, and promotions allowed.
ATTEMPT_BUDGET = 3
PROMOTIONS = 1
#: Off until SPAWNER-UTILITY-PROPS closes; the only runtime path for spawner selection is this module.
SPAWNER_SELECTION_ENABLED = False
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
        spent = {*tried, last.id}  # tried=[] => the failed leg is route.first_leg
        for leg in route.legs:
            if leg.tier == last.tier and leg.id not in spent:
                return ("rotate", leg)
        return ("wait", None)
    if tier_attempts < route.attempt_budget:
        return ("retry", last)
    rank = TIERS.index(last.tier)
    for leg in route.legs:
        if TIERS.index(leg.tier) > rank:
            return ("promote", leg)
    return ("park", None)


class SpawnRefused(RouteError):
    """A spawn lacks its mandatory card or explicit audited override."""


class BenchRequired(SpawnRefused):
    """Strict benchmark policy refused one or more candidate legs."""

    def __init__(self, message, legs=()):
        super().__init__(message)
        self.gaps = [leg for leg in legs if isinstance(leg, dict)]
        self.legs = [leg['leg'] if isinstance(leg, dict) else leg for leg in legs]


class BenchDataError(BenchRequired):
    """Benchmark storage could not be read; not missing model coverage."""


# Table-specific aliases to the resolver's canonical columns. No fuzzy model aliases.
BENCHMARK_COLUMNS = {
    "catalog_benchmarks": {"model_id": "model_id"},
    "model_benchmarks": {"model_id": "model_id", "intelligence": "aa_intelligence",
                         "tok_per_s": "verified_tok_per_s"},
}


def benchmark_rows_from_db(path):
    """Return (model-id rows, errors) without creating or modifying a database.

    Prefer the catalog table when both supported tables exist. Duplicate model rows
    are counted in ``benchmark_row_count``; the first row supplies ranking metrics.
    """
    try:
        uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = next((t for t in BENCHMARK_COLUMNS if t in tables), None)
            if table is None:
                return {}, [f"{path}: missing benchmark table (catalog_benchmarks or model_benchmarks)"]
            result = {}
            for raw in conn.execute(f'SELECT * FROM "{table}"'):
                row = dict(raw)
                for original, canonical in BENCHMARK_COLUMNS[table].items():
                    if original in row and canonical not in row:
                        row[canonical] = row[original]
                model = row.get("model_id")
                if not isinstance(model, str) or not model:
                    return {}, [f"{path}: {table} row missing model_id"]
                if model in result:
                    result[model]["benchmark_row_count"] += 1
                else:
                    result[model] = dict(row, benchmark_row_count=1)
            return result, []
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {}, [f"{path}: cannot read benchmark database: {exc}"]


@dataclass(frozen=True)
class SpawnPlan:
    legs: tuple[Leg, ...]
    skipped: tuple[dict, ...]
    p_source: str
    review_seats: tuple[Leg, ...]
    reasons: tuple[str, ...]
    error: str | None = None
    gaps: tuple[dict, ...] = ()

    @property
    def first_leg(self) -> Leg | None:
        return self.legs[0] if self.legs else None

    def to_dict(self):
        result = asdict(self)
        result["first_leg"] = asdict(self.first_leg) if self.first_leg else None
        return result


def plan_spawn(card, *, role, writer_families=(), leg_reader=None, now=None,
                legs=DEFAULT_LEGS, benchmark=None, strict=False) -> SpawnPlan:
    """Plan by canonical rules; benchmark utility can only permute one tier.

    Readers may be a LegStateStore or a callable with the leg_status contract.
    Account/combo scope comes from the card and is never a wildcard.
    """
    if not isinstance(card, Mapping) or not card or "estimate_tokens" not in card:
        raise SpawnRefused("card required")
    if role not in ("implement", "review"):
        raise ValueError("role must be implement|review")
    from plangraph.spawner.risk import evaluate_risk
    assessment = evaluate_risk(paths=card.get('paths', card.get('write_set', ())),
                               bucket=card.get('bucket', 'S1'),
                               has_tests=card.get('has_tests', True),
                               risk_tags=card.get('risk_tags', ()),
                               is_pure_doc=card.get('is_pure_doc', False))
    if (assessment.risk in ('high', 'critical') or card.get('risk') in ('high', 'critical')) and card.get('reviewer_count', 2) < 2:
        raise SpawnRefused('high/critical changes require two reviewers')
    if card.get('reviewer_count') == 0 and (
            not assessment.review_exempt or card.get('risk', 'normal') in ('high', 'critical')
            ):
        raise SpawnRefused('zero reviewers requires low/normal review_exempt')
    from plangraph.spawner.leg_state import leg_status
    reader = getattr(leg_reader, "leg_status", leg_reader) or leg_status
    candidates = tuple(l for l in legs if role in l.roles)
    rows = (benchmark if isinstance(benchmark, Mapping) else {
        row["model_id"]: row for row in benchmark}) if benchmark is not None else {}
    gaps = tuple({"leg": l.id, "model": l.model, "reason": "no benchmark row"}
                 for l in candidates if l.model not in rows and l.id not in rows)
    if strict and gaps:
        raise BenchRequired("benchmark rows missing: " + ", ".join(g["leg"] for g in gaps), gaps)
    bench_reasons = [f"{g['leg']} ({g['model']}): no benchmark row" for g in gaps]
    if benchmark is None:
        bench_reasons.extend(["benchmark data absent", "no benchmark p"])
    excluded = set(writer_families) if role == "review" else set()
    states = {l.id: reader(l.model, now=now, account=card.get("account"),
                            combo=card.get("combo")) for l in candidates}
    limited = {key for key, state in states.items() if state["state"] == "limited"}
    skipped = []
    # Route each leg through the same canonical eligibility rules to retain
    # individual exclusion reasons even when there is no eligible route.
    for leg in candidates:
        reason = None
        if leg.id in limited:
            reason = "limited"
        elif leg.family in excluded:
            reason = "excluded_family"
        else:
            try:
                route_card(card, legs=(leg,))
            except RouteError as exc:
                reason = str(exc).split("excluded 1 legs: ")[-1]
        if reason:
            skipped.append({"leg": leg.id, "reason": reason,
                            "evidence": states[leg.id]["evidence"]})
    try:
        route = route_card(card, legs=candidates, unavailable=limited,
                           exclude_families=excluded)
    except RouteError as exc:
        return SpawnPlan((), tuple(skipped), "operator-order", (),
                         tuple([str(exc), *bench_reasons]), str(exc), gaps)
    ordered = list(route.legs)
    reasons = [*route.reasons, *bench_reasons]
    source = "operator-order"
    if benchmark is not None:
        from plangraph.spawner.resolver import rank_candidates
        for tier in TIERS:
            tier_positions = [i for i, l in enumerate(ordered) if l.tier == tier]
            positions = [i for i in tier_positions
                         if ordered[i].model in rows or ordered[i].id in rows]
            missing = [ordered[i] for i in tier_positions if i not in positions]
            measured = [dict(rows.get(ordered[i].model, rows.get(ordered[i].id)),
                             model_id=ordered[i].id, tier=tier) for i in positions]
            ranked = rank_candidates(measured, role=role, need_tokens=card["estimate_tokens"])
            by_id = {ordered[i].id: ordered[i] for i in positions}
            for i, leg in zip(tier_positions, [by_id[row["model_id"]] for row in ranked] + missing):
                ordered[i] = leg
            if positions:
                source = "benchmark"
    seats = []
    families = set(excluded)
    if role == "review":
        for leg in ordered:
            if leg.family not in families:
                seats.append(leg)
                families.add(leg.family)
            if len(seats) == 2:
                break
    error = "two distinct review families unavailable" if role == "review" and len(seats) < 2 else None
    if error:
        reasons.append(error)
    return SpawnPlan(tuple(ordered), tuple(skipped), source, tuple(seats), tuple(reasons), error, gaps)


def hand_picked(model, card=None, override_reason=None, *, log_path=None, caller=None):
    """Explicit model selection always needs a durable override audit."""
    if not isinstance(override_reason, str) or len(override_reason.strip()) < 10:
        raise SpawnRefused("spawn by card; --override-reason required")
    from plangraph.mimo_policy import check_mimo_path
    check_mimo_path(model)
    path = Path(log_path) if log_path is not None else Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "plangraph/spawn-overrides.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    from plangraph.spawner.private_io import append_private
    append_private(path, json.dumps({"ts": time.time(), "model": model,
                    "reason": override_reason, "caller": caller or "planner"}) + "\n")
    return model


def next_leg_after_provider_stop(plan, leg_id, status, body, headers, caller, *,
                                 store=None, now=None, account=None, combo=None):
    """Consume an actual provider stop; unrelated failures never suppress a leg."""
    from plangraph.spawner import leg_state
    state = store or leg_state
    by_id = {leg.id: leg for leg in plan.legs}
    if leg_id not in by_id:
        raise ValueError("leg not in plan")
    if type(status) is not int or status not in (429, 402) or not isinstance(body, str) or not body.strip():
        return by_id[leg_id]
    state.record_provider_response(by_id[leg_id].model, status, body, headers, caller,
                                   now=now, account=account, combo=combo)
    start = list(by_id).index(leg_id) + 1
    for leg in plan.legs[start:]:
        if state.leg_status(leg.model, now=now, account=account, combo=combo)["state"] != "limited":
            return leg
    return None
