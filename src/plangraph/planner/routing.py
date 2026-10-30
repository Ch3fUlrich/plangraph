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

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import asdict
import json
import os
import re
import sqlite3
from pathlib import Path
import time

from plangraph.keeper.schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES
from plangraph.routing_errors import RouteError, SpawnRefused

from .legs import DEFAULT_LEGS, TIERS, WRITER_ORDER, Leg

__all__ = ["RouteError", "Route", "effective_estimate", "pick_window_class",
           "route_card", "next_after_failure", "WINDOW_TOKENS", "USABLE_FRACTION",
            "SpawnRefused", "SpawnPlan", "plan_spawn",
             "hand_picked", "next_leg_after_provider_stop", "benchmark_rows_from_db"]

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
               overflow: bool = False, role: str | None = None) -> Route:
    """Route ``card`` (keeper card dict) over ``legs``; see module docstring.

    ``role`` falls back to ``card["role"]``; implement sorts by WRITER_ORDER.
    ``overflow`` marks a context-overflow failure: the window class grows by one.
    Raises ``RouteError`` when no leg is eligible, ``ValueError`` on a bad card.
    """
    effective_role = role if role is not None else card.get("role")
    exclude_fams = set(exclude_families)
    legs = tuple(legs)
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
        elif leg.family in exclude_fams:
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
                         + "; ".join(reasons), exclusion_reasons=dropped)
    if effective_role == "implement":
        def sort_key(t):
            tier_idx, pos, leg = t
            try:
                w_idx = WRITER_ORDER.index(leg.id)
            except ValueError:
                w_idx = len(WRITER_ORDER)
            return (w_idx, tier_idx, pos)
        eligible.sort(key=sort_key)
    else:
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


class BenchmarkError(SpawnRefused):
    """Benchmark policy or storage failure."""


class BenchRequired(BenchmarkError):
    """Strict benchmark policy refused one or more candidate legs."""

    def __init__(self, message, legs=()):
        super().__init__(message)
        self.gaps = [leg for leg in legs if isinstance(leg, dict)]
        self.legs = [leg['leg'] if isinstance(leg, dict) else leg for leg in legs]


class BenchDataError(BenchmarkError):
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
    candidates: tuple[dict, ...] = ()
    writer_groups: tuple = ()
    seat_coverage: tuple[dict, ...] = ()

    @property
    def first_leg(self) -> Leg | None:
        return self.legs[0] if self.legs else None

    def to_dict(self):
        result = asdict(self)
        if not self.writer_groups:
            result.pop('writer_groups')
        if not self.seat_coverage:
            result.pop('seat_coverage')
        result["first_leg"] = asdict(self.first_leg) if self.first_leg else None
        return result


#: A git revision the inline-diff path accepts: no option-looking or spaced refs.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}$")


def _check_diff_ref(ref):
    """``card["diff_ref"]`` = ``{"repo": <absolute path>, "base": <rev>, "head": <rev>}``."""
    if (not isinstance(ref, Mapping) or set(ref) != {"repo", "base", "head"}
            or not all(isinstance(ref[k], str) for k in ref)
            or not os.path.isabs(ref["repo"])
            or not _REF_RE.match(ref["base"]) or not _REF_RE.match(ref["head"])):
        raise SpawnRefused('diff_ref must be {"repo": absolute path, "base": rev, "head": rev}')


def _seat_family(family):
    # The keeper counts every Claude seat as one family named anthropic.
    return "anthropic" if family == "claude" else family


def pick_seats(ordered, writer_families, *, high):
    """Seats covering every writer part (DECISIONS 3.2, keeper ``coverage.uncovered``).

    A part (writer family) is covered by ``need`` distinct seat families other
    than its own (1 standard, 2 high); a high part also needs one non-Claude
    family other than its own.  A seat may share a family with ANOTHER writer
    (D-3084).  Seats are taken in route order, one per family, only while they
    help cover an open part; at least two families are always picked.  Returns
    ``(seats, coverage, open_parts)``.
    """
    from plangraph.keeper.coverage import uncovered
    writers = sorted({_seat_family(w) for w in writer_families if w})
    need = 2 if high else 1
    seats, families = [], []
    for leg in ordered:
        fam = _seat_family(leg.family)
        if fam in families or (writers and all(fam == w for w in writers)):
            continue
        open_parts = uncovered(writers, families, need=need, sensitive=high)
        if len(families) >= 2 and not any(fam != w for w in open_parts):
            continue
        seats.append(leg)
        families.append(fam)
        if len(families) >= 2 and not uncovered(writers, families, need=need, sensitive=high):
            break
    coverage = tuple({"leg": leg.id, "covers": [w for w in writers if w != _seat_family(leg.family)]}
                     for leg in seats)
    return seats, coverage, uncovered(writers, families, need=need, sensitive=high)


def plan_spawn(card, *, role, writer_families=(), exclude_families=(), leg_reader=None, now=None,
                legs=DEFAULT_LEGS, benchmark=None, strict=False, policy=None) -> SpawnPlan:
    """Plan by canonical rules; benchmark utility can only permute one tier.

    The implement role ignores benchmark permutation (operator order D-2683).
    ``exclude_families`` excludes the specified family names from candidate legs.
    ``policy`` (default :func:`load_route_policy`) skips banned/off families and
    paused harnesses.  A review leg whose family is EVERY writer family is
    excluded; seats cover each writer part (:func:`pick_seats`).  An inline-only
    review leg needs ``card["diff_ref"]``: the spawner inlines that diff.
    Readers may be a LegStateStore or a callable with the leg_status contract.
    Account/combo scope comes from the card and is never a wildcard.
    """
    if not isinstance(card, Mapping) or not card or "estimate_tokens" not in card:
        raise SpawnRefused("card required")
    for field in ('paths', 'write_set', 'risk_tags'):
        if field in card and (not isinstance(card[field], (list, tuple)) or
                              not all(isinstance(v, str) for v in card[field])):
            raise SpawnRefused(f'{field} must be a list of strings')
    for field in ('has_tests', 'is_pure_doc', 'is_docs_only'):
        if field in card and type(card[field]) is not bool:
            raise SpawnRefused(f'{field} must be boolean')
    for field in ('estimate_tokens', 'reviewer_count', 'fanout'):
        if field in card and (not _is_int(card[field]) or card[field] < (1 if field == 'estimate_tokens' else 0)):
            raise SpawnRefused(f'{field} must be a valid integer')
    for field, allowed in (('bucket', ('S0', 'S1', 'S2', 'S3', 'S4')), ('window_class', WINDOW_CLASSES)):
        if field in card and card[field] not in allowed:
            raise SpawnRefused(f'{field} has invalid value')
    if any(t not in RISK_TAGS for t in card.get('risk_tags', ())):
        raise SpawnRefused('risk_tags has invalid value')
    if 'diff_ref' in card:
        _check_diff_ref(card['diff_ref'])
    if policy is None:
        from .route_policy import load_route_policy
        try:
            policy = load_route_policy()
        except ValueError as exc:
            raise SpawnRefused(str(exc)) from None
    if role not in ("implement", "review", "orchestrate"):
        raise ValueError("role must be implement|review|orchestrate")
    from plangraph.routing_support.risk import evaluate_risk
    assessment = evaluate_risk(paths=card.get('paths', card.get('write_set', ())),
                               bucket=card.get('bucket', 'S1'),
                               has_tests=card.get('has_tests', True),
                               risk_tags=card.get('risk_tags', ()),
                               is_pure_doc=card.get('is_pure_doc', False))
    bands = ('low', 'normal', 'high', 'critical')
    requested_risk = card.get('risk', 'normal')
    if requested_risk not in bands:
        raise SpawnRefused('risk has invalid value')
    ranking_risk = max((assessment.risk, requested_risk), key=bands.index)
    if (assessment.risk in ('high', 'critical') or card.get('risk') in ('high', 'critical')) and card.get('reviewer_count', 2) < 2:
        raise SpawnRefused('high/critical changes require two reviewers')
    if card.get('reviewer_count') == 0 and (
            not assessment.review_exempt or card.get('risk', 'normal') in ('high', 'critical')
            ):
        raise SpawnRefused('zero reviewers requires low/normal review_exempt')
    from plangraph.routing_support.leg_state import leg_status
    reader = getattr(leg_reader, "leg_status", leg_reader) or leg_status
    from plangraph.routing_support.resolver import OPERATOR_REMOVED_MODELS
    legs = tuple(l for l in legs if l.model not in OPERATOR_REMOVED_MODELS)
    for candidate in legs:
        validate_model(candidate.model)
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
    writer_set = {w for w in writer_families if w}
    # D-3084 per-part seats: only a family that is EVERY writer covers no part.
    excluded = (writer_set if role == "review" and len(writer_set) == 1 else set()) | set(exclude_families)
    if excluded & {"claude", "anthropic"}:
        excluded |= {"claude", "anthropic"}   # one family, two spellings
    def _leg_status(leg_obj):
        st = reader(leg_obj.model, now=now, account=card.get("account"), combo=card.get("combo"))
        if leg_obj.harness == "agy" and st["state"] != "limited" and "/" not in leg_obj.model:
            alt = reader("agy/" + leg_obj.model, now=now, account=card.get("account"), combo=card.get("combo"))
            if alt["state"] == "limited":
                return alt
        return st

    states = {l.id: _leg_status(l) for l in candidates}
    limited = {key for key, state in states.items() if state["state"] == "limited"}
    skipped = []
    from plangraph.routing_support.resolver import score_model
    candidate_details = []
    # Route each leg through the same canonical eligibility rules to retain
    # individual exclusion reasons even when there is no eligible route.
    for leg in candidates:
        reason = policy.skip_reason(leg, role)
        if reason:
            pass
        elif leg.id in limited:
            reason = "limited"
        elif leg.family in excluded:
            reason = "excluded_family"
        elif role == "review" and leg.tool_mode == "inline-only" and 'diff_ref' not in card:
            reason = "inline_only_needs_diff"
        else:
            try:
                route_card(card, legs=(leg,), role=role, exclude_families=excluded)
            except RouteError as exc:
                reason = next(iter(exc.exclusion_reasons), 'routing_error')
                states[leg.id] = dict(states[leg.id], evidence={'state': states[leg.id]['evidence'], 'error': str(exc)})
        if reason:
            skipped.append({"leg": leg.id, "reason": reason,
                            "evidence": states[leg.id]["evidence"]})
        row = rows.get(leg.model, rows.get(leg.id))
        detail = {"leg": leg.id, "tier": leg.tier,
                  "u_core": score_model(dict(row, tier=leg.tier), role=role, risk=ranking_risk,
                                        need_tokens=card["estimate_tokens"]) if row is not None else None,
                  "p_source": "measured" if row is not None else "prior"}
        if reason:
            detail.update(reason=reason, evidence=states[leg.id]["evidence"])
        candidate_details.append(detail)
    policy_skipped = {s["leg"] for s in skipped if s["reason"] in (
        "banned_family", "off_family", "paused_harness", "inline_only_needs_diff")}
    try:
        route = route_card(card, legs=candidates, unavailable=limited | policy_skipped,
                           exclude_families=excluded, role=role)
    except RouteError as exc:
        return SpawnPlan((), tuple(skipped), "operator-order", (),
                         tuple([str(exc), *bench_reasons]), str(exc), gaps, tuple(candidate_details))
    ordered = list(route.legs)
    reasons = [*route.reasons, *bench_reasons]
    source = "operator-order"
    if benchmark is not None and role != "implement":
        from plangraph.routing_support.resolver import rank_candidates
        for tier in TIERS:
            tier_positions = [i for i, l in enumerate(ordered) if l.tier == tier]
            positions = [i for i in tier_positions
                         if ordered[i].model in rows or ordered[i].id in rows]
            missing = [ordered[i] for i in tier_positions if i not in positions]
            measured = [dict(rows.get(ordered[i].model, rows.get(ordered[i].id)),
                             model_id=ordered[i].id, tier=tier, available=1, active=True) for i in positions]
            ranked = rank_candidates(measured, role=role, risk=ranking_risk, need_tokens=card["estimate_tokens"])
            by_id = {ordered[i].id: ordered[i] for i in positions}
            for i, leg in zip(tier_positions, [by_id[row["model_id"]] for row in ranked] + missing):
                ordered[i] = leg
            if positions:
                source = "benchmark"
    seats, coverage, open_parts = [], (), []
    if role == "review":
        high = ranking_risk in ("high", "critical") or bool(card.get('risk_tags'))
        seats, coverage, open_parts = pick_seats(ordered, writer_set, high=high)
    error = None
    if role == "review" and len(seats) < 2:
        error = "two distinct review families unavailable"
    elif open_parts:
        error = "seat coverage unmet for writer parts: " + ", ".join(open_parts)
    if error:
        reasons.append(error)
    order = {leg.id: i for i, leg in enumerate(ordered)}
    candidate_details.sort(key=lambda c: order.get(c['leg'], len(order)))
    from plangraph.routing_support.fixer import split_findings
    from plangraph.settings import admit_fixer_threshold
    # plan_spawn receives no store-derived keeper item, so the trusted layers
    # are global (R2); the card's project/phase layers may only tighten (R3).
    threshold = admit_fixer_threshold(card=card)
    groups = split_findings(card.get('findings', ()), limit=threshold) if role == 'implement' else ()
    return SpawnPlan(tuple(ordered), tuple(skipped), source, tuple(seats), tuple(reasons), error, gaps,
                     tuple(candidate_details), groups, coverage)


def validate_model(model):
    """The resolver catalog and registered legs are the exact admission door."""
    from plangraph.routing_support.resolver import (get_catalog_models, catalog_row_admitted,
                                           OPERATOR_REMOVED_MODELS)
    from plangraph.mimo_policy import check_mimo_path
    from .legs import DEFAULT_LEGS
    if not isinstance(model, str) or not model:
        raise SpawnRefused('model must be a nonempty string')
    check_mimo_path(model)
    rows = get_catalog_models()
    inactive = {row['id'] for row in rows if not catalog_row_admitted(row)}
    allowed = ({row['id'] for row in rows if catalog_row_admitted(row)} |
               {leg.model for leg in DEFAULT_LEGS}) - inactive - OPERATOR_REMOVED_MODELS
    if model not in allowed:
        raise SpawnRefused('unknown model: ' + str(model))
    return model


def hand_picked(model, card=None, override_reason=None, *, log_path=None, caller=None):
    """Explicit model selection always needs a durable override audit."""
    if not isinstance(override_reason, str) or len(override_reason.strip()) < 10:
        raise SpawnRefused("spawn by card; --override-reason required")
    validate_model(model)
    path = Path(log_path) if log_path is not None else Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "plangraph/spawn-overrides.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    from plangraph.routing_support.private_io import append_private
    append_private(path, json.dumps({"ts": time.time(), "model": model,
                    "reason": override_reason, "caller": caller or "planner"}) + "\n")
    return model


def next_leg_after_provider_stop(plan, leg_id, status, body, headers, caller, *,
                                 store=None, now=None, account=None, combo=None):
    """Consume an actual provider stop; unrelated failures never suppress a leg."""
    from plangraph.routing_support import leg_state
    state = store or leg_state
    by_id = {leg.id: leg for leg in plan.legs}
    if not isinstance(leg_id, str):
        raise SpawnRefused('leg id must be a string')
    if leg_id not in by_id:
        raise ValueError("leg not in plan")
    if type(status) is not int or status not in (429, 402) or not isinstance(body, str) or not body.strip():
        return by_id[leg_id]
    state.record_provider_response(by_id[leg_id].model, status, body, headers, caller,
                                   now=now, account=account, combo=combo)
    start = list(by_id).index(leg_id) + 1
    for leg in plan.legs[start:]:
        st = state.leg_status(leg.model, now=now, account=account, combo=combo)
        if leg.harness == "agy" and st["state"] != "limited" and "/" not in leg.model:
            alt = state.leg_status("agy/" + leg.model, now=now, account=account, combo=combo)
            if alt["state"] == "limited":
                continue
        if st["state"] != "limited":
            return leg
    return None
