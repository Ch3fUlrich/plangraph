"""Review tier validation and defaults (split from store_create)."""
from __future__ import annotations

import re
from typing import Any

from .errors import Refused

REVIEW_TIERS = ("standard", "high", "none")
_REVIEW_KEYWORD = re.compile(
    r"(?i)(?<![a-z])(secret|secrets|key|keys|security|gateway|omniroute|deploy"
    r"|keeper|fence)(?![a-z])")

_DECISION_TIER = "none"
_ALLOWED_DECISION_KINDS = (frozenset({"decision"}), frozenset({"decision", "commit"}))

_TIER_FIELDS = frozenset(("review_tier", "evidence_kinds"))

_SEAT_CLAUSE = re.compile(
    r"(?<![\w-])(?:[2-9]|two|three|four|five|six|seven|eight|nine)\s+"
    r"(?:(?!(?:of|the)\b)[a-z]+(?:-[a-z]+)*\s+){0,3}"
    r"(?:seats?|families|family|reviewers?|verdicts?)(?![\w-])",
    re.IGNORECASE)


def seat_clause_needs_high(text: str | None) -> str | None:
    """Return a multi-seat clause, excluding table-navigation prose."""
    match = _SEAT_CLAUSE.search(text or "")
    return match.group(0) if match else None


def check_seat_clause_tier(done_criteria: str | None, tier: str) -> None:
    """Refuse criteria promising multiple review seats outside the high tier."""
    clause = seat_clause_needs_high(done_criteria)
    if clause and tier != "high":
        raise Refused(
            "validation",
            f"done_criteria ask for {clause} but review_tier is {tier!r}; "
            "two seats/families need review_tier 'high' "
            "(raise the tier or reduce the clause)")


def default_review_tier(project: str, identifier: str, title: str, track: str) -> str:
    """The tier an item gets when nobody named one: ``high`` for risky work."""
    if project == "plangraph":
        return "high"
    for value in (identifier, title, track):
        if value and _REVIEW_KEYWORD.search(value):
            return "high"
    return "standard"


def validated_review_tier(value: Any) -> str:
    """Return *value* if it is a legal tier, else refuse as ``validation``."""
    if value not in REVIEW_TIERS:
        raise Refused("validation",
                      f"review_tier must be one of {list(REVIEW_TIERS)}")
    return value


def tier_needs_router(tier: str) -> bool:
    """X7b: only the router may put an item outside the review system."""
    return tier == _DECISION_TIER


def validated_decision_tier(tier: str, evidence_kinds: Any) -> str:
    """Return *tier*; a ``none`` item must have evidence_kinds == ['decision']
    or ['decision', 'commit'] (order-insensitive), or it is refused.
    """
    if tier == _DECISION_TIER:
        kinds_list = list(evidence_kinds or [])
        kinds_set = frozenset(kinds_list)
        if len(kinds_list) != len(kinds_set) or kinds_set not in _ALLOWED_DECISION_KINDS:
            raise Refused("validation",
                          f"review_tier='none' requires evidence_kinds="
                          f"['decision'] or ['decision', 'commit'], not {kinds_list}")
    return tier


def _tier_only(fields: Any) -> bool:
    """D-434: a non-empty subset of the two tier fields (empty or non-mapping
    answers ``False``, so those shapes keep the ordinary open-only refusal)."""
    return (isinstance(fields, dict) and bool(fields)
            and not set(fields) - _TIER_FIELDS)
