"""Credit-tier expiry gate shared by legs and catalog rows (D-3258/D-3264).

A ``tier == "credit"`` entry -- leg or catalog row -- is only usable while the
route policy carries a valid, unexpired ``credit_tiers`` entry for its provider
prefix (``model.partition('/')[0]``).  No entry, an unparsable ``expires`` or a
past date gates the entry with ``credit_expired``: fail closed.

``RoutePolicy.skip_reason`` applies this gate to legs.  Catalog rows take the
same gate on the automatic path (``is_model_available`` / ``rank_candidates``)
so a credit catalog row can never be scored at the top (W_tier +14.0) without
live credit.  The default policy comes from ``load_route_policy`` (honours
``PLANGRAPH_ROUTE_POLICY``); an unusable policy file gates every credit row.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from typing import Any

__all__ = ["GATED", "credit_expiry_skip", "catalog_credit_skip_reason",
           "load_default_policy", "clear_policy_cache", "policy_today"]

#: Skip reason for a credit entry without live credit (same spelling as legs).
GATED = "credit_expired"

_policy_cache: dict[tuple[str, int], object] = {}


def credit_expiry_skip(model: str, tier: str,
                       credit_tiers: Mapping[str, dict], today: date) -> str | None:
    """The expiry gate for one entry; ``None`` only with live credit."""
    if str(tier or "").lower() != "credit":
        return None
    entry = credit_tiers.get(str(model or "").partition("/")[0])
    try:
        expires = date.fromisoformat(entry["expires"])
    except (KeyError, TypeError, ValueError):
        return GATED
    return GATED if expires < today else None


def load_default_policy():
    """Cached ``load_route_policy``; ``None`` when the policy is unusable.

    Caching freezes the file, never the day: gating re-evaluates ``today`` per
    call (:meth:`RoutePolicy.current_today`).
    """
    from plangraph.planner.route_policy import DEFAULT_POLICY_PATH, load_route_policy

    path = os.environ.get("PLANGRAPH_ROUTE_POLICY") or DEFAULT_POLICY_PATH
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = -1
    key = (str(path), stamp)
    if key in _policy_cache:
        return _policy_cache[key]
    try:
        policy = load_route_policy(path)
    except ValueError:
        policy = None  # fail closed for credit rows; other tiers are unaffected
    if len(_policy_cache) > 8:
        _policy_cache.clear()
    _policy_cache[key] = policy
    return policy


def clear_policy_cache() -> None:
    """Drop the cached default policy (policy file swapped under a process)."""
    _policy_cache.clear()


def policy_today(policy: Any) -> date:
    """The day ``policy`` gates credit with; re-read per call, never frozen.

    Duck-typed: a policy without ``current_today`` falls back to its ``today``.
    """
    getter = getattr(policy, "current_today", None)
    return getter() if callable(getter) else policy.today


def catalog_credit_skip_reason(row: Mapping[str, Any] | None,
                               policy: Any = None) -> str | None:
    """Expiry gate for a catalog row; the same contract as a credit leg.

    ``policy`` is a ``RoutePolicy`` (its ``today`` governs, keeping the verdict
    identical to ``RoutePolicy.skip_reason``); the default policy is loaded
    lazily and cached.
    """
    if not isinstance(row, Mapping) or str(row.get("tier") or "").lower() != "credit":
        return None
    model = str(row.get("id") or row.get("model_id") or "")
    if policy is None:
        policy = load_default_policy()
        if policy is None:
            return GATED
    return credit_expiry_skip(model, "credit", policy.credit_tiers, policy_today(policy))
