"""SQLite-backed decision & cross-family review engine.

Enforces:
  - 5-rung tier ladder (TIER_RUNG): subscription strictly prioritized over paid
  - Utility scoring: mimo/mimo-v2.6-pro scores above opencode-go/qwen3.8-max
  - Codex ACP luna-only: cxa/gpt-6-luna-high available, cxa/gpt-6-astra-max & sol rejected
  - Guardrails:
      1. clamp_effort: Sonnet 5.5 max effort -> xhigh
      2. Price cliff: Haiku excluded when need_tokens > 100000
  - Cross-family review enforcement on shared combo layers (resolve_review_plan):
      writer_family != rev1_family != rev2_family with matching X-OmniRoute-* headers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from plangraph.spawner.catalog_seed import TIER_RUNG
from plangraph.spawner.clients import build_routing_envelope_headers
from plangraph.spawner.risk import (
    RISK_BETA_EXCESS,
    RISK_GAMMA_COST,
    RISK_MIN_INTELLIGENCE,
    RISK_TARGET_INTELLIGENCE,
    evaluate_risk,
)

# Role multiplier for intelligence gain above I_min
ROLE_ALPHA: dict[str, float] = {
    "implement": 1.0,
    "worker": 1.0,
    "l2-worker": 1.0,
    "driver": 1.1,
    "l3-driver": 1.1,
    "orchestrate": 1.1,
    "l1-orchestrator": 1.1,
    "review": 1.2,
    "reviewer": 1.2,
    "l2-reviewer": 1.2,
}

# Role trajectory factor: buffers multi-turn conversation growth before price cliff
ROLE_TRAJECTORY_FACTORS: dict[str, float] = {
    "implement": 1.5,
    "worker": 1.5,
    "l2-worker": 1.5,
    "driver": 1.3,
    "l3-driver": 1.3,
    "orchestrate": 1.3,
    "l1-orchestrator": 1.3,
    "review": 1.1,
    "reviewer": 1.1,
    "l2-reviewer": 1.1,
}

# Try importing CatalogDB from catalog_db; fallback gracefully if unavailable.
try:
    from plangraph.spawner.catalog_db import CatalogDB  # type: ignore
except ImportError:
    CatalogDB = None  # type: ignore

DEFAULT_FALLBACK_MODELS: list[dict[str, Any]] = [
    {
        "id": "opencode-go/gemini-4-argon-contributor",
        "family": "gemini",
        "aa_intelligence": 53.0,
        "verified_tok_per_s": 115.0,
        "cost_per_task_usd": 0.50,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 1000000,
        "combos": ["l1-orchestrator", "l2-worker", "l2-reviewer", "l3-driver"],
    },
    {
        "id": "mimo/mimo-v2.6-pro",
        "family": "mimo",
        "aa_intelligence": 46.0,
        "verified_tok_per_s": 51.0,
        "cost_per_task_usd": 0.13,
        "pool_burn_weight": 0.5,
        "tier": "subscription",
        "available": 1,
        "context_window": 1000000,
        "combos": ["l1-orchestrator", "l2-worker", "l2-reviewer"],
    },
    {
        "id": "opencode-go/qwen3.8-max",
        "family": "qwen",
        "aa_intelligence": 45.4,
        "verified_tok_per_s": 85.0,
        "cost_per_task_usd": 0.45,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 1000000,
        "combos": ["l1-orchestrator", "l2-worker"],
    },
    {
        "id": "cxa/gpt-6-luna-high",
        "family": "gpt",
        "aa_intelligence": 38.4,
        "verified_tok_per_s": 180.0,
        "cost_per_task_usd": 0.07,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 872000,
        "combos": ["l1-orchestrator", "l2-worker", "l2-reviewer", "l3-driver"],
    },
    {
        "id": "cxa/gpt-6-astra-max",
        "family": "gpt",
        "aa_intelligence": 53.0,
        "verified_tok_per_s": 69.0,
        "cost_per_task_usd": 3.26,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 0,  # Rejected per Codex ACP luna-only
        "context_window": 400000,
        "combos": [],
    },
    {
        "id": "cxa/gpt-6-sol-max",
        "family": "gpt",
        "aa_intelligence": 52.0,
        "verified_tok_per_s": 60.0,
        "cost_per_task_usd": 0.72,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 0,  # Rejected per Codex ACP luna-only
        "context_window": 400000,
        "combos": [],
    },
    {
        "id": "agy/claude-sonnet-5-5-high",
        "family": "claude",
        "aa_intelligence": 46.8,
        "verified_tok_per_s": 100.0,
        "cost_per_task_usd": 1.12,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 1000000,
        "combos": ["l1-orchestrator", "l2-worker", "l2-reviewer"],
    },
    {
        "id": "claude/claude-haiku-4-5",
        "family": "claude",
        "aa_intelligence": 43.4,
        "verified_tok_per_s": 130.0,
        "cost_per_task_usd": 0.21,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 200000,
        "price_cliff_tokens": 100000,  # Price cliff at 100k
        "combos": ["l2-worker"],
    },
    {
        "id": "opencode-go/muse-spark-1.3-contributor",
        "family": "muse",
        "aa_intelligence": 45.0,
        "verified_tok_per_s": 287.0,
        "cost_per_task_usd": 1.37,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 200000,
        "combos": ["l2-worker"],
    },
    {
        "id": "opencode-go/deepseek-v4-flash-max",
        "family": "deepseek",
        "aa_intelligence": 45.0,
        "verified_tok_per_s": 231.0,
        "cost_per_task_usd": 0.26,
        "pool_burn_weight": 1.0,
        "tier": "subscription",
        "available": 1,
        "context_window": 128000,
        "combos": ["l2-worker", "l3-driver"],
    },
    {
        "id": "deepseek/deepseek-v4-flash-max",
        "family": "deepseek",
        "aa_intelligence": 45.0,
        "verified_tok_per_s": 231.0,
        "cost_per_task_usd": 0.26,
        "pool_burn_weight": 1.0,
        "tier": "paid",
        "available": 1,
        "context_window": 128000,
        "combos": ["l2-worker", "l3-driver"],
    },
    {
        "id": "agy/gemini-3.8-flash-tiered-high",
        "family": "gemini",
        "aa_intelligence": 41.0,
        "verified_tok_per_s": 158.0,
        "cost_per_task_usd": 1.24,
        "pool_burn_weight": 1.0,
        "tier": "free",
        "available": 1,
        "context_window": 1048576,
        "combos": ["l1-orchestrator", "l2-worker"],
    },
]

_DEFAULT_CATALOG_DB: Any = None


def _get_default_catalog_db() -> Any:
    global _DEFAULT_CATALOG_DB
    if _DEFAULT_CATALOG_DB is None and CatalogDB is not None:
        inst = CatalogDB(":memory:")
        inst.init_and_seed()
        _DEFAULT_CATALOG_DB = inst
    return _DEFAULT_CATALOG_DB


def detect_model_family(model_id: str) -> str:
    """Detect the canonical model family: claude, gemini, gpt, mimo, qwen, deepseek, muse, glm, kimi."""
    mid = (model_id or "").lower()
    if any(tok in mid for tok in ("claude", "sonnet", "haiku", "opus")):
        return "claude"
    if any(tok in mid for tok in ("gemini", "argon")):
        return "gemini"
    if any(tok in mid for tok in ("luna", "astra", "sol", "gpt", "openai", "codex")):
        return "gpt"
    if "mimo" in mid:
        return "mimo"
    if "qwen" in mid:
        return "qwen"
    if "deepseek" in mid:
        return "deepseek"
    if "muse" in mid:
        return "muse"
    if "glm" in mid:
        return "glm"
    if any(tok in mid for tok in ("kimi", "moonshot")):
        return "kimi"
    if "/" in mid:
        return mid.split("/", 1)[0]
    return mid


def is_model_available(model: Mapping[str, Any]) -> bool:
    """Check if model is eligible and available.

    Enforces Codex ACP luna-only: cxa/gpt-6-luna-high is eligible,
    while cxa/gpt-6-astra-max and cxa/gpt-6-sol-* are rejected.
    """
    if int(model.get("available", 1)) == 0:
        return False
    status = str(model.get("status", "active")).lower()
    if status not in ("active", ""):
        return False
    mid = str(model.get("id") or model.get("model_id") or "").lower()
    if mid.startswith("cxa/") and not mid.startswith("cxa/gpt-6-luna"):
        return False
    return True


def clamp_effort(model_id: str, requested_effort: str) -> str:
    """Clamp reasoning effort for models with vendor-specific constraints.

    Claude Sonnet 5.5 does not support effort 'max', clamping 'max' -> 'xhigh'.
    """
    mid = (model_id or "").lower()
    eff = (requested_effort or "medium").lower()
    if ("claude-sonnet-5-5" in mid or "sonnet-5-5" in mid) and eff == "max":
        return "xhigh"
    return requested_effort


def score_model(
    model: Mapping[str, Any],
    role: str = "implement",
    risk: str = "normal",
    need_tokens: int = 32000,
) -> float:
    """Compute primary utility score U_core(M) for a candidate model.

    Formula:
      U_intel(M) = alpha_role * (min(I(M) - I_min, I_target - I_min) + max(0.0, I(M) - I_target) * beta_excess)
      U_core(M) = U_intel(M) - Cost(M) * gamma_cost - Burn(M) * 1.0 + W_tier(M)

    Decoupled risk parameters:
      - low:      I_min = 37.5, I_target = 41.0, gamma_cost = 15.0, beta_excess = 0.2
      - normal:   I_min = 44.0, I_target = 47.0, gamma_cost = 10.0, beta_excess = 0.4
      - high:     I_min = 46.0, I_target = 50.0, gamma_cost = 4.0,  beta_excess = 2.0
      - critical: I_min = 50.0, I_target = 54.0, gamma_cost = 1.5,  beta_excess = 2.5

    Role multipliers (alpha_role):
      - implement / worker: 1.0
      - driver / orchestrator: 1.1
      - review / reviewer: 1.2

    Tier weights (W_tier):
      trial (0): +14.0, free (1): +10.5, credit (2): +7.0, subscription (3): +3.5, paid (4): 0.0, unknown: -35.0
    """
    intel = float(model.get("aa_intelligence") or 40.0)
    cost = float(model.get("cost_per_task_usd") if model.get("cost_per_task_usd") is not None else 0.20)
    burn = float(model.get("pool_burn_weight") or 1.0)
    tier = str(model.get("tier") or "paid").lower()

    # Fail-closed unknown tier -> 99
    tier_rung = TIER_RUNG.get(tier, 99)
    tier_bonus = (4 - tier_rung) * 3.5 if tier_rung <= 4 else -35.0

    i_min = RISK_MIN_INTELLIGENCE.get(risk, 44.0)
    i_target = RISK_TARGET_INTELLIGENCE.get(risk, 47.0)
    gamma_cost = RISK_GAMMA_COST.get(risk, 10.0)
    beta_excess = RISK_BETA_EXCESS.get(risk, 0.4)
    alpha_role = ROLE_ALPHA.get(role.lower(), 1.0)

    # Decoupled intelligence utility
    base_intel = min(intel - i_min, i_target - i_min)
    excess_intel = max(0.0, intel - i_target) * beta_excess
    u_intel = alpha_role * (base_intel + excess_intel)

    # Core utility without linear throughput bonus (throughput is reserved for tie-breaking)
    # and without double-counted cache discount
    u_core = u_intel - (cost * gamma_cost) - (burn * 1.0) + tier_bonus
    return round(u_core, 4)


def rank_candidates(
    candidates: Sequence[Mapping[str, Any]],
    role: str = "implement",
    risk: str = "normal",
    need_tokens: int = 32000,
) -> list[dict[str, Any]]:
    """Rank candidates using two-stage lexicographic optimization.

    Stage 1: Primary utility score U_core(M) based on intelligence, cost, burn, and tier.
    Stage 2: Lexicographic equivalence band (epsilon = 0.3 for high/critical, 0.5 for low/normal).
             Within each equivalence band, resolve ties using verified throughput (Speed(M)).
    """
    if not candidates:
        return []

    remaining: list[tuple[dict[str, Any], float, float]] = []
    for c in candidates:
        d = dict(c)
        score = score_model(d, role=role, risk=risk, need_tokens=need_tokens)
        speed = float(d.get("verified_tok_per_s") or d.get("tok_per_s") or 0.0)
        remaining.append((d, score, speed))

    epsilon = 0.3 if risk in ("high", "critical") else 0.5
    ranked: list[dict[str, Any]] = []

    while remaining:
        u_max = max(item[1] for item in remaining)
        in_band: list[tuple[dict[str, Any], float, float]] = []
        out_band: list[tuple[dict[str, Any], float, float]] = []
        for item in remaining:
            if (u_max - item[1]) <= epsilon:
                in_band.append(item)
            else:
                out_band.append(item)

        # Sort in_band by speed descending, then score descending as tertiary tie breaker
        in_band.sort(key=lambda x: (x[2], x[1]), reverse=True)
        for item in in_band:
            ranked.append(item[0])

        remaining = out_band

    return ranked


def select_top_candidate(
    candidates: Sequence[Mapping[str, Any]],
    role: str = "implement",
    risk: str = "normal",
    need_tokens: int = 32000,
) -> dict[str, Any] | None:
    """Select the #1 candidate using two-stage lexicographic optimization."""
    ranked = rank_candidates(candidates, role=role, risk=risk, need_tokens=need_tokens)
    return ranked[0] if ranked else None


def _normalize_catalog_row(row: Mapping[str, Any]) -> dict[str, Any]:
    d = dict(row)
    if "id" not in d and "model_id" in d:
        d["id"] = d["model_id"]
    if "context_window" not in d and "context_tokens" in d:
        d["context_window"] = d["context_tokens"]
    return d


def get_catalog_models(db: Any = None) -> list[dict[str, Any]]:
    """Retrieve models from SQLite CatalogDB (or fallback dictionary if unavailable)."""
    target_db = db if db is not None else _get_default_catalog_db()
    if target_db is not None:
        if hasattr(target_db, "list_models"):
            models = target_db.list_models(active_only=False)
            if models is not None:
                return [_normalize_catalog_row(m) for m in models]
        if hasattr(target_db, "get_models"):
            models = target_db.get_models()
            if models is not None:
                return [_normalize_catalog_row(m) for m in models]

    return [_normalize_catalog_row(m) for m in DEFAULT_FALLBACK_MODELS]


def _passes_context_and_cliff(m: Mapping[str, Any], need_tokens: int, role: str = "implement") -> bool:
    """Check if model satisfies context window and price cliff limits.

    Multiplies need_tokens by role trajectory factor (1.5x worker, 1.3x driver, 1.1x reviewer)
    before checking against price_cliff_tokens so multi-turn tool loops do not cross the price cliff mid-task.
    """
    ctx = int(m.get("context_window") or m.get("context_tokens") or 128000)
    if ctx < need_tokens:
        return False
    cliff = m.get("price_cliff_tokens")
    if cliff is not None:
        factor = ROLE_TRAJECTORY_FACTORS.get(role.lower(), 1.5)
        trajectory_tokens = int(need_tokens * factor)
        if trajectory_tokens > int(cliff):
            return False
    return True


def resolve_review_plan(
    writer_model: str,
    risk: str = "normal",
    combo_name: str = "l2-worker",
    need_tokens: int = 32000,
    db: Any = None,
) -> dict[str, Any]:
    """Resolve cross-family review plan on shared combo layers.

    Rules:
      - Detects writer_family
      - Reviewer count: 1 for low/normal risk, 2 for high/critical risk
      - Selects Reviewer 1 with family_1 != writer_family
      - For 2 reviewers: selects Reviewer 2 with family_2 not in (writer_family, family_1)
      - Emits exact X-OmniRoute-* envelope headers for each reviewer targeting the combo:
          Rev 1: X-OmniRoute-Task-Role: review, X-OmniRoute-Exclude-Family: <writer_family>
          Rev 2: X-OmniRoute-Task-Role: review, X-OmniRoute-Exclude-Family: <writer_family>,<family_1>
    """
    writer_family = detect_model_family(writer_model)
    reviewer_count = 2 if risk in ("high", "critical") else 1

    all_models = get_catalog_models(db)

    # Filter candidate models
    target_db = db if db is not None else _get_default_catalog_db()
    combo_priority_legs = False
    candidates: list[dict[str, Any]] = []

    if target_db is not None and hasattr(target_db, "get_route_legs") and combo_name:
        route_legs = target_db.get_route_legs(combo_name, active_only=True)
        if route_legs:
            candidates = [_normalize_catalog_row(r) for r in route_legs if _passes_context_and_cliff(r, need_tokens, role="review")]
            combo_priority_legs = True

    if not candidates:
        for m in all_models:
            if not is_model_available(m):
                continue
            combos = m.get("combos")
            if combos and combo_name not in combos:
                continue
            if not _passes_context_and_cliff(m, need_tokens, role="review"):
                continue
            candidates.append(m)

    min_intelligence = RISK_MIN_INTELLIGENCE.get(risk, 44.0)

    # Rank Reviewer 1 candidates (family != writer_family)
    rev1_candidates = [
        c for c in candidates
        if detect_model_family(c["id"]) != writer_family
    ]
    if not rev1_candidates:
        # Emergency relaxation outside combo, still enforcing availability, context, and price cliff
        rev1_candidates = [
            c for c in all_models
            if is_model_available(c)
            and _passes_context_and_cliff(c, need_tokens, role="review")
            and detect_model_family(c["id"]) != writer_family
        ]

    if combo_priority_legs:
        qual_rev1 = [c for c in rev1_candidates if float(c.get("aa_intelligence", 0)) >= min_intelligence]
        reviewer_1 = qual_rev1[0] if qual_rev1 else (rev1_candidates[0] if rev1_candidates else None)
    else:
        ranked_rev1 = rank_candidates(rev1_candidates, role="review", risk=risk, need_tokens=need_tokens)
        reviewer_1 = ranked_rev1[0] if ranked_rev1 else None

    if not reviewer_1:
        reviewer_1 = {
            "id": "mimo/mimo-v2.6-pro",
            "family": "mimo",
            "aa_intelligence": 46.0,
            "verified_tok_per_s": 51.0,
            "cost_per_task_usd": 0.13,
            "pool_burn_weight": 0.5,
            "tier": "subscription",
            "context_window": 1000000,
        }
    family_1 = detect_model_family(reviewer_1["id"])

    headers_1 = build_routing_envelope_headers(
        role="review",
        risk=risk,
        min_intelligence=min_intelligence,
        required_context=need_tokens,
        exclude_families=[writer_family],
        effort="medium",
        author_model=writer_model,
    )

    reviewers: list[dict[str, Any]] = [
        {
            "index": 1,
            "model": reviewer_1["id"],
            "family": family_1,
            "combo": combo_name,
            "headers": headers_1,
            "score": score_model(reviewer_1, role="review", risk=risk, need_tokens=need_tokens),
        }
    ]

    if reviewer_count == 2:
        rev2_candidates = [
            c for c in candidates
            if detect_model_family(c["id"]) not in (writer_family, family_1)
        ]
        if not rev2_candidates:
            rev2_candidates = [
                c for c in all_models
                if is_model_available(c)
                and _passes_context_and_cliff(c, need_tokens, role="review")
                and detect_model_family(c["id"]) not in (writer_family, family_1)
            ]

        if combo_priority_legs:
            qual_rev2 = [c for c in rev2_candidates if float(c.get("aa_intelligence", 0)) >= min_intelligence]
            reviewer_2 = qual_rev2[0] if qual_rev2 else (rev2_candidates[0] if rev2_candidates else None)
        else:
            ranked_rev2 = rank_candidates(rev2_candidates, role="review", risk=risk, need_tokens=need_tokens)
            reviewer_2 = ranked_rev2[0] if ranked_rev2 else None

        if not reviewer_2:
            reviewer_2 = {
                "id": "opencode-go/muse-spark-1.3-contributor",
                "family": "muse",
                "aa_intelligence": 45.0,
                "verified_tok_per_s": 287.0,
                "cost_per_task_usd": 1.37,
                "pool_burn_weight": 1.0,
                "tier": "subscription",
                "context_window": 200000,
            }
        family_2 = detect_model_family(reviewer_2["id"])

        headers_2 = build_routing_envelope_headers(
            role="review",
            risk=risk,
            min_intelligence=min_intelligence,
            required_context=need_tokens,
            exclude_families=[writer_family, family_1],
            effort="medium",
            author_model=writer_model,
        )

        reviewers.append({
            "index": 2,
            "model": reviewer_2["id"],
            "family": family_2,
            "combo": combo_name,
            "headers": headers_2,
            "score": score_model(reviewer_2, role="review", risk=risk, need_tokens=need_tokens),
        })

    return {
        "writer_model": writer_model,
        "writer_family": writer_family,
        "risk": risk,
        "combo": combo_name,
        "need_tokens": need_tokens,
        "reviewer_count": reviewer_count,
        "reviewers": reviewers,
    }


def resolve_model_for_task(
    role: str = "implement",
    risk: str = "normal",
    combo_name: str = "l2-worker",
    need_tokens: int = 32000,
    exclude_families: Sequence[str] = (),
    effort: str = "medium",
    author_model: str | None = None,
    db: Any = None,
) -> dict[str, Any]:
    """Resolve the optimal model candidate for a given task role and risk band.

    Enforces:
      - 5-rung tier ladder (subscription prioritized over paid)
      - Model utility scoring (mimo/mimo-v2.6-pro prioritized over qwen3.8-max)
      - Minimum intelligence threshold for the risk band
      - Availability and gated model exclusion
      - Family exclusion (including author_model family)
      - Context window and price cliff limits (with role trajectory factor buffer)
      - Two-stage lexicographic tie-breaker via rank_candidates
      - Vendor effort clamping (Sonnet 5.5 max -> xhigh)
    """
    excluded = {f.lower().strip() for f in exclude_families if f.strip()}
    if author_model:
        excluded.add(detect_model_family(author_model))

    min_intelligence = RISK_MIN_INTELLIGENCE.get(risk, 44.0)
    all_models = get_catalog_models(db)

    # 1. Filter candidates within combo
    candidates: list[dict[str, Any]] = []
    for m in all_models:
        if not is_model_available(m):
            continue
        combos = m.get("combos")
        if combo_name and combos and combo_name not in combos:
            continue
        if detect_model_family(m["id"]) in excluded:
            continue
        if not _passes_context_and_cliff(m, need_tokens, role=role):
            continue
        candidates.append(m)

    # Filter by min intelligence if possible, or relax if empty
    high_intel = [c for c in candidates if float(c.get("aa_intelligence", 0)) >= min_intelligence]
    if high_intel:
        candidates = high_intel

    if not candidates:
        # Relax combo constraint while keeping availability, excluded families, and context/cliff
        for m in all_models:
            if not is_model_available(m):
                continue
            if detect_model_family(m["id"]) in excluded:
                continue
            if not _passes_context_and_cliff(m, need_tokens, role=role):
                continue
            candidates.append(m)

    # Rank candidates using two-stage lexicographic optimization
    ranked_candidates = rank_candidates(candidates, role=role, risk=risk, need_tokens=need_tokens)

    selected = ranked_candidates[0] if ranked_candidates else {
        "id": "mimo/mimo-v2.6-pro",
        "family": "mimo",
        "aa_intelligence": 46.0,
        "verified_tok_per_s": 51.0,
        "cost_per_task_usd": 0.13,
        "pool_burn_weight": 0.5,
        "tier": "subscription",
        "context_window": 1000000,
    }

    selected_id = selected["id"]
    selected_family = detect_model_family(selected_id)
    clamped_effort = clamp_effort(selected_id, effort)

    headers = build_routing_envelope_headers(
        role=role,
        risk=risk,
        min_intelligence=min_intelligence,
        required_context=need_tokens,
        exclude_families=list(excluded),
        effort=clamped_effort,
        author_model=author_model,
    )

    return {
        "model": selected_id,
        "family": selected_family,
        "tier": str(selected.get("tier", "subscription")).lower(),
        "score": score_model(selected, role=role, risk=risk, need_tokens=need_tokens),
        "combo": combo_name,
        "effort": clamped_effort,
        "headers": headers,
        "candidate": selected,
    }

