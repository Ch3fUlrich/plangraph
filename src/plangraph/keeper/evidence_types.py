"""Shared types for the keeper's pluggable evidence verifiers.

Kept tiny and dependency-free so the store, the verifiers and every test share
one definition of :class:`Result` and :class:`Context`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["Result", "Context", "as_epoch", "valid_commit_ref", "DEFAULT_SKEW_S",
           "has_live_clause", "live_evidence_status", "no_deploy_declaration",
           "oneshot_timer", "proven_pairs"]

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
    the moment the item was claimed. The floor is the ref head sha ("base")
    recorded at the item's latest release or latest claim by a different
    principal; ``bind_floor_required=False`` only when no such event exists
    (first claim). ``bind_floor_shas`` maps repo key -> that repo's head at the
    same instant (the floor event's ``bases``, D-3335): ``None`` for a context
    from before per-repo floors (``bind_floor_sha`` then applies to every repo),
    ``{}`` when the floor event predates them. ``merge_principals`` lists
    allowed merge-commit committer emails (empty permits any committer).
    """

    git: Any = None
    ci: Any = None
    runs: Any = None
    semaphore: Any = None
    decisions: Any = None
    now: float = 0.0
    claim_time: float | None = None
    merge_principals: tuple[str, ...] = ()
    bind_floor_sha: str | None = None
    bind_floor_shas: dict | None = None
    bind_floor_required: bool = True
    skew: float = DEFAULT_SKEW_S
    spawner_records: Any = None
    claude_projects_dir: str | None = None


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


_LIVE_CLAUSE_RE = re.compile(r"(?:^|[.!?;]\s+|\n\s*)Live:\s*\S")


def has_live_clause(item: dict) -> bool:
    """True when the done criteria carry a ``Live:`` clause."""
    return bool(_LIVE_CLAUSE_RE.search(item.get("done_criteria") or ""))


_NO_DEPLOY_RE = re.compile(r"(?im)^[ \t]*no deploy needed:[ \t]*(.*?)[ \t]*$")


def no_deploy_declaration(item: dict) -> str | None:
    """The reason of a ``no deploy needed: <reason>`` line in the description or note; ``""`` when the
    line is there with an empty reason; ``None`` when the item has no such line."""
    text = f"{item.get('description') or ''}\n{item.get('note') or ''}"
    reasons = [m.group(1) for m in _NO_DEPLOY_RE.finditer(text)]
    return next((r for r in reasons if r), "" if reasons else None)


def oneshot_timer(ref: dict) -> str | None:
    """The timer a oneshot deploy row needs active beside it, or ``None`` when the row is not a oneshot."""
    unit = ref.get("unit")
    if (ref.get("type") == "oneshot" and isinstance(unit, str)
            and not unit.startswith("bin:") and unit.endswith(".service")):
        return unit.removesuffix(".service") + ".timer"
    return None


def proven_pairs(refs) -> set:
    """``(host, unit)`` pairs that verified deploy refs prove: a oneshot service counts only beside an
    active row of its timer on the same host. The deployment gate and the close gate share this match;
    each passes the rows it counts as eligible."""
    refs = list(refs)
    active = {(ref.get("host"), ref.get("unit")) for ref in refs if ref.get("is_active") == "active"}
    return {(ref.get("host"), ref.get("unit")) for ref in refs
            if (timer := oneshot_timer(ref)) is None or (ref.get("host"), timer) in active}


def has_live_proof(item: dict, rows, since: float | None = None) -> bool:
    """A verified ``deploy`` row of the item's current epoch, recorded at or after ``since`` (epoch seconds;
    ``None`` = no floor, infinity = nothing counts), proves a unit live.

    A timer row that only serves as the companion of a oneshot row in the same set is no proof of its
    own: the oneshot must have its active timer on the same host, and the pair is one proof."""
    refs = [row.get("ref") or {} for row in rows
            if row["kind"] == "deploy" and row["verified"] and row["epoch"] == item["epoch"]
            and (since is None or (as_epoch(row["at"]) or 0) >= since)]
    companions = {timer for ref in refs if (timer := oneshot_timer(ref))}
    return any(unit not in companions for _, unit in proven_pairs(refs))


def live_evidence_status(item: dict, rows, since: float | None = None) -> str | None:
    """Why an item may not close yet, or ``None``: every item needs a verified ``deploy`` row of its current
    epoch (the item went into production) or a ``no deploy needed: <reason>`` line.

    An explicit ``Live:`` clause is met by the row only; the declaration never excuses it. ``since`` is the
    item's latest landing: a row recorded before it proves nothing about newer work.
    Codes: ``live_evidence_missing``, ``no_deploy_reason_empty``."""
    if has_live_proof(item, rows, since):
        return None
    if has_live_clause(item):
        return "live_evidence_missing"
    declared = no_deploy_declaration(item)
    if declared:
        return None
    return "no_deploy_reason_empty" if declared == "" else "live_evidence_missing"
