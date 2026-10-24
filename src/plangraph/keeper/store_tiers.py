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

_MULTI_INTEGER = r"(?:0*[2-9]|0*[1-9][0-9]+)"
_COUNT = (
    rf"(?:[0-9]+\s*(?:-|\bto\b)\s*{_MULTI_INTEGER}"
    rf"|(?:(?:at\s+least|min)\s+)?{_MULTI_INTEGER}\+?"
    r"|twenty(?:-(?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|both|a\s+second|another)")
_SEAT_NOUN = r"(?:seats|families|reviewers|verdicts|sign-offs)"
_SEAT_CLAUSE = re.compile(
    rf"(?<![\w-])(?:{_SEAT_NOUN}\s*[:=]\s*{_COUNT}(?![\w+-])"
    rf"|{_COUNT}(?:-(?:seat|family|reviewer|verdict|sign-off)(?![\w-])"
    rf"|[\s,]+(?:of\s+(?:the\s+)?)?"
    rf"(?:[a-z0-9]+(?:-[a-z0-9]+)*[\s,]+){{0,3}}?{_SEAT_NOUN}"
    r"(?![\w'-]|’)(?!\s+(?:table|section|list|chart|column|row|page|docs?"
    r"|comments|findings|notes|feedback|packages|licenses)\b)))",
    re.IGNORECASE)
_NAVIGATION = re.compile(
    r"\b(?:step|table|section|figure|phase|row|column|part|item|level|tier|page"
    r"|chapter)\s+$", re.IGNORECASE)


def seat_clause_needs_high(text: str | None) -> str | None:
    """Return a multi-seat clause, excluding table-navigation prose."""
    text = text or ""
    for match in _SEAT_CLAUSE.finditer(text):
        if not _NAVIGATION.search(text[:match.start()]):
            return match.group(0)
    return None


# 'high' alone needs two families; 'none'/'standard' are refused by design.
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
