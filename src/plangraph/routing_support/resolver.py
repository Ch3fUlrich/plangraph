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

import math
import os
import shutil
import subprocess
import asyncio
import hashlib
import json
import time
import threading
import weakref
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from plangraph.routing_support.credit_gate import catalog_credit_skip_reason
from plangraph.routing_support.leg_state import leg_status
from plangraph.routing_support.catalog_seed import TIER_RUNG
from plangraph.mimo_policy import ACCEPTED_MIMO_IDS, mimo_path_allowed
from plangraph.routing_support.clients import build_routing_envelope_headers
from plangraph.routing_support.risk import (RISK_BETA_EXCESS, RISK_GAMMA_COST,
    RISK_MIN_INTELLIGENCE, RISK_TARGET_INTELLIGENCE, evaluate_risk)

# Role multiplier for intelligence gain above I_min
ROLE_ALPHA: dict[str, float] = {
    "implement": 1.0,
    "worker": 1.0,
    "l2-worker": 1.0,
    "drive": 1.1,
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
    "drive": 1.3,
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
    from plangraph.routing_support.catalog_db import CatalogDB  # type: ignore
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

# Registered seats are the authoritative offline routing surface. Legacy rows
# remain visible for diagnostics but must not become new offline spawn routes.
from plangraph.planner.legs import DEFAULT_LEGS

# OVH removed by operator, muse-spark-1.3-free gone D-1714, D-2655 G3.
OPERATOR_REMOVED_MODELS = frozenset({
    'omniroute/ovh-gpt-oss-120b',
    'omniroute/or-north-mini-code-free',
    'omniroute/or-laguna-s-2.1-free',
    'omniroute/or-nemotron-3-super-free',
    'opencode/muse-spark-1.3-contributor-free',
})


def admission_models() -> set[str]:
    """Registered offline routes, excluding models removed by the operator."""
    return {leg.model for leg in DEFAULT_LEGS} - OPERATOR_REMOVED_MODELS


_fallback_legs = {leg.model: leg for leg in DEFAULT_LEGS if leg.model in admission_models()}
from plangraph.registry_columns import INLINE_ONLY_MODELS as _INLINE_ONLY
for _row in DEFAULT_FALLBACK_MODELS:
    _row['active'] = _row['id'] in _fallback_legs
    _row.setdefault('tool_mode', 'inline-only' if _row['id'] in _INLINE_ONLY else 'tools')
    if not _row['active']:
        _row['available'] = 0
for _leg in DEFAULT_LEGS:
    _model = _leg.model
    if not any(row['id'] == _model for row in DEFAULT_FALLBACK_MODELS):
        DEFAULT_FALLBACK_MODELS.append(dict(
            id=_model, family=_leg.family, harness=_leg.harness,
            tier=_leg.tier, available=int(_model in _fallback_legs),
            active=_model in _fallback_legs,
            context_window=_leg.max_request_tokens, tool_mode=_leg.tool_mode,
            aa_intelligence=0.0, combos=[]))

_DEFAULT_CATALOG_DB: Any = None
_MISSING = object()
_VALID_RISKS = frozenset({"low", "normal", "high", "critical"})

def _get_default_catalog_db() -> Any:
    global _DEFAULT_CATALOG_DB
    if _DEFAULT_CATALOG_DB is None and CatalogDB is not None:
        inst = CatalogDB(":memory:")
        inst.init_and_seed()
        _DEFAULT_CATALOG_DB = inst
    return _DEFAULT_CATALOG_DB

def _validate_risk(risk: str) -> str:
    risk_l = str(risk).strip().lower() if risk is not None else ""
    if risk_l not in _VALID_RISKS or risk_l not in RISK_MIN_INTELLIGENCE:
        raise ValueError(f"unknown risk band: {risk!r}")
    return risk_l

def _validate_role(role: str) -> str:
    role_l = str(role).strip().lower() if role is not None else ""
    if role_l not in ROLE_ALPHA or role_l not in ROLE_TRAJECTORY_FACTORS:
        raise ValueError(f"unknown role: {role!r}")
    return role_l

def _validate_need_tokens(need_tokens: Any) -> int:
    if isinstance(need_tokens, bool) or not isinstance(need_tokens, (int, float)):
        raise ValueError(f"need_tokens must be a positive finite number: {need_tokens!r}")
    if isinstance(need_tokens, float):
        if not math.isfinite(need_tokens) or not need_tokens.is_integer():
            raise ValueError(f"need_tokens must be a positive finite number: {need_tokens!r}")
    tokens = int(need_tokens)
    if tokens <= 0:
        raise ValueError(f"need_tokens must be a positive finite number: {need_tokens!r}")
    return tokens

def _validated_metric(
    model: Mapping[str, Any],
    field_name: str,
    *,
    default: Any = _MISSING,
    none_as_default: bool = False,
) -> float:
    if field_name not in model:
        if default is _MISSING:
            raise ValueError(f"missing required numeric field: {field_name}")
        return float(default)

    value = model[field_name]
    if value is None:
        if none_as_default:
            if default is _MISSING:
                return 0.0
            return float(default)
        raise ValueError(f"missing numeric field: {field_name}")

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field {field_name}: {value!r}") from exc

    if not math.isfinite(number):
        raise ValueError(f"non-finite numeric field {field_name}: {value!r}")
    if number < 0.0:
        raise ValueError(f"negative numeric field {field_name}: {value!r}")
    return number

def _speed_for_model(model: Mapping[str, Any]) -> float:
    for field_name in ("verified_tok_per_s", "tok_per_s"):
        if field_name in model:
            value = model[field_name]
            if value is not None:
                number = _validated_metric(model, field_name)
                if number != 0.0:
                    return number
    return 0.0

def _raw_speed(model: Mapping[str, Any]) -> float:
    for field_name in ("verified_tok_per_s", "tok_per_s"):
        if field_name in model:
            value = model[field_name]
            if value is not None:
                number = float(value)
                if number != 0.0:
                    return number
    return 0.0

#: Canonical families detect_model_family can prove from a model id. Anything
#: else resolves to a label (the provider prefix, or the string itself, as
#: with the combo alias "l2-worker") and must never be used as a family.
MODEL_FAMILIES = frozenset({
    "claude", "gemini", "gpt", "mimo", "qwen", "deepseek", "muse", "glm", "kimi"})


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

def catalog_row_admitted(row: Mapping[str, Any]) -> bool:
    """Fail closed: both catalog admission signals must be explicit."""
    return (row.get('id') not in OPERATOR_REMOVED_MODELS
            and row.get('available') == 1 and row.get('active') is True)


_served_cache = {}
_served_lock = threading.Lock()
_served_inflight = {}
_probe_child_env = ContextVar('probe_child_env', default=None)


def provider_config_digest(config):
    """Identity of model discovery; request headers never affect discovery."""
    try:
        providers = json.loads(config).get('providers', {})
        def stable(node):
            if isinstance(node, dict):
                return {k: stable(v) for k, v in node.items() if k.lower() != 'headers'}
            if isinstance(node, list):
                return [stable(v) for v in node]
            return node
        payload = json.dumps(stable(providers), sort_keys=True)
    except (ValueError, AttributeError, TypeError):
        payload = config
    return hashlib.sha256(payload.encode()).hexdigest()


def host_probe_env(env=None):
    from .clients import build_child_env
    from .opencode_overlay import overlay_env
    return overlay_env(build_child_env(extra_env=env))


def served_models(config, *, env=None, clock=time.monotonic):
    """A failed probe is UNKNOWN, never evidence that a model is absent."""
    from concurrent.futures import Future
    from .opencode_overlay import credential_names
    env = host_probe_env(env)
    try:
        providers = json.loads(config).get('providers', {})
    except (ValueError, AttributeError, TypeError):
        providers = {}
    present = sorted(name for name in credential_names(providers) if env.get(name))
    key = (provider_config_digest(config), hashlib.sha256(json.dumps(present).encode()).hexdigest())
    with _served_lock:
        cached = _served_cache.get(key)
        if cached and clock() < cached[0]:
            return cached[1]
        future = _served_inflight.get(key)
        owner = future is None
        if owner:
            future = _served_inflight[key] = Future()
    if not owner:
        return future.result()
    try:
        token = _probe_child_env.set(env)
        try:
            result = _probe_served_models(config)
        finally:
            _probe_child_env.reset(token)
        with _served_lock:
            if len(_served_cache) >= 8:
                _served_cache.pop(next(iter(_served_cache)))
            _served_cache[key] = (clock() + (30 if not result else 300), result)
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _served_lock:
            _served_inflight.pop(key, None)


def _probe_served_models(config, *, env=None):
    import logging
    logger = logging.getLogger(__name__)
    env = dict(env or _probe_child_env.get() or host_probe_env())
    env['OPENCODE_CONFIG_CONTENT'] = config
    try:
        executable = shutil.which('opencode', path=env.get('PATH')) or 'opencode'
        result = subprocess.run([executable, 'models'], env=env, capture_output=True,
                                text=True, timeout=10)
        if result.returncode == 0:
            import re
            parsed = frozenset(line.strip() for line in result.stdout.splitlines()
                               if re.fullmatch(r'[^\s/]+/[^\s/]+', line.strip()))
            if parsed:
                return parsed
            logger.warning('host_probe_error: empty or unrecognised model listing; availability unknown')
        else:
            logger.warning('host_probe_error: opencode models exit %s; availability unknown', result.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning('host_probe_error: %s; availability unknown', type(exc).__name__)
    return None


served_models.cache_clear = _served_cache.clear
# Real function retained for tests whose autouse fixture stubs served_models.
_served_models_real = served_models


async def served_models_async(config, *, env=None, clock=time.monotonic):
    return await asyncio.to_thread(_served_models_real, config, env=env, clock=clock)


async def host_skip_reason_async(row, *, env=None, state=None):
    child = host_probe_env(env)
    models = None
    if row.get('harness') == 'opencode':
        # List the rendered config, like host_skip_reason and the launch (D-2868).
        from .opencode_render import render_gateway_providers
        probe_env, model = dict(child), row.get('model') or row.get('id') or row.get('model_id') or ''
        if '/' not in model or not render_gateway_providers(probe_env, model):
            models = await served_models_async(probe_env.get('OPENCODE_CONFIG_CONTENT', '{}'), env=probe_env)
    return host_skip_reason(row, env=child, probe=lambda env: models, state=state)


def plan_for_launch(plan, *, dry_run=False, probe=None, env=None, state=None):
    """Apply host admission at the shared real-launch boundary, never offline."""
    if dry_run:
        return plan
    from dataclasses import replace
    admitted, skipped = [], list(plan.skipped)
    options = {}
    if probe is not None:
        options['probe'] = probe
    if env is not None:
        options['env'] = env
    for leg in plan.legs:
        # state rides the row: admission reads the denial file the launch wrote (D-3259).
        reason = host_skip_reason(dict(model=leg.model, harness=leg.harness, state=state),
                                  **options)
        if reason:
            # W30 callers consume skip_reason; reason remains canonical like
            # the planner's pre-existing skipped entries.
            skipped.append(dict(leg=leg.id, reason=reason, skip_reason=reason))
        else:
            admitted.append(leg)
    return replace(plan, legs=tuple(admitted), skipped=tuple(skipped))


async def plan_for_launch_async(plan, *, dry_run=False, probe=None, env=None, state=None):
    # W32A runner: await this boundary; the synchronous API remains for CLI use.
    return await asyncio.to_thread(plan_for_launch, plan, dry_run=dry_run, probe=probe, env=env, state=state)


def admitted_by_render(model):
    """A spawner-rendered mimo provider model, admitted without the keyless listing (gateway 401; D-2868/D-2869)."""
    return str(model or '').split('#', 1)[0] in ACCEPTED_MIMO_IDS


def host_skip_reason(row, *, probe=None, env=None, state=None):
    harness = row.get('harness')
    if harness in ('agy', 'qodercli'):
        return None if shutil.which(harness) else 'harness_missing: ' + harness
    if harness != 'opencode': return None
    model = row.get('model') or row.get('id') or row.get('model_id')
    from plangraph.planner.model_admission import console_host_skip; child = host_probe_env(env)
    from plangraph.planner.host_models import is_gateway, unavailable_models; state = state or row.get('state')
    # Negative evidence gates first, providerless models included (D-3259).
    if model and model.lower().split('#', 1)[0] in unavailable_models(state=state, env=env): return 'not_served_on_host: ' + model
    if not model or '/' not in model: return None
    # D-3264: only console credit providers need an affirmative host listing.
    if model.split('/', 1)[0].lower().startswith('console-') and not is_gateway(model, child):
        return console_host_skip(model, probe, env, state=state)
    from .opencode_overlay import OVERLAY_UNKNOWN, OVERLAY_ROOT_MISSING
    if child.get(OVERLAY_UNKNOWN): return None
    # Probe the config the launch will use: S1 renders the gateway providers per run.
    from .opencode_render import render_gateway_providers
    render_reason = render_gateway_providers(child, model)
    if render_reason:
        return render_reason
    try:
        cfg = json.loads(child.get('OPENCODE_CONFIG_CONTENT', '{}'))
        provider, name = model.split('/', 1)
        entry = cfg.get('providers', {}).get(provider, {}).get('models', {}).get(name, {})
        if isinstance(entry, dict) and (entry.get('combo') or entry.get('alias')):
            return None
        if admitted_by_render(model):
            return None
    except (ValueError, AttributeError, TypeError):
        pass
    models = probe(child) if probe else served_models(child.get('OPENCODE_CONFIG_CONTENT', '{}'), env=child)
    base = model.lower().split('#', 1)[0]
    listing = {m.lower() for m in models or ()}
    variants = {m for m in listing if m.startswith(base + '#')}
    present = models is None or (base in {m.split('#', 1)[0] for m in listing}
        and ('#' not in model or not variants or model.lower() in variants))
    if present:
        return None
    reason = 'not_served_on_host: ' + model
    if child.get(OVERLAY_ROOT_MISSING):
        reason += f' (root_provider_config_missing: {child[OVERLAY_ROOT_MISSING]})'
    return reason


def is_model_available(model: Mapping[str, Any], leg_state_reader=None, *, account=None, combo=None) -> bool:
    """Check if model is eligible and available.

    Enforces Codex ACP luna-only: cxa/gpt-6-luna-high is eligible,
    while cxa/gpt-6-astra-max and cxa/gpt-6-sol-* are rejected.
    """
    if not mimo_path_allowed(str(model.get("id") or model.get("model_id") or "")):
        return False
    # Ranking historical catalog seats is not a new-spawn admission decision.
    # Credit catalog rows take the same expiry gate as credit legs (D-3264).
    if not catalog_row_admitted(model) or catalog_credit_skip_reason(model):
        return False
    status = str(model.get("status", "active")).lower()
    if status not in ("active", ""):
        return False
    mid = str(model.get("id") or model.get("model_id") or "").lower()
    if mid.startswith("cxa/") and not mid.startswith("cxa/gpt-6-luna"):
        return False
    reader = leg_state_reader or leg_status
    return reader(str(model.get("id") or model.get("model_id") or ""), account=account, combo=combo)["state"] != "limited"

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
      - high:     I_min = 46.0, I_target = 50.0, gamma_cost = 4.0,  beta_excess = 0.6
      - critical: I_min = 50.0, I_target = 54.0, gamma_cost = 4.0,  beta_excess = 0.6

    Role multipliers (alpha_role):
      - implement / worker: 1.0
      - drive / driver / orchestrator: 1.1
      - review / reviewer: 1.2

    Tier weights (W_tier):
      credit (0): +14.0, trial (1): +10.5, free (2): +7.0, subscription (3): +3.5, paid (4): 0.0, unknown: -35.0
    """
    need_tokens = _validate_need_tokens(need_tokens)
    risk_l = _validate_risk(risk)
    role_l = _validate_role(role)

    intel = _validated_metric(model, "aa_intelligence")
    cost = _validated_metric(model, "cost_per_task_usd", default=0.20)
    burn = _validated_metric(model, "pool_burn_weight", default=1.0)
    _speed_for_model(model)

    tier = str(model.get("tier") or "paid").lower()

    # Fail-closed unknown tier -> 99
    tier_rung = TIER_RUNG.get(tier, 99)
    tier_bonus = (4 - tier_rung) * 3.5 if tier_rung <= 4 else -35.0

    i_min = RISK_MIN_INTELLIGENCE[risk_l]
    i_target = RISK_TARGET_INTELLIGENCE[risk_l]
    gamma_cost = RISK_GAMMA_COST[risk_l]
    beta_excess = RISK_BETA_EXCESS[risk_l]
    alpha_role = ROLE_ALPHA[role_l]

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
    Stage 2: Absolute equivalence band in utility points (0.3 for high/critical,
             0.5 for low/normal). The band is calibrated and quantified by the
             committed sensitivity sweep. Within each band, resolve ties using
             verified throughput (Speed(M)); an existing test pins that a score
             gap <= 0.5 is tied and ordered by speed.
    """
    need_tokens = _validate_need_tokens(need_tokens)
    risk_l = _validate_risk(risk)
    role_l = _validate_role(role)

    if not candidates:
        return []

    # Validate every candidate before entering the ranking loop.  This keeps the
    # loop finite even when a caller passes a malformed later candidate.
    prepared: list[tuple[dict[str, Any], float, float]] = []
    for c in candidates:
        if not catalog_row_admitted(c) or catalog_credit_skip_reason(c):
            continue
        d = dict(c)
        score = score_model(d, role=role_l, risk=risk_l, need_tokens=need_tokens)
        speed = _raw_speed(d)
        prepared.append((d, score, speed))

    remaining = prepared
    epsilon = 0.3 if risk_l in ("high", "critical") else 0.5
    ranked: list[dict[str, Any]] = []

    while remaining:
        u_max = max(item[1] for item in remaining)

        in_band: list[tuple[dict[str, Any], float, float]] = []
        out_band: list[tuple[dict[str, Any], float, float]] = []
        for item in remaining:
            if (u_max - item[1]) <= epsilon + 1e-12:
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
    d.setdefault('available', 0)
    d.setdefault('active', False)
    if "id" not in d and "model_id" in d:
        d["id"] = d["model_id"]
    if "context_window" not in d and "context_tokens" in d:
        d["context_window"] = d["context_tokens"]
    # Harness and tool mode are catalog columns (SPAWNER-REGISTRY-ROUTE), never inferred.
    return d

_catalog_failures = {}


def get_catalog_models(db: Any = None, *, active_only: bool = False,
                       clock=time.monotonic) -> list[dict[str, Any]]:
    """Retrieve catalog models, including inactive rows unless explicitly filtered."""
    catalog_source = None
    identity = db if db is not None else _get_default_catalog_db
    try:
        # Weak identity prevents id reuse without retaining the DB or factory.
        key = weakref.ref(identity)
        hash(key)
    except TypeError:
        key = (type(identity).__name__, id(identity))
    now = clock()
    for expired in [k for k, v in _catalog_failures.items()
                    if (isinstance(k, weakref.ReferenceType) and k() is None)
                    or now >= v['warned_at'] + 30]:
        _catalog_failures.pop(expired, None)
    failure = _catalog_failures.get(key)
    if failure:
        return [dict(_normalize_catalog_row(m), catalog_source='offline_fallback_db_error')
                for m in DEFAULT_FALLBACK_MODELS if not active_only or catalog_row_admitted(m)]
    try:
        target_db = db if db is not None else _get_default_catalog_db()
        if target_db is not None:
            if hasattr(target_db, "list_models"):
                models = target_db.list_models(active_only=active_only)
                # D-2631: no admitted rows is not an empty catalog. Preserve the
                # active-only query contract, but probe total rows before fallback.
                if active_only and not models:
                    if target_db.list_models(active_only=False):
                        _catalog_failures.pop(key, None)
                        return []
                if models:
                    _catalog_failures.pop(key, None)
                    return [_normalize_catalog_row(m) for m in models
                            if not active_only or catalog_row_admitted(_normalize_catalog_row(m))]
            if hasattr(target_db, "get_models"):
                models = target_db.get_models()
                if models:
                    _catalog_failures.pop(key, None)
                    return [_normalize_catalog_row(m) for m in models
                            if not active_only or catalog_row_admitted(_normalize_catalog_row(m))]
    except Exception as exc:
        import logging
        if not failure:
            logging.getLogger(__name__).warning(
                "catalog_db_error fallback=offline err=%s", type(exc).__name__)
        if len(_catalog_failures) >= 8:
            _catalog_failures.pop(next(iter(_catalog_failures)))
        _catalog_failures[key] = dict(error=type(exc).__name__, warned_at=now)
        catalog_source = "offline_fallback_db_error"

    if not catalog_source:
        _catalog_failures.pop(key, None)
    rows = [_normalize_catalog_row(m) for m in DEFAULT_FALLBACK_MODELS
            if not active_only or catalog_row_admitted(m)]
    if catalog_source:
        rows = [dict(m, catalog_source=catalog_source) for m in rows]
    return rows

def _passes_context_and_cliff(m: Mapping[str, Any], need_tokens: int, role: str = "implement") -> bool:
    """Check if model satisfies context window and price cliff limits.

    Multiplies need_tokens by role trajectory factor (1.5x worker, 1.3x driver, 1.1x reviewer)
    before checking against both context_window and price_cliff_tokens so multi-turn tool loops
    do not cross the price cliff or overflow the effective context window mid-task.
    """
    need_tokens = _validate_need_tokens(need_tokens)
    role_l = str(role).strip().lower() if role is not None else "implement"
    factor = ROLE_TRAJECTORY_FACTORS.get(role_l, 1.5)

    ctx_value = m.get('context_window')
    if ctx_value is None:
        ctx_value = m.get('context_tokens')
    if ctx_value is None:
        ctx = 128000
    else:
        if isinstance(ctx_value, bool) or not isinstance(ctx_value, (int, float)):
            raise ValueError(f'invalid context_window: {ctx_value!r}')
        if isinstance(ctx_value, float) and not math.isfinite(ctx_value):
            raise ValueError(f'invalid context_window: {ctx_value!r}')
        if ctx_value <= 0:
            raise ValueError(f'invalid context_window: {ctx_value!r}')
        ctx = int(ctx_value)

    required_ctx = int(need_tokens * factor)
    if ctx < required_ctx:
        return False

    cliff = m.get('price_cliff_tokens')
    if cliff is not None:
        if isinstance(cliff, bool) or not isinstance(cliff, (int, float)):
            raise ValueError(f'invalid price_cliff_tokens: {cliff!r}')
        if isinstance(cliff, float) and not math.isfinite(cliff):
            raise ValueError(f'invalid price_cliff_tokens: {cliff!r}')
        if cliff < 0:
            raise ValueError(f'invalid price_cliff_tokens: {cliff!r}')
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
    leg_state_reader=None,
    account=None,
    reviewer_count=None,
    review_exempt=False,
    paths=(), bucket='S1', has_tests=True, is_pure_doc=False,
    risk_tags=(),
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
      - Raises ValueError if no eligible reviewer exists.
    """
    if review_exempt:
        raise ValueError("caller cannot assert review_exempt; zero reviewers requires evaluated exemption")
    need_tokens = _validate_need_tokens(need_tokens)
    risk_l = _validate_risk(risk)
    writer_family = detect_model_family(writer_model)
    from .risk import evaluate_risk
    assessment = evaluate_risk(paths=paths, bucket=bucket, has_tests=has_tests,
                               is_pure_doc=is_pure_doc, risk_tags=risk_tags)
    review_exempt = assessment.review_exempt
    if assessment.risk in ('high', 'critical'):
        risk_l = assessment.risk
    if reviewer_count == 0:
        if not review_exempt or risk_l in ('high', 'critical'):
            raise ValueError('zero reviewers requires low/normal review_exempt')
        return {'reviewer_count': 0, 'reviewers': [], 'review_exempt': True}
    reviewer_count = max(reviewer_count or 1, 2 if risk_l in ("high", "critical") else 1)

    reader = leg_state_reader or leg_status
    def annotate(m):
        d = dict(m)
        d["leg_state"] = reader(str(m.get("id") or m.get("model_id") or ""), account=account, combo=combo_name)
        return d
    all_models = [annotate(m) for m in get_catalog_models(db)]

    # Filter candidate models
    target_db = None if any(m.get("catalog_source") == "offline_fallback_db_error" for m in all_models) else (db if db is not None else _get_default_catalog_db())
    combo_priority_legs = False
    candidates: list[dict[str, Any]] = []

    if target_db is not None and hasattr(target_db, "get_route_legs") and combo_name:
        route_legs = target_db.get_route_legs(combo_name, active_only=True)
        if route_legs:
            candidates = [
                annotate(_normalize_catalog_row(r))
                for r in route_legs
                if is_model_available(_normalize_catalog_row(r), reader, account=account, combo=combo_name) and _passes_context_and_cliff(r, need_tokens, role="review")
            ]
            combo_priority_legs = True

    if not candidates:
        for m in all_models:
            if not is_model_available(m, reader, account=account, combo=combo_name):
                continue
            combos = m.get("combos")
            if combos and combo_name not in combos:
                continue
            if not _passes_context_and_cliff(m, need_tokens, role="review"):
                continue
            candidates.append(m)

    min_intelligence = RISK_MIN_INTELLIGENCE[risk_l]

    def _pick_reviewer(
        pool: Sequence[Mapping[str, Any]],
        excluded_families: set[str],
    ) -> dict[str, Any] | None:
        eligible = [
            dict(c)
            for c in pool
            if detect_model_family(str(c.get("id") or c.get("model_id") or ""))
            not in excluded_families
        ]

        if not eligible:
            # Emergency relaxation outside combo, still enforcing availability,
            # context/price cliff, and family exclusions.
            eligible = [
                dict(c)
                for c in all_models
                if is_model_available(c, reader, account=account, combo=combo_name)
                and _passes_context_and_cliff(c, need_tokens, role="review")
                and detect_model_family(str(c.get("id") or c.get("model_id") or ""))
                not in excluded_families
            ]

        if not eligible:
            return None

        if combo_priority_legs:
            qualifying = []
            for c in eligible:
                try:
                    intel = _validated_metric(c, "aa_intelligence")
                except ValueError:
                    continue
                if intel >= min_intelligence:
                    qualifying.append(c)
            if qualifying:
                return qualifying[0]
            return eligible[0]

        ranked = rank_candidates(
            eligible, role="review", risk=risk_l, need_tokens=need_tokens
        )
        return ranked[0] if ranked else None

    reviewer_1 = _pick_reviewer(candidates, {writer_family})
    if reviewer_1 is None:
        raise ValueError("no eligible reviewer family distinct from writer")

    reviewer_1_id = str(reviewer_1.get("id") or reviewer_1.get("model_id") or "")
    family_1 = detect_model_family(reviewer_1_id)

    headers_1 = build_routing_envelope_headers(
        role="review",
        risk=risk_l,
        min_intelligence=min_intelligence,
        required_context=need_tokens,
        exclude_families=[writer_family],
        effort="medium",
        author_model=writer_model,
    )

    reviewers: list[dict[str, Any]] = [
        {
            "index": 1,
            "model": reviewer_1_id,
            "leg_state": reviewer_1["leg_state"],
            "family": family_1,
            "combo": combo_name,
            "headers": headers_1,
            "score": score_model(
                reviewer_1, role="review", risk=risk_l, need_tokens=need_tokens
            ),
        }
    ]

    if reviewer_count == 2:
        reviewer_2 = _pick_reviewer(candidates, {writer_family, family_1})
        if reviewer_2 is None:
            raise ValueError("no eligible second reviewer family")

        reviewer_2_id = str(reviewer_2.get("id") or reviewer_2.get("model_id") or "")
        family_2 = detect_model_family(reviewer_2_id)

        headers_2 = build_routing_envelope_headers(
            role="review",
            risk=risk_l,
            min_intelligence=min_intelligence,
            required_context=need_tokens,
            exclude_families=[writer_family, family_1],
            effort="medium",
            author_model=writer_model,
        )

        reviewers.append(
            {
                "index": 2,
                "model": reviewer_2_id,
                "leg_state": reviewer_2["leg_state"],
                "family": family_2,
                "combo": combo_name,
                "headers": headers_2,
                "score": score_model(
                    reviewer_2, role="review", risk=risk_l, need_tokens=need_tokens
                ),
            }
        )

    return {
        "writer_model": writer_model,
        "writer_family": writer_family,
        "risk": risk_l,
        "combo": combo_name,
        "need_tokens": need_tokens,
        "reviewer_count": reviewer_count,
        "reviewers": reviewers,
        "candidates": all_models,
    }

# New helper name: _risk_envelope_intelligence
def _risk_envelope_intelligence(
    *,
    role: str,
    risk_l: str,
    combo_name: str,
    need_tokens: int,
    exclude_families: Sequence[str],
    effort: str,
    author_model: str | None,
    db: Any,
    leg_state_reader=None,
    account=None,
) -> float | None:
    order = ("low", "normal", "high", "critical")
    if risk_l not in order:
        return None

    picked: list[float] = []
    for lower_risk in order[: order.index(risk_l)]:
        try:
            lower = resolve_model_for_task(
                role=role,
                risk=lower_risk,
                combo_name=combo_name,
                need_tokens=need_tokens,
                exclude_families=exclude_families,
                effort=effort,
                author_model=author_model,
                db=db,
                relax_floor=True,
                relax_reason="monotone-envelope",
                _envelope=False,
                leg_state_reader=leg_state_reader,
                account=account,
            )
        except ValueError:
            continue

        candidate = lower.get("candidate")
        if isinstance(candidate, Mapping):
            picked.append(_validated_metric(candidate, "aa_intelligence"))

    return max(picked) if picked else None

def resolve_model_for_task(
    role: str = "implement",
    risk: str = "normal",
    combo_name: str = "l2-worker",
    need_tokens: int = 32000,
    exclude_families: Sequence[str] = (),
    effort: str = "medium",
    author_model: str | None = None,
    db: Any = None,
    relax_floor: bool = False,
    relax_reason: str = "",
    _envelope: bool = True,
    leg_state_reader=None,
    account=None,
) -> dict[str, Any]:
    """Resolve the optimal model candidate for a given task role and risk band.

    Enforces:
      - 5-rung tier ladder (subscription prioritized over paid)
      - Model utility scoring (mimo/mimo-v2.6-pro prioritized over qwen3.8-max)
      - Minimum intelligence threshold for the risk band; high/critical fail closed
      - Availability and gated model exclusion
      - Family exclusion (including author_model family)
      - Context window and price cliff limits (with role trajectory factor buffer)
      - Two-stage lexicographic tie-breaker via rank_candidates
      - Vendor effort clamping (Sonnet 5.5 max -> xhigh)
      - Best-effort risk-monotone intelligence envelope across low<normal<high<critical
    """
    need_tokens = _validate_need_tokens(need_tokens)
    role_l = _validate_role(role)
    risk_l = _validate_risk(risk)

    exclude_families_seq = tuple(exclude_families or ())
    excluded = {
        str(f).lower().strip()
        for f in exclude_families_seq
        if f is not None and str(f).strip()
    }
    if author_model:
        excluded.add(detect_model_family(author_model))

    envelope_intelligence: float | None = None
    if _envelope:
        envelope_intelligence = _risk_envelope_intelligence(
            role=role_l,
            risk_l=risk_l,
            combo_name=combo_name,
            need_tokens=need_tokens,
            exclude_families=exclude_families_seq,
            effort=effort,
            author_model=author_model,
            db=db,
            leg_state_reader=leg_state_reader,
            account=account,
        )

    min_intelligence = RISK_MIN_INTELLIGENCE[risk_l]
    reader = leg_state_reader or leg_status
    all_models = get_catalog_models(db)
    all_models = [dict(m, leg_state=reader(str(m.get("id") or m.get("model_id") or ""), account=account, combo=combo_name)) for m in all_models]

    def _eligible(m: Mapping[str, Any], require_combo: bool) -> bool:
        if not is_model_available(m, reader, account=account, combo=combo_name):
            return False
        combos = m.get("combos")
        if require_combo and combo_name and combos and combo_name not in combos:
            return False
        mid = str(m.get("id") or m.get("model_id") or "")
        if detect_model_family(mid) in excluded:
            return False
        if not _passes_context_and_cliff(m, need_tokens, role=role_l):
            return False
        return True

    # 1. Filter candidates within combo
    candidates: list[dict[str, Any]] = [
        dict(m) for m in all_models if _eligible(m, require_combo=True)
    ]

    # Fallback: relax combo constraint while keeping all exclusions, availability,
    # context, and price-cliff guards.
    if not candidates:
        candidates = [
            dict(m) for m in all_models if _eligible(m, require_combo=False)
        ]

    if not candidates:
        raise ValueError("no eligible model candidates")

    if envelope_intelligence is not None:
        envelope_candidates = [
            c
            for c in candidates
            if _validated_metric(c, "aa_intelligence") >= envelope_intelligence
        ]
        if envelope_candidates:
            candidates = envelope_candidates
        # If none can meet the envelope, keep the previous behaviour for this
        # band (floor rules and ValueError/relax as now).

    qualifying: list[dict[str, Any]] = []
    for c in candidates:
        if _validated_metric(c, "aa_intelligence") >= min_intelligence:
            qualifying.append(c)

    relaxed = False
    reason = str(relax_reason or "").strip()

    if qualifying:
        candidates = qualifying
    else:
        if risk_l in ("high", "critical"):
            if not relax_floor or not reason:
                raise ValueError("no candidate meets risk intelligence floor")
            relaxed = True

        # For explicit high/critical relaxation, and for low/normal floor misses,
        # keep the highest-intelligence slice.  This preserves risk-monotone
        # intelligence even when the floor cannot be met.
        max_intel = max(_validated_metric(c, "aa_intelligence") for c in candidates)
        candidates = [
            c
            for c in candidates
            if abs(_validated_metric(c, "aa_intelligence") - max_intel) <= 1e-12
        ]

    ranked_candidates = rank_candidates(
        candidates, role=role_l, risk=risk_l, need_tokens=need_tokens
    )
    if not ranked_candidates:
        raise ValueError("no eligible model candidates")

    selected = ranked_candidates[0]
    selected_id = str(selected.get("id") or selected.get("model_id") or "")
    selected_family = detect_model_family(selected_id)
    clamped_effort = clamp_effort(selected_id, effort)

    headers = build_routing_envelope_headers(
        role=role_l,
        risk=risk_l,
        min_intelligence=min_intelligence,
        required_context=need_tokens,
        exclude_families=list(excluded),
        effort=clamped_effort,
        author_model=author_model,
    )

    return {
        "model": selected_id,
        "leg_state": selected["leg_state"],
        "candidates": all_models,
        "family": selected_family,
        "tier": str(selected.get("tier", "subscription")).lower(),
        "score": score_model(
            selected, role=role_l, risk=risk_l, need_tokens=need_tokens
        ),
        "combo": combo_name,
        "effort": clamped_effort,
        "headers": headers,
        "candidate": selected,
        "relaxed": relaxed,
        "relax_reason": reason if relaxed else "",
        "envelope_intelligence": envelope_intelligence,
    }
