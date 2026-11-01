"""policy.py - pure reaping decision policy (SESSION-AUTO-CLOSE, D-2442).

Order of checks matters: every guard that keeps a session runs before the
timing rules, and an input that cannot be trusted (unknown name, garbled
marker, no real timestamp) keeps the session.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Iterable, Optional, Sequence, Union

from .scan import parse_iso_dt, parse_ttl_marker

DEFAULT_ALLOWLIST = (
    "*-L1-*",
    "routing*",
    "*judge*",
    "*planner*",
    "plangraph-oversight*",
)

DEFAULT_DONE_GRACE_MIN = 2
DEFAULT_IDLE_MIN = 30
# No threshold may be shorter than this, so a 0/negative value (typo, bad marker)
# cannot make every finished session stoppable on the very next run.
MIN_THRESHOLD_MIN = 1.0


@dataclass
class ReapConfig:
    done_grace_min: float = DEFAULT_DONE_GRACE_MIN
    idle_min: float = DEFAULT_IDLE_MIN
    # Extra patterns. They are added to DEFAULT_ALLOWLIST, never a replacement for it.
    allowlist: Sequence[str] = ()


def effective_allowlist(extra: Optional[Iterable[str]] = None) -> tuple[str, ...]:
    """DEFAULT_ALLOWLIST plus the operator's extra patterns, de-duplicated."""
    merged = list(DEFAULT_ALLOWLIST)
    for pattern in extra or ():
        if pattern not in merged:
            merged.append(pattern)
    return tuple(merged)


def is_allowlisted(name: str, extra: Optional[Iterable[str]] = None) -> bool:
    """Case-insensitive glob match on every platform (fnmatch alone follows the OS)."""
    folded = name.casefold()
    return any(fnmatchcase(folded, p.casefold()) for p in effective_allowlist(extra))


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


def _cfg_val(cfg: Any, key: str, default: Any) -> Any:
    if cfg is None:
        return default
    return _get_val(cfg, key, default)


def _minutes(value: Any) -> Optional[float]:
    """A finite real number of minutes, or None for anything else (bool, str, nan, inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _floor(minutes: float) -> float:
    return max(minutes, MIN_THRESHOLD_MIN)


def decide(
    record: Any,
    now: Any,
    cfg: Optional[Union[ReapConfig, dict, Any]] = None,
) -> tuple[str, str]:
    """Pure decision function: decide whether to stop or keep a background session.

    Returns:
        (action, reason) where action is 'stop' or 'keep'.
    """
    done_grace_min = _cfg_val(cfg, "done_grace_min", DEFAULT_DONE_GRACE_MIN)
    idle_min = _cfg_val(cfg, "idle_min", DEFAULT_IDLE_MIN)
    extra_allowlist = _cfg_val(cfg, "allowlist", ())

    name = _get_val(record, "name", "")
    name = name.strip() if isinstance(name, str) else ""
    state = str(_get_val(record, "state", "") or "")
    has_result = _get_val(record, "has_result", False)
    allow_stop = _get_val(record, "allow_stop", None)
    ttl_min = _get_val(record, "ttl_min", None)
    ttl_error = _get_val(record, "ttl_error", None)

    # A plain-dict record may carry the raw ttl.json body; parse it as strictly as scan does.
    ttl_dict = _get_val(record, "ttl", None)
    if isinstance(ttl_dict, dict) and allow_stop is None and ttl_min is None and not ttl_error:
        allow_stop, ttl_min, ttl_error = parse_ttl_marker(ttl_dict)

    # 1. A session we cannot name cannot be matched against the allowlist: keep.
    if not name:
        return "keep", "unknown_name"

    # 2. Allowlist: glob patterns on name -> always keep.
    if is_allowlisted(name, extra_allowlist):
        return "keep", "allowlisted"

    # 3. Per-session marker: unreadable/garbled -> keep; allow_stop false -> keep.
    if ttl_error:
        return "keep", "ttl_invalid"
    if allow_stop is False:
        return "keep", "ttl_no_stop"
    # Only a real True/None may reach the timing rules; 0, "false", 0.0 etc. are garbled.
    if allow_stop is not None and allow_stop is not True:
        return "keep", "ttl_invalid"
    # A marker ttl_min that is not a positive number is garbled, not a short TTL.
    if ttl_min is not None:
        ttl_min = _minutes(ttl_min)
        if ttl_min is None or ttl_min <= 0:
            return "keep", "ttl_invalid"

    # A truthy string such as "false" would shorten the idle threshold to the grace one.
    if not isinstance(has_result, bool):
        return "keep", "result_invalid"

    # 4. Stop only if state == 'done'.
    if state != "done":
        return "keep", f"state_{state}" if state else "not_done"

    # 5. Age needs a real, timezone-aware updatedAt and a sane clock.
    raw_updated = _get_val(record, "updated_at", None)
    if isinstance(raw_updated, datetime) and raw_updated.tzinfo is None:
        return "keep", "missing_timestamp"
    now_dt = _normalize_dt(now)
    updated_dt = _normalize_dt(raw_updated)
    if now_dt is None or updated_dt is None:
        return "keep", "missing_timestamp"

    age_seconds = (now_dt - updated_dt).total_seconds()
    if age_seconds < 0:
        return "keep", "future_timestamp"

    # 6. Threshold: ttl_min (when the launcher set one) replaces both defaults.
    if ttl_min is not None:
        threshold_min, within = _floor(ttl_min), "within_ttl"
    else:
        configured = _minutes(done_grace_min if has_result else idle_min)
        if configured is None:
            return "keep", "config_invalid"
        threshold_min = _floor(configured)
        within = "within_grace" if has_result else "within_idle"

    if age_seconds >= threshold_min * 60.0:
        return "stop", "done_with_result" if has_result else "done_idle"
    return "keep", within
