"""policy.py - pure reaping decision policy (SESSION-AUTO-CLOSE, D-2442)."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Union

from .scan import parse_iso_dt

DEFAULT_ALLOWLIST = (
    "*-L1-*",
    "routing*",
    "*judge*",
    "*planner*",
    "plangraph-oversight*",
)

DEFAULT_DONE_GRACE_MIN = 2
DEFAULT_IDLE_MIN = 30


@dataclass
class ReapConfig:
    done_grace_min: float = DEFAULT_DONE_GRACE_MIN
    idle_min: float = DEFAULT_IDLE_MIN
    allowlist: Sequence[str] = DEFAULT_ALLOWLIST


def _normalize_dt(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return parse_iso_dt(dt)


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def decide(
    record: Any,
    now: Any,
    cfg: Optional[Union[ReapConfig, dict, Any]] = None,
) -> tuple[str, str]:
    """Pure decision function: decide whether to stop or keep a background session.

    Returns:
        (action, reason) where action is 'stop' or 'keep'.
    """
    # Resolve config
    if cfg is None:
        done_grace_min = DEFAULT_DONE_GRACE_MIN
        idle_min = DEFAULT_IDLE_MIN
        allowlist = DEFAULT_ALLOWLIST
    elif isinstance(cfg, dict):
        done_grace_min = cfg.get("done_grace_min", DEFAULT_DONE_GRACE_MIN)
        idle_min = cfg.get("idle_min", DEFAULT_IDLE_MIN)
        allowlist = cfg.get("allowlist", DEFAULT_ALLOWLIST)
    else:
        done_grace_min = getattr(cfg, "done_grace_min", DEFAULT_DONE_GRACE_MIN)
        idle_min = getattr(cfg, "idle_min", DEFAULT_IDLE_MIN)
        allowlist = getattr(cfg, "allowlist", DEFAULT_ALLOWLIST)

    # Extract record properties
    name = str(_get_val(record, "name", "") or "")
    state = str(_get_val(record, "state", "") or "")
    has_result = bool(_get_val(record, "has_result", False))
    allow_stop = _get_val(record, "allow_stop", None)
    ttl_min = _get_val(record, "ttl_min", None)

    # Check ttl dict if present and allow_stop/ttl_min not extracted directly
    ttl_dict = _get_val(record, "ttl", None)
    if isinstance(ttl_dict, dict):
        if allow_stop is None and "allow_stop" in ttl_dict:
            allow_stop = bool(ttl_dict["allow_stop"])
        if ttl_min is None and "ttl_min" in ttl_dict:
            try:
                ttl_min = int(ttl_dict["ttl_min"])
            except (ValueError, TypeError):
                pass

    # 1. Allowlist: glob patterns on name -> always keep, reason allowlisted
    for pattern in allowlist:
        if fnmatch.fnmatch(name, pattern):
            return "keep", "allowlisted"

    # 2. Per-session TTL: allow_stop: false keeps
    if allow_stop is False:
        return "keep", "ttl_no_stop"

    # 3. Stop only if state == 'done'
    if state != "done":
        return "keep", f"state_{state}" if state else "not_done"

    # Compute session age
    now_dt = _normalize_dt(now)
    updated_dt = _normalize_dt(_get_val(record, "updated_at", None))
    if now_dt is None or updated_dt is None:
        return "keep", "missing_timestamp"

    age_seconds = (now_dt - updated_dt).total_seconds()
    if age_seconds < 0:
        return "keep", "future_timestamp"

    # 4. DONE with result -> stop after done_grace_min (default 2)
    if has_result:
        grace_seconds = float(done_grace_min) * 60.0
        if age_seconds >= grace_seconds:
            return "stop", "done_with_result"
        return "keep", "within_grace"

    # 5. Done without result -> stop after idle_min (default 30, replaced by ttl_min if set)
    effective_idle_min = float(ttl_min) if ttl_min is not None else float(idle_min)
    idle_seconds = effective_idle_min * 60.0
    if age_seconds >= idle_seconds:
        return "stop", "done_idle"
    return "keep", "within_idle"
