"""Shared types for the keeper's pluggable evidence verifiers.

Kept tiny and dependency-free so the store, the verifiers and every test share
one definition of :class:`Result` and :class:`Context`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["Result", "Context", "as_epoch", "valid_commit_ref", "DEFAULT_SKEW_S"]

# A commit reference is a full lowercase hex sha: 40 for sha1, 64 for sha256.
# Abbreviations, refs ("main") and anything option-like are rejected so a sha
# can never become a git/gh option.  ``fullmatch`` (not ``$``) so a trailing
# newline cannot smuggle an otherwise-clean sha past the check.
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

# Clock skew tolerated between a commit's author/committer dates and the claim.
DEFAULT_SKEW_S = 120.0


def valid_commit_ref(value: Any) -> bool:
    """True only for a full lowercase hex commit sha (40 or 64 hex digits)."""
    return isinstance(value, str) and _COMMIT_RE.fullmatch(value) is not None


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
    the moment the item was claimed. ``bind_floor_time`` is the lower commit-
    time bound for a trailer-bound commit: the later of the item's last
    ``released`` event and its last ``claimed`` event by a principal other than
    the current claimant (``None`` when neither exists, which puts commits that
    carry the item's ``Plan-Item`` trailer under no time bound at all).
    """

    git: Any = None
    ci: Any = None
    runs: Any = None
    semaphore: Any = None
    decisions: Any = None
    now: float = 0.0
    claim_time: float | None = None
    bind_floor_time: float | None = None
    skew: float = DEFAULT_SKEW_S


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
