"""Keeper routing and spawner boundary (design 4.3, 5; D25)."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, asdict, field
from plangraph.routing_support.liveness import read_leg_model, unavailable
from plangraph.routing_support.risk import evaluate_risk, measure_card_shape
import json
import os
from pathlib import Path
import time

from plangraph.keeper.schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES
from plangraph.routing_errors import RouteError, SpawnRefused
from plangraph.retired_models import check_route_targets
from plangraph.model_admission import validate_model  # compatibility re-export

from .benchmarks import (BENCHMARK_COLUMNS, BenchDataError, BenchmarkError,  # noqa: F401
                         BenchRequired, benchmark_for_leg, benchmark_rows_from_db,
                         current_facts, current_index_version, rank_ineligible)
from .legs import CODEX_FALLBACK_WRITERS, SONNET_WRITERS, SONNET_WRITER_HARD, DEFAULT_LEGS, READINESS_LEGS, LEAD_WRITER, TIERS, WRITER_ORDER, Leg, leg_active, explicit_only_skips  # noqa: F401  (re-export for SpawnService.route)
from .legs import CARD_NEEDS, LEG_ROLES, TOOL_MODES, research_route_pool, unmet_need
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


def check_card_needs(card: Mapping) -> None:
    """``needs`` lists what the card requires of a leg; ``tool_mode`` the weakest tool mode it accepts."""
    needs = card.get("needs", ())
    if not isinstance(needs, (list, tuple)) or any(n not in CARD_NEEDS for n in needs):
        raise ValueError(f"needs must be a list drawn from {CARD_NEEDS}")
    if card.get("tool_mode") is not None and card["tool_mode"] not in TOOL_MODES:
        raise ValueError(f"tool_mode must be one of {TOOL_MODES}")


def _window_share(window_class: str) -> int:
    return USABLE_FRACTION_PCT * WINDOW_TOKENS[window_class] // 100


def effort_profiles() -> dict:
    return json.loads((Path(__file__).resolve().parents[1] /
                       "config" / "effort_profiles.json").read_text(encoding="utf-8"))


def _effort_measure(card):
    paths = card.get("paths", card.get("write_set", ()))
    has_tests = card.get("has_tests")
    if has_tests is None and (card.get("check_cmd") or card.get("smoke_checks")):
        has_tests = True
    return measure_card_shape(paths, fanout=card.get("fanout"), has_tests=has_tests)


def task_class(card, *, measurement=None) -> str:
    measured = measurement if measurement is not None else _effort_measure(card)
    size = ("small", "small", "medium", "large", "large")[int(measured.bucket[1])]
    kind = card.get("check_kind", "change")
    if kind not in ("change", "regression"):
        raise ValueError("check_kind must be change or regression")
    return f"{size}_{kind}"


def rule_level(card) -> dict:
    """Compute the routing profile from card facts without changing routing order."""
    measured = _effort_measure(card)
    table = effort_profiles()
    bands = tuple(table["risk_floors"])
    assessed = evaluate_risk(paths=measured.files, bucket=measured.bucket,
                             has_tests=measured.has_tests, fanout=measured.fanout,
                             is_docs_only=measured.is_docs_only,
                             is_pure_doc=card.get("is_pure_doc", False),
                             risk_tags=card.get("risk_tags", ()))
    declared = card.get("risk", assessed.risk)
    if declared not in bands:
        raise ValueError("invalid effort risk band")
    risk = max(assessed.risk, declared, key=bands.index)
    level, reason = int(measured.bucket[1]), "size_bucket"
    if risk == "critical":
        level, reason = 4, "critical_risk"
    elif risk == "high" and level < 3:
        level, reason = 3, "high_risk"
    elif not measured.has_tests and level < 3:
        level, reason = 3, "no_tests"
    estimate = card.get("estimate_tokens") or 0
    window = card.get("window_class") or WINDOW_CLASSES[-1]
    over_window = not _fits(estimate, window)
    if over_window:
        level, reason = 4, "estimate_exceeds_window"
    assigned = card.get("level")
    if assigned is not None and assigned not in table["levels"]:
        raise ValueError("level must be L0 through L4")
    lowered = assigned if assigned is not None and int(assigned[1]) < level else None
    if assigned is not None and int(assigned[1]) > level:
        level, reason = int(assigned[1]), "planner_raise"
    label = f"L{level}"
    profile = table["levels"][label]
    return dict(level=label, reason=reason, lowered=lowered,
                floor=max(profile["floor"], table["risk_floors"][risk]),
                bucket=measured.bucket, risk_band=risk, seats=profile["seats"],
                cascade=profile["cascade"], decompose=measured.should_decompose or over_window,
                task_class=task_class(card, measurement=measured),
                check_kind=card.get("check_kind", "change"), version=table["version"])


def log_level_lowering(effort, item, ts):
    if effort.get("lowered") is not None:
        import logging
        logging.getLogger(__name__).info("effort_level_lowered %s", json.dumps(
            dict(ts=ts, item=item or "adhoc", assigned=effort["lowered"], reason=effort["reason"])))


def _fits(effective: int, window_class: str) -> bool:
    return effective <= _window_share(window_class)


def pick_window_class(effective: int, floor: str | None = None, *, full_window: bool = False) -> str:
    """Pick the smallest class above the floor, using its 55% share by default.

    Raises ``RouteError("estimate too big")`` when even the 1m class is too small.
    """
    if floor is not None and floor not in WINDOW_CLASSES:
        raise ValueError(f"floor must be one of {WINDOW_CLASSES}")
    start = WINDOW_CLASSES.index(floor) if floor is not None else 0
    limit = WINDOW_TOKENS.__getitem__ if full_window else _window_share
    for window_class in WINDOW_CLASSES[start:]:
        if effective <= limit(window_class):
            return window_class
    largest = WINDOW_CLASSES[-1]
    if full_window:
        raise RouteError(f"estimate too big: {largest} is {limit(largest)} tokens")
    raise RouteError(f"estimate too big: {USABLE_FRACTION_PCT}% of {largest} is {_window_share(largest)} tokens")


def _is_low_risk(risk: str, tags: Sequence[str] | None) -> bool:
    """The one low-risk predicate behind the low_risk_only leg gate."""
    return risk == "low" and not tags


def _review(tags: Sequence[str]) -> tuple[str, int]:
    return ("risky", 2) if tags else ("standard", 1)


def route_card(card: Mapping, *, legs: Sequence[Leg] = DEFAULT_LEGS, subtasks: int = 1,
               unavailable: Collection[str] = (), exclude_families: Collection[str] = (),
               overflow: bool = False, role: str | None = None, now=None) -> Route:
    """Route ``card`` (keeper card dict) over ``legs``; see module docstring.
    ``role`` falls back to ``card["role"]``; absent roles retain keeper admission.
    Spawn roles use the leg's context; the keeper pack still budgets by class.
    ``overflow`` marks a context-overflow failure: the window class grows by one.
    Raises ``RouteError`` when no leg is eligible, ``ValueError`` on a bad card.
    """
    from .route_policy import implement_family_excluded
    from .legs import LOW_RISK_ONLY_LEGS
    check_route_targets(card, legs)
    effective_role = role if role is not None else card.get("role")
    research = effective_role == "research"
    exclude_fams = set(exclude_families)
    legs = tuple(READINESS_LEGS if effective_role is None and legs is DEFAULT_LEGS else legs)
    if research:
        legs = research_route_pool(legs)
    effective = effective_estimate(card.get("estimate_tokens"), subtasks)
    tags = list(card.get("risk_tags") or [])
    low_risk = _is_low_risk(card.get("risk", "normal"), tags)
    bad = [t for t in tags if t not in RISK_TAGS]
    if bad:
        raise ValueError(f"unknown risk tags: {bad}")
    check_card_needs(card)
    per_leg = effective_role in LEG_ROLES and not overflow
    largest = WINDOW_CLASSES[-1]
    full_window = research and per_leg
    class_limit = WINDOW_TOKENS.__getitem__ if full_window else _window_share
    # Large spawn estimates reach per-leg admission even above the class share.
    class_estimate = min(effective, class_limit(largest)) if per_leg else effective
    window_class = card.get("window_class")
    if window_class is None:
        window_class = pick_window_class(class_estimate, full_window=full_window)
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
    if per_leg and class_estimate > class_limit(window_class):
        window_class = pick_window_class(class_estimate, floor=window_class, full_window=full_window)
        reasons.append(f'request limit: window class upgraded to {window_class}')
    window_fits = _fits(effective, window_class)
    serving_windows = WINDOW_CLASSES[WINDOW_CLASSES.index(window_class):] if research else (largest,)
    bound_notes = []
    dropped: dict[str, int] = {}
    eligible: list[tuple[int, int, Leg]] = []
    for pos, leg in enumerate(legs):
        declared_bound = (per_leg and leg.max_request_tokens is not None
                          and (research or window_class == largest)
                          and any(w in leg.windows for w in serving_windows))
        if not leg_active(leg, now=now):
            why = "disabled"
        elif effective_role == "implement" and implement_family_excluded(leg):
            why = "excluded_family"
        elif effective_role in ("implement", "review") and leg.id in LOW_RISK_ONLY_LEGS and not low_risk:
            why = "low_risk_only"
        elif leg.id in unavailable:
            why = "unavailable"
        elif leg.family in exclude_fams:
            why = "excluded_family"
        elif (need := unmet_need(leg, card)) is not None:
            why = "needs_" + need
        elif no_train and leg.trains_on_data:
            why = "trains_on_data"
        elif research and card.get("web_required") and not leg.has_web_tools:
            why = "web_access_unavailable"
        elif declared_bound and effective > leg.max_request_tokens:
            why = "max_request_tokens"
            bound_notes.append(f"{leg.id}: max_request_tokens {leg.max_request_tokens}")
        elif per_leg and not declared_bound and leg.max_request_tokens is not None and (
                research or effective * 100 > USABLE_FRACTION_PCT * leg.max_request_tokens):
            why = "window_class"
        elif (not per_leg or leg.max_request_tokens is None) and window_class not in leg.windows:
            why = "window_class"
        elif not declared_bound and not window_fits:
            why = "over_window_share"
            bound_notes.append(f"{leg.id}: {USABLE_FRACTION_PCT}% of {window_class} is {_window_share(window_class)} tokens")
        elif leg.max_request_tokens is not None and effective > leg.max_request_tokens:
            why = "max_request_tokens"
        else:
            eligible.append((TIERS.index(leg.tier), pos, leg))
            continue
        dropped[why] = dropped.get(why, 0) + 1
    for why, count in dropped.items():
        reasons.append(f"excluded {count} legs: {why}")
    reasons.extend(bound_notes)
    if not eligible:
        raise RouteError(f"no eligible leg for {window_class} (effective {effective}): "
                         + "; ".join(reasons), exclusion_reasons=dropped)
    if effective_role == "implement":
        from .route_policy import implement_key
        eligible.sort(key=lambda t: implement_key(t[2], t[0], t[1]))
    elif effective_role == "review":
        eligible.sort(key=lambda t: review_key(t[2], t[0], t[1]))
    else:
        eligible.sort(key=lambda t: (research and t[2].claude_family, t[0], t[1]))
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


#: Route reason (and run-record ``route_reason``) of a Sonnet writer served while the Codex lead is limited.
CODEX_LIMITED = "codex_limited"


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
    placement: dict = field(default_factory=lambda: dict(host=None, reason='placement_not_evaluated', free_mb=None))

    agy_quota_states: dict = field(default_factory=dict)
    effort: dict = field(default_factory=dict)
    static_v0: str | None = None

    @property
    def first_leg(self) -> Leg | None:
        return self.legs[0] if self.legs else None

    def to_dict(self):
        result = asdict(self)
        for field in ('writer_groups', 'seat_coverage'):
            if not getattr(self, field): result.pop(field)
        result["first_leg"] = asdict(self.first_leg) if self.first_leg else None
        return result


def plan_spawn(card, *, role, writer_families=(), exclude_families=(), leg_reader=None, now=None,
                legs=DEFAULT_LEGS, benchmark=None, strict=False, policy=None, placement=None,
                catalog_rows=None, installed=None, busy_legs=()) -> SpawnPlan:
    """Plan by canonical rules; benchmark utility can only permute one tier.
    Measured inputs rank within cost tiers; implement writer priority is final.
    ``exclude_families`` excludes the specified family names from candidate legs.
    ``policy`` (default :func:`load_route_policy`) skips banned/off families and
    paused harnesses.  A review leg whose family is ANY writer family is
    excluded; seats cover each writer part (:func:`pick_seats`).  An inline-only
    review leg needs ``card["diff_ref"]``: the spawner inlines that diff.
    ``busy_legs`` names review legs whose parallel review slots are all taken (rule 3.66 f10 clause 6).
    ``catalog_rows`` injects tier/admission data; None reads the live catalog once.
    Readers may be a LegStateStore or a callable with the leg_status contract.
    Account/combo scope comes from the card and is never a wildcard.
    ``installed`` (client name -> bool) puts each leg into its own harness before policy, seats and
    ordering run (rule 3.53); None skips host resolution.
    """
    native_review = role == "review" and legs is DEFAULT_LEGS
    native_implement = role == "implement" and legs is DEFAULT_LEGS
    from .legs import (REVIEW_ORDER, REVIEW_REQUIRED, LOW_RISK_ONLY_LEGS, review_route_pool, REVIEW_HYDROGEN,
                       REVIEW_ARGON_LOW, REVIEW_ARGON, REVIEW_SONNET, REVIEW_GPT, REVIEW_GEMINI_SMART, REVIEW_SMART)
    if native_review:
        legs = review_route_pool(legs)
    if role == "research":
        legs = research_route_pool(legs)
    harness_skipped = []
    if installed is not None:
        from .legs import resolve_legs
        legs, skips = resolve_legs(legs, installed)
        harness_skipped = list(skips)
    check_route_targets(card, legs)
    placement = dict(placement) if placement is not None else dict(host=None, reason='placement_not_evaluated', free_mb=None)
    if not isinstance(card, Mapping) or not card or "estimate_tokens" not in card:
        raise SpawnRefused("card required" + (": missing estimate_tokens" if isinstance(card, Mapping) and card else ""))
    for field in ('paths', 'write_set', 'risk_tags'):
        if field in card and (not isinstance(card[field], (list, tuple)) or
                              not all(isinstance(v, str) for v in card[field])):
            raise SpawnRefused(f'{field} must be a list of strings')
    for field in ('has_tests', 'is_pure_doc', 'is_docs_only', 'repository_review_required', 'hard', 'web_required'):
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
    try:
        check_card_needs(card)
    except ValueError as exc:
        raise SpawnRefused(str(exc)) from None
    if 'diff_ref' in card:
        _check_diff_ref(card['diff_ref'])
    if policy is None:
        from .route_policy import load_route_policy
        try:
            policy = load_route_policy()
        except ValueError as exc:
            raise SpawnRefused(str(exc)) from None
    if role not in LEG_ROLES:
        raise ValueError(f"role must be {'|'.join(LEG_ROLES)}")
    score_role = "orchestrate" if role == "research" else role
    from plangraph.routing_support.risk import evaluate_risk, propose_risk_tags
    paths = card.get('paths', card.get('write_set', ()))
    risk_tags = card.get('risk_tags', ())
    sensitive_review = native_review and bool({"auth", "secrets"}.intersection(
        (*risk_tags, *propose_risk_tags((*paths, *(card.get('diff_ref') or {}).get('paths', ()))))))
    assessment = evaluate_risk(paths=paths,
                               bucket=card.get('bucket', 'S1'),
                               has_tests=card.get('has_tests', True),
                               risk_tags=card.get('risk_tags', ()),
                               is_pure_doc=card.get('is_pure_doc', False))
    bands = ('low', 'normal', 'high', 'critical')
    requested_risk = card.get('risk', 'normal')
    if requested_risk not in bands:
        raise SpawnRefused('risk has invalid value')
    ranking_risk = max((assessment.risk, requested_risk), key=bands.index)
    try:
        effort = rule_level(card)
    except (ValueError, TypeError) as exc:
        raise SpawnRefused(str(exc)) from exc
    log_level_lowering(effort, card.get("item_key") or card.get("item"), time.time())
    if (assessment.risk in ('high', 'critical') or card.get('risk') in ('high', 'critical')) and card.get('reviewer_count', 2) < 2:
        raise SpawnRefused('high/critical changes require two reviewers')
    if card.get('reviewer_count') == 0 and (
            not assessment.review_exempt or card.get('risk', 'normal') in ('high', 'critical')
            ):
        raise SpawnRefused('zero reviewers requires low/normal review_exempt')
    from plangraph.routing_support.leg_state import leg_status, last_probe_attempt_ts
    reader = getattr(leg_reader, "leg_status", leg_reader) or leg_status
    probe_time = (getattr(leg_reader, "last_probe_attempt_ts", None)
                  if leg_reader is not None else last_probe_attempt_ts)
    from plangraph.routing_support.resolver import OPERATOR_REMOVED_MODELS, get_catalog_models
    catalog_rows = tuple(get_catalog_models() if catalog_rows is None else catalog_rows)
    legs = catalog_tiers(tuple(l for l in legs if l.model not in OPERATOR_REMOVED_MODELS), catalog_rows)
    for candidate in legs:
        validate_model(candidate.model, allow_inactive=not leg_active(candidate, now=now), catalog_rows=catalog_rows)
    candidates = tuple(l for l in legs if role in l.roles and leg_active(l, now=now))
    rows = (benchmark if isinstance(benchmark, Mapping) else {
        row["model_id"]: row for row in benchmark}) if benchmark is not None else {}
    missing = tuple(leg for leg in candidates if benchmark_for_leg(rows, leg) is None)
    gaps = tuple({"leg": leg.id, "model": leg.model,
                  "reason": "explicitly unmeasured" if leg.benchmark_optional else "no benchmark row"}
                 for leg in missing)
    if strict:
        required_gaps = tuple(gap for leg, gap in zip(missing, gaps) if not leg.benchmark_optional)
        if required_gaps:
            raise BenchRequired("benchmark rows missing: " + ", ".join(g["leg"] for g in required_gaps),
                                required_gaps)
    if native_implement:   # Claude writers join the route behind Codex (rule 3.58); subject to the dual quota guard.
        from .legs import EXPLICIT_LEGS   # Added after the strict gap check: explicit legs carry no benchmark row.
        fallbacks = tuple(l for l in EXPLICIT_LEGS if l.id in (*SONNET_WRITERS, *CODEX_FALLBACK_WRITERS)
                          and (l.id != SONNET_WRITER_HARD or card.get('hard'))   # xhigh only for a hard card
                          and role in l.roles and leg_active(l, now=now))
        if installed is not None:   # the Claude fallback writers need their client too
            from .legs import resolve_legs
            fallbacks, skips = resolve_legs(fallbacks, installed)
            harness_skipped.extend(skips)
        candidates += fallbacks
    bench_reasons = [f"{g['leg']} ({g['model']}): {g['reason']}" for g in gaps]
    if benchmark is None:
        bench_reasons.extend(["benchmark data absent", "no benchmark p"])
    # Review writers are keeper families (fail closed on an unknown name).
    writer_set = writer_parts(writer_families) if role == "review" else {w for w in writer_families if w}
    from .route_policy import train_delta_document
    delta_document = train_delta_document(card, role)
    if role == "review" and not writer_set and not delta_document:
        raise SpawnRefused("review needs a writer family (writer_families or review_of)")
    # D-2511: exclude every writer family from all review seats.
    excluded = family_spellings((writer_set if role == "review" else set())
                                | set(exclude_families) | claude_writer_fence(role, writer_set), legs)
    from plangraph.routing_support.leg_state import leg_key, quota_key, routing_status
    def read_status(model, **scope):
        return routing_status(reader(model, now=now, account=card.get("account"),
                                     combo=card.get("combo"), **scope))

    def _leg_status(leg_obj):
        # Sonnet's bare model is also the native Claude subscription identity.
        if leg_obj.harness == "agy" and leg_obj.model == REVIEW_SONNET:
            return read_status("agy/" + leg_obj.model)
        st = read_leg_model(read_status, leg_obj, leg_obj.model)
        if leg_obj.harness == "agy" and "/" not in leg_obj.model:
            alt = read_status(leg_key(leg_obj))
            if unavailable(alt) and not unavailable(st):
                return alt
        return st

    states = {l.id: _leg_status(l) for l in candidates}
    busy = set(busy_legs) if native_review else set()
    quota_states = {"agy:" + model: (states[leg_id] if leg_id in states and leg_id != REVIEW_SONNET else
        read_status("agy:" + model))
        for model, leg_id in (("gemini-4-hydrogen", REVIEW_HYDROGEN),
                             ("gemini-4-argon", REVIEW_ARGON_LOW),
                             ("claude-sonnet-5-5", REVIEW_SONNET))}
    agy_models = {leg.id: quota_key(leg_key(leg)) for leg in candidates if leg.harness == "agy"}
    probe_times = ({model: stamp for model in set(quota_states) | set(agy_models.values())
                    if (stamp := probe_time(model, now=now)) is not None}
                   if probe_time is not None else {})
    quota_states = {model: dict(status, last_probe_at=probe_times[model])
                    if model in probe_times else status for model, status in quota_states.items()}
    blocked = {key for key, state in states.items() if unavailable(state)
               or (state["state"] == "unknown" and state.get("reason"))}
    def quota_limited(state):
        return state["state"] == "limited" and (state.get("evidence") or {}).get("kind") != "auth_refused"

    claude_quota_fallback = False
    writer_model = next(leg.model for leg in DEFAULT_LEGS if leg.id == LEAD_WRITER)
    # D-6652 correction: the Sonnet writers serve only while the Codex lead is limited (429 or quota).
    codex_limited = native_implement and any(
        quota_limited(read_status(key, harness="codex")) for key in (writer_model, LEAD_WRITER))
    if native_implement and quota_limited(quota_states["agy:gemini-4-hydrogen"]):
        claude_quota_fallback = any(
            quota_limited(read_status(key, harness="codex"))
            for key in (writer_model, LEAD_WRITER, REVIEW_GPT))
    # Rule 3.66 (f10): the Claude seat serves once no Gemini smart leg (hydrogen, argon) is left in the route.
    required_seats_out = native_review and not any(
        leg.id in REVIEW_GEMINI_SMART and leg.id not in blocked and leg.family not in excluded
        and not policy.skip_reason(leg, role) for leg in candidates)

    disabled_skips = [{"leg": l.id, "reason": "disabled: " + l.disabled,
                       "evidence": {"kind": "leg_disabled", "reason": l.disabled}}
               for l in legs if role in l.roles and not leg_active(l, now=now)]
    skipped = list(harness_skipped)
    from plangraph.routing_support.resolver import score_model
    from .route_policy import has_repository_review_tools
    from plangraph.routing_support.liveness import agy_limit_reason, codex_limit_reason
    from plangraph.routing_support.quota_snapshots import skip_reason as quota_skip_reason
    candidate_details = []
    low_risk = _is_low_risk(ranking_risk, card.get('risk_tags'))
    index_version = current_index_version(rows)
    rank_evidence = {}
    for leg in candidates:
        row = benchmark_for_leg(rows, leg)
        status = states[leg.id]
        state_reason = status["state"] if leg.id in blocked else None
        if state_reason in ("down", "unknown"):
            state_reason = status.get("reason") or state_reason
        if leg.id in blocked and (status.get("evidence") or {}).get("kind") == "auth_refused":
            state_reason = "auth_refused"
        reason = policy.skip_reason(leg, role) or state_reason or ("review_slots_busy" if leg.id in busy else None)
        if (not reason and (native_implement or native_review) and leg.claude_family
                and not (native_implement and leg.id in SONNET_WRITERS and codex_limited)
                and not (native_implement and leg.id not in SONNET_WRITERS and claude_quota_fallback)
                and not (native_review and leg.id == REVIEW_SONNET and (sensitive_review or required_seats_out))):
            reason = ("claude_requires_codex_limited" if native_implement and leg.id in SONNET_WRITERS
                      else "claude_requires_gpt_and_hydrogen_limits")
        if reason == 'limited' and leg.harness == 'agy':
            reason = agy_limit_reason(leg_key(leg), states[leg.id]) or reason
        if reason == 'limited':
            reason = quota_skip_reason(states[leg.id]) or reason
        if reason == 'limited' and leg.harness == 'codex':
            reason = codex_limit_reason(leg, states[leg.id]) or reason
        if reason:
            pass
        elif leg.family in excluded:
            reason = "excluded_family"
        elif strict and leg.benchmark_optional and row is None:
            reason = "benchmark_unmeasured"
        elif leg.id in LOW_RISK_ONLY_LEGS and not low_risk:
            reason = "low_risk_only"
        elif role == "review" and card.get("repository_review_required") and not has_repository_review_tools(leg):
            reason = "repository_review_tools_unavailable"
        elif role == "review" and leg.tool_mode == "inline-only" and 'diff_ref' not in card and not delta_document:
            reason = "inline_only_needs_diff"
        elif native_review and leg.id not in REVIEW_ORDER and seat_family(leg.family) != "openai" and not (
                low_risk and leg.id in LOW_RISK_ONLY_LEGS):
            reason = "review_route_excluded"
        elif row is not None and (ineligible := rank_ineligible(
                leg, row, risk=ranking_risk, effort=card.get('effort'), now=now,
                current=current_facts(leg, catalog_rows, index_version))):
            reason, rank_evidence[leg.id] = ineligible
        else:
            try:
                route_card(card, legs=(leg,), role=role, exclude_families=excluded, now=now)
            except RouteError as exc:
                reason = next(iter(exc.exclusion_reasons), 'routing_error')
                states[leg.id] = dict(states[leg.id], evidence={'state': states[leg.id]['evidence'], 'error': str(exc)})
        if reason:
            skipped.append({"leg": leg.id, "reason": reason,
                            "evidence": rank_evidence.get(leg.id, states[leg.id]["evidence"])})
        detail = {"leg": leg.id, "tier": leg.tier,
                  "u_core": score_model(dict(row, tier=leg.tier), role=score_role, risk=ranking_risk,
                                        need_tokens=card["estimate_tokens"]) if row is not None else None,
                  "p_source": "measured" if row is not None else "prior"}
        if agy_models.get(leg.id) in probe_times:
            detail["last_probe_at"] = probe_times[agy_models[leg.id]]
        if row is not None:
            detail.update(intelligence=row.get("aa_intelligence"), cost_per_task=row.get("cost_per_task_usd"))
        if reason:
            detail.update(reason=reason, evidence=rank_evidence.get(leg.id, states[leg.id]["evidence"]))
        candidate_details.append(detail)
    skipped.extend(disabled_skips)
    policy_skipped = {s["leg"] for s in skipped if s["reason"] in (
        "banned_family", "off_family", "paused_harness", "inline_only_needs_diff", "no_verdict_seat",
        "review_route_excluded", "review_slots_busy", "low_risk_only", "claude_requires_gpt_and_hydrogen_limits",
        "claude_requires_codex_limited",
        "repository_review_tools_unavailable", "benchmark_unmeasured", "opencode_tools_denied",
        "effort_channel", "stale_row", "canary_pending")}
    try:
        route = route_card(card, legs=candidates, unavailable=blocked | policy_skipped,
                           exclude_families=excluded, role=role, now=now)
    except RouteError as exc:
        return SpawnPlan((), tuple(skipped), "operator-order", (),
                         tuple([str(exc), *bench_reasons]), str(exc), gaps, tuple(candidate_details), placement=placement, agy_quota_states=quota_states)
    ordered = list(route.legs)
    reasons, source = [*route.reasons, *bench_reasons], "operator-order"
    if native_implement and any(leg.id in SONNET_WRITERS for leg in ordered):
        reasons.insert(0, CODEX_LIMITED)   # D-6652: the Sonnet writers are routed only while Codex is limited
    preferred = SONNET_WRITERS[0] if role == 'implement' else REVIEW_ORDER[0] if role == 'review' else None
    if any(s['leg'] == preferred and s['reason'] == 'window_class' for s in skipped):
        reasons.insert(0, f'{preferred}: window_class (estimate exceeds {USABLE_FRACTION_PCT}% of leg context)')
    if benchmark is not None:
        from plangraph.routing_support.resolver import rank_candidates
        for tier in TIERS:
            tier_positions = [i for i, l in enumerate(ordered) if l.tier == tier]
            positions = [i for i in tier_positions if benchmark_for_leg(rows, ordered[i]) is not None]
            missing = [ordered[i] for i in tier_positions if i not in positions]
            measured = [dict(benchmark_for_leg(rows, ordered[i]),
                             model_id=ordered[i].id, tier=tier, available=1, active=True) for i in positions]
            ranked = rank_candidates(measured, role=score_role, risk=ranking_risk, need_tokens=card["estimate_tokens"])
            by_id = {ordered[i].id: ordered[i] for i in positions}
            for i, leg in zip(tier_positions, [by_id[row["model_id"]] for row in ranked] + missing):
                ordered[i] = leg
            if positions:
                source = "benchmark"
    if role == "implement":
        from .route_policy import implement_key
        order = {leg.id: i for i, leg in enumerate(ordered)}
        ordered.sort(key=lambda leg: implement_key(leg, TIERS.index(leg.tier), order[leg.id]))
        if card.get('hard'):   # D-6652: xhigh for a hard card, high behind it
            ordered.sort(key=lambda leg: leg.id != SONNET_WRITER_HARD)
    if role == "review":
        # Benchmarks rank families, not interchangeable seats within a family.
        # Restore table order only in each family's measured slots, retaining
        # cross-family ranking and keeping unmeasured legs behind measured ones.
        input_order = {leg.id: i for i, leg in enumerate(legs)}
        for family in {leg.family for leg in ordered}:
            for tier in TIERS:
                positions = [i for i, leg in enumerate(ordered)
                             if leg.family == family and leg.tier == tier
                             and (leg.model in rows or leg.id in rows)]
                siblings = sorted((ordered[i] for i in positions),
                                  key=lambda leg: input_order[leg.id])
                for i, leg in zip(positions, siblings):
                    ordered[i] = leg
        # Only measured legs bypass role preferences, never the cost-tier key.
        table_order = {leg.id: i for i, leg in enumerate(ordered)}
        ordered.sort(key=lambda leg: review_key(leg, TIERS.index(leg.tier), table_order[leg.id],
                     measured=rows.get(leg.model, rows.get(leg.id)) is not None))
    if native_review:
        # Rule 3.66 (f10): REVIEW_ORDER leads (smart legs, then the weak pair), the non-order extras follow.
        ordered.sort(key=lambda leg: REVIEW_ORDER.index(leg.id) if leg.id in REVIEW_ORDER
                     else len(REVIEW_ORDER) + (leg.id in LOW_RISK_ONLY_LEGS))
        if sensitive_review:
            ordered.sort(key=lambda leg: leg.id != REVIEW_SONNET)
        source = "operator-order"
    ordered.sort(key=lambda leg: role in ("orchestrate", "research") and leg.claude_family)  # D-4812/D-297: Claude last
    if role in ('implement', 'review'):
        from plangraph.planner.route_policy import codex_leg_effort
        ordered = [codex_leg_effort(leg, card) for leg in ordered]
    seats, coverage, open_parts = [], (), []
    if role == "review":
        high = ranking_risk in ("high", "critical") or bool(card.get('risk_tags'))
        seat_order = {leg.id: i for i, leg in enumerate(route.legs)}
        seats, coverage, open_parts = pick_seats(
            sorted(ordered, key=lambda leg: seat_order.get(leg.id, len(seat_order))), writer_set, high=high)
    smart_seat = False
    if native_review:
        # Rule 3.66 (f10) clause 6 (D-6787): the head's smart seat is its first family-eligible smart leg
        # (Sonnet first on a credential diff). Limited, unserved or with every parallel slot busy, the
        # weak pair seats the head at once; no head waits for a smart slot.
        primary = next((leg.id for id_ in ((REVIEW_SONNET, *REVIEW_SMART) if sensitive_review else REVIEW_SMART)
                        for leg in candidates if leg.id == id_ and leg.family not in excluded
                        and not policy.skip_reason(leg, role)), None)
        smart = next((leg for leg in ordered if leg.id == primary), None)
        if smart is None:
            ordered = [*(leg for leg in ordered if leg.id in REVIEW_REQUIRED),
                       *(leg for leg in ordered if leg.id not in REVIEW_REQUIRED)]
        if smart is not None:
            # Rule 3.66 (f10): one counted seat on the first smart leg; the weak pair seats only when none serves.
            smart_seat = True
            writers = sorted({seat_family(w) for w in writer_set if w} - {seat_family(smart.family)})
            seats, coverage, open_parts = [smart], ({"leg": smart.id, "covers": writers},), []
        else:
            seats, coverage, open_parts = pick_seats(ordered, writer_set, high=high)
        # D-6652: both required OpenCode seats stay in the seat set when no smart leg serves, and beside the
        # Sonnet seat of a credential diff (the keeper close needs two non-Anthropic families there).
        missing = [] if smart_seat and not sensitive_review else [
            leg for leg in ordered if leg.id in REVIEW_REQUIRED and leg not in seats
            and seat_family(leg.family) not in {seat_family(s.family) for s in seats}]
        if missing:
            seats = [*seats, *missing]
            coverage = (*coverage, *({"leg": leg.id, "covers": [w for w in sorted({seat_family(w) for w in writer_set if w})
                                                                if w != seat_family(leg.family)]} for leg in missing))
    error = None
    if role == "review" and len(seats) < (1 if smart_seat else 2):   # rule 3.66 (f10): one smart seat, else two weak seats
        error = "two distinct review families unavailable"
    elif open_parts:
        error = "seat coverage unmet for writer parts: " + ", ".join(open_parts)
    if error:
        reasons.append(error)
    order = {leg.id: i for i, leg in enumerate(ordered)}
    candidate_details.sort(key=lambda c: order.get(c['leg'], len(order)))
    from plangraph.routing_support.fixer import split_findings
    from plangraph.settings import admit_fixer_threshold
    # No store-derived item: global R2 applies; card project/phase may only tighten (R3).
    threshold = admit_fixer_threshold(card=card)
    groups = split_findings(card.get('findings', ()), limit=threshold) if role == 'implement' else ()
    return SpawnPlan(tuple(ordered), tuple(skipped), source, tuple(seats), tuple(reasons), error, gaps,
                     tuple(candidate_details), groups, coverage, placement, quota_states, effort, route.first_leg.id)


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
