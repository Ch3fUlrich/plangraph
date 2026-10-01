"""Shared types for the keeper's pluggable evidence verifiers.

Kept tiny and dependency-free so the store, the verifiers and every test share
one definition of :class:`Result` and :class:`Context`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["Result", "Context", "as_epoch"]


@dataclass
class Result:
    """Outcome of one verification.

    ``ref`` echoes the evidence reference that was checked so a caller can store
    it alongside ``verified`` and ``detail`` without reconstructing it.
    """

    ok: bool
    detail: str
    ref: dict = field(default_factory=dict)


@dataclass
class Context:
    """Injected adapters plus the ambient facts a verifier needs.

    Tests build this (or any duck-typed stand-in) with fakes; production builds
    it with :func:`plangraph.keeper.evidence.default_context`. ``claim_time`` is
    the moment the item was claimed and is compared against a commit's time; a
    missing claim time fails closed rather than passing the check.
    """

    git: Any = None
    ci: Any = None
    runs: Any = None
    semaphore: Any = None
    decisions: Any = None
    now: float = 0.0
    claim_time: float | None = None


def as_epoch(value: Any) -> float | None:
    """Coerce an epoch number or an ISO-8601 string to a POSIX timestamp.

    Returns ``None`` for anything that cannot be understood; callers treat that
    as "unknown", never as zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
