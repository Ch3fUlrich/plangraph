"""Jev shadow questions: build the four ``choice`` questions the rules already answer.

Jev (a free decision model) answers a ``choice`` question over a ``state`` text and
returns its top option (the criterion KEY).  We ask it the SAME questions the rules
answer -- ``harness``, ``window``, ``budget`` and ``review`` -- in
SHADOW (RT-P1-JEV-SHADOW), so the two answers can be compared and Jev never decides.

Every question is a plain dict, so this module never imports ``jev_client`` and stays a
pure function of its inputs (no I/O, no network).  The routing option sets are read from
the real enums (:data:`legs.HARNESSES`, :data:`keeper.schema.WINDOW_CLASSES`) so a new
harness or window class shows up here for free; a criterion maps an option (the key Jev
picks) to a one-line description.

Why the state is built by hand instead of dumping the card: a card carries secrets
(``heldout_check``, ``check_cmd``) and the write set names the tests to game, so on top
of sending only the fields Jev needs this module redacts tokens, paths, URLs and IPs and
hard-clips the text.  The rules' OWN choice is never placed in the state -- Jev must
reach its answer independently, or the comparison proves nothing.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from plangraph.keeper.schema import WINDOW_CLASSES

from .legs import HARNESSES

__all__ = [
    "ROUTING_KINDS", "REVIEW_LEVELS", "BUDGET_OPTIONS",
    "MAX_STATE_CHARS", "redact", "routing_state", "routing_questions",
    "rule_answers", "validate_questions",
]

#: The routing decision kinds Jev shadows, == the decision kind names.
ROUTING_KINDS = ("harness", "window", "budget", "review")
#: The two review levels ``routing._review`` can return (twin of that function's
#: ``("risky", 2) if tags else ("standard", 1)``).
REVIEW_LEVELS = ("standard", "risky")
#: Attempt budget as the strings Jev compares: the rule default is
#: ``str(routing.ATTEMPT_BUDGET)`` == "3".
BUDGET_OPTIONS = ("1", "2", "3", "4", "5")
#: Hard cap on the routing state handed to Jev (far below its 64k-token limit).
MAX_STATE_CHARS = 6000

_REDACTED = "[redacted]"

# --------------------------------------------------------------------- redaction
_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")
# sk-/ghp_/xox prefixed tokens, and an HTTP Bearer credential.
_PREFIX_TOKEN_RE = re.compile(r"(?:sk-|ghp_|xox|Bearer\s+)\S*")
# Any long opaque run (base64/hex-ish) is presumed a secret.
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
# Absolute unix path (``/`` + word + ``/`` ...), a drive path (``C:\``/``C:/``) or a
# home path (``~`` then ``/`` or ``\``).  The ``~`` form is written as a class so this
# source file holds no literal home-directory token.
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s]*"
    r"|~[/\\][^\s]*"
    r"|(?<![\w/])/[\w.\-]+/[\w.\-]*"
)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def redact(text: Any, cap: int) -> str:
    """Collapse whitespace, redact secrets/paths/URLs/IPs, clip to ``cap`` characters.

    Never raises: ``None`` -> ``""`` and any other non-str is ``str()``-ed first (a
    failed ``str()`` also yields ``""``).
    """
    try:
        if text is None:
            return ""
        value = text if isinstance(text, str) else str(text)
        value = _WS_RE.sub(" ", value).strip()
        value = _URL_RE.sub(_REDACTED, value)
        value = _PREFIX_TOKEN_RE.sub(_REDACTED, value)
        value = _LONG_RUN_RE.sub(_REDACTED, value)
        value = _PATH_RE.sub(_REDACTED, value)
        value = _IPV4_RE.sub(_REDACTED, value)
        return value[:cap]
    except Exception:  # pragma: no cover - defensive: redaction must not raise
        return ""


# ------------------------------------------------------------------ option copy
_HARNESS_DESCRIPTIONS = {
    "qodercli": "qoder CLI harness, free tier while credits remain",
    "opencode": "opencode harness over free and subscription providers",
    "agy": "Antigravity harness, subscription quota",
}
_WINDOW_DESCRIPTIONS = {
    "128k": "128k token context window class",
    "256k": "256k token context window class",
    "1m": "1m token context window class",
}
_BUDGET_DESCRIPTIONS = {n: f"{n} attempts on one tier before promotion or parking"
                        for n in BUDGET_OPTIONS}
_REVIEW_DESCRIPTIONS = {
    "standard": "one cross-family reviewer, no risk tags",
    "risky": "two reviewers, card carries risk tags",
}


# ------------------------------------------------------------------ state builders
def routing_state(card: Mapping, route: Any) -> str:
    """Compact, non-secret routing state for Jev, <= :data:`MAX_STATE_CHARS` characters.

    Built only from the card's key/title/outcome, ``estimate_tokens``, ``risk_tags``,
    its stored ``window_class``, the number of subtasks (when a list) and the count of
    ``write_set`` entries, plus the rules' eligible legs and reasons.  Never the rules'
    own choice, and never ``heldout_check``, ``check_cmd``, the write set or any other
    card field.
    """
    card = card if isinstance(card, Mapping) else {}
    tags = card.get("risk_tags")
    tags = tags if isinstance(tags, (list, tuple)) else []
    lines = [
        f"card: {redact(card.get('key'), 200)}",
        f"title: {redact(card.get('title'), 400)}",
        f"outcome: {redact(card.get('outcome'), 400)}",
        f"estimate_tokens: {card.get('estimate_tokens')}",
        f"risk_tags: {', '.join(redact(t, 50) for t in tags)}",
        f"window_class: {card.get('window_class')}",
    ]
    subtasks = card.get("subtasks")
    if isinstance(subtasks, (list, tuple)):
        lines.append(f"subtasks: {len(subtasks)}")
    write_set = card.get("write_set")
    if isinstance(write_set, (list, tuple)):
        lines.append(f"write files: {len(write_set)}")
    legs = getattr(route, "legs", ()) or ()
    for leg in list(legs)[:12]:
        lines.append("leg: %s %s %s %s %s %s %s" % (
            redact(getattr(leg, "id", ""), 100),
            getattr(leg, "tier", ""),
            getattr(leg, "harness", ""),
            redact(getattr(leg, "family", ""), 50),
            "|".join(getattr(leg, "windows", ()) or ()),
            getattr(leg, "max_request_tokens", ""),
            getattr(leg, "trains_on_data", ""),
        ))
    reasons = getattr(route, "reasons", ()) or ()
    for reason in list(reasons)[:8]:
        lines.append(f"reason: {redact(reason, 200)}")
    return "\n".join(lines)[:MAX_STATE_CHARS]


def routing_questions(card: Mapping | None = None) -> dict[str, dict]:
    """The four routing ``choice`` questions, keyed by :data:`ROUTING_KINDS`.

    ``card`` is accepted for call-site symmetry; the questions are card-independent.
    """
    return {
        "harness": {
            "type": "choice",
            "instructions": "Which harness should run this card first?",
            "criteria": {opt: _HARNESS_DESCRIPTIONS[opt] for opt in HARNESSES},
        },
        "window": {
            "type": "choice",
            "instructions": "Which context window class does this card need?",
            "criteria": {opt: _WINDOW_DESCRIPTIONS[opt] for opt in WINDOW_CLASSES},
        },
        "budget": {
            "type": "choice",
            "instructions": "How many attempts on one tier should this card get before"
                            " it is promoted or parked?",
            "criteria": {opt: _BUDGET_DESCRIPTIONS[opt] for opt in BUDGET_OPTIONS},
        },
        "review": {
            "type": "choice",
            "instructions": "What review level does this card need: standard (one"
                            " reviewer) or risky (two reviewers)?",
            "criteria": {opt: _REVIEW_DESCRIPTIONS[opt] for opt in REVIEW_LEVELS},
        },
    }


def rule_answers(route: Any) -> dict[str, str]:
    """The rules' answers for ``route`` in Jev's string vocabulary (all four kinds)."""
    return {
        "harness": route.harness,
        "window": route.window_class,
        "budget": str(route.attempt_budget),
        "review": route.review_level,
    }


# ------------------------------------------------------------------ validation
def validate_questions(questions: Any) -> list[str]:
    """Problems that would make ``questions`` unusable for Jev (empty list == valid).

    Every value must be a ``choice`` question with non-empty string ``instructions``
    (<= 500 chars) and a non-empty ``criteria`` mapping of at most 255 non-empty string
    option names (<= 100 chars each) to string descriptions (<= 255 chars).
    """
    problems: list[str] = []
    if not isinstance(questions, dict):
        return ["questions must be a mapping"]
    if not questions:
        problems.append("questions must not be empty")
    for key, question in questions.items():
        where = repr(key)
        if not isinstance(question, dict):
            problems.append(f"{where}: question must be a dict")
            continue
        if question.get("type") != "choice":
            problems.append(f"{where}: type must be 'choice'")
        instructions = question.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            problems.append(f"{where}: instructions must be a non-empty string")
        elif len(instructions) > 500:
            problems.append(f"{where}: instructions must be at most 500 characters")
        criteria = question.get("criteria")
        if not isinstance(criteria, dict):
            problems.append(f"{where}: criteria must be a dict")
            continue
        if not criteria:
            problems.append(f"{where}: criteria must not be empty")
        if len(criteria) > 255:
            problems.append(f"{where}: criteria must have at most 255 options")
        for option, description in criteria.items():
            if not isinstance(option, str) or not option:
                problems.append(f"{where}: option names must be non-empty strings")
            elif len(option) > 100:
                problems.append(f"{where}: option {option[:20]!r}... exceeds 100 characters")
            if not isinstance(description, str):
                problems.append(f"{where}: description for {option!r} must be a string")
            elif len(description) > 255:
                problems.append(f"{where}: description for {option!r} exceeds 255 characters")
    return problems
