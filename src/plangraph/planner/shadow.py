"""Jev shadow hook (design D18): an audit-only shadow answerer beside the rules router.

``route_with_shadow`` always routes with :func:`plangraph.planner.routing.route_card`
first and returns *that* route unchanged: the shadow answer is an observation for the
audit log, never a decision.  The hook is off unless ``PLANGRAPH_PLANNER_SHADOW`` reads
``1``/``true``/``yes``/``on``; it needs both an ``answerer`` and a log path (argument or
``PLANGRAPH_PLANNER_SHADOW_LOG``).  Each run appends one JSONL ``decision`` row (rules,
shadow answer, shadow error, first-leg agreement) and ``record_outcome`` later appends
the attempt outcome for the same id; ``read_rows`` / ``summarize`` read them back.

Failures are ignored by design: an answerer exception becomes ``shadow_error``, a log
write failure (``OSError``) is swallowed, and nothing about the returned route changes.
Only ``card_key`` and rule/shadow/outcome fields are logged - never card text such as
``heldout_check``, ``check_cmd`` or ``write_set``.  Stdlib only: no network, no store or
schema change.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from .routing import Route, route_card

__all__ = ["ENV_FLAG", "ENV_LOG", "Answerer", "enabled", "route_with_shadow",
           "record_outcome", "read_rows", "summarize"]

#: Master switch: the shadow hook runs only when this reads ``1/true/yes/on``.
ENV_FLAG = "PLANGRAPH_PLANNER_SHADOW"
#: Default audit-log path when ``log_path`` is not passed.
ENV_LOG = "PLANGRAPH_PLANNER_SHADOW_LOG"

#: Values that turn the hook on (case-insensitive); anything else is off.
_ON = frozenset({"1", "true", "yes", "on"})

#: Card fields that must never reach the log (redaction).
_SECRET_FIELDS = frozenset({"heldout_check", "check_cmd", "write_set"})

#: ``answerer(card, rules_route)`` returns a JSON-able mapping (e.g. the legs it would pick).
Answerer = Callable[[Mapping, Route], Mapping]


def _env(env: Mapping | None) -> Mapping:
    return os.environ if env is None else env


def enabled(env: Mapping | None = None) -> bool:
    """True only for ``PLANGRAPH_PLANNER_SHADOW`` in ``1/true/yes/on``; default off."""
    raw = _env(env).get(ENV_FLAG, "")
    return str(raw).strip().lower() in _ON


def _log_path(log_path, env: Mapping | None) -> str | None:
    path = log_path if log_path is not None else _env(env).get(ENV_LOG)
    if path is None:
        return None
    text = str(path).strip()
    return text or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append(path: str, row: Mapping) -> bool:
    """Append one JSON line; log failures are swallowed, never raised."""
    try:
        line = json.dumps(row, default=repr)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, ValueError, TypeError):
        return False
    return True


def _scrub(value):
    """Drop the redacted card fields (recursively) from a shadow answer."""
    if isinstance(value, Mapping):
        return {k: _scrub(v) for k, v in value.items() if k not in _SECRET_FIELDS}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _first_leg_id(answer) -> str | None:
    """First leg id of a shadow answer (list of ids, or of ``{"id": ...}`` maps)."""
    if not isinstance(answer, Mapping):
        return None
    legs = answer.get("legs")
    if not legs or isinstance(legs, (str, bytes)):
        return None
    first = legs[0]
    if isinstance(first, Mapping):
        first = first.get("id")
    return None if first is None else str(first)


def route_with_shadow(card: Mapping, *, answerer: Answerer | None = None,
                      log_path=None, env: Mapping | None = None,
                      **route_kw) -> tuple[Route, str | None]:
    """Route ``card`` with the rules and audit a shadow answer beside it.

    The rules route from ``route_card`` (its exceptions propagate unchanged) is always
    computed first and always returned unmodified; the returned id names the audit row
    (``None`` when the hook is off or no log path resolves).  The answerer sees a deep
    copy of the card and route, and its failure is recorded and ignored.
    """
    rules = route_card(card, **route_kw)
    if not enabled(env) or answerer is None:
        return rules, None
    path = _log_path(log_path, env)
    if path is None:
        return rules, None

    decision_id = uuid.uuid4().hex
    shadow = None
    shadow_error = None
    try:
        shadow = answerer(copy.deepcopy(card), copy.deepcopy(rules))
    except Exception as exc:  # a broken shadow must never break routing
        shadow_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    answer = _scrub(shadow) if shadow is not None else None
    first = _first_leg_id(answer)
    agree = None if first is None else first == rules.legs[0].id
    row = {"v": 1, "event": "decision", "id": decision_id, "ts": _now(),
           "card_key": card.get("key"),
           "rules": {"window_class": rules.window_class,
                     "legs": [leg.id for leg in rules.legs],
                     "review_seats": rules.review_seats,
                     "reasons": list(rules.reasons)},
           "shadow": answer, "shadow_error": shadow_error, "agree": agree}
    _append(path, row)
    return rules, decision_id


def record_outcome(decision_id, outcome: Mapping, *, log_path=None,
                   env: Mapping | None = None) -> bool:
    """Append the attempt ``outcome`` for ``decision_id``; False when it did not land."""
    if decision_id is None or not enabled(env):
        return False
    path = _log_path(log_path, env)
    if path is None:
        return False
    row = {"v": 1, "event": "outcome", "id": decision_id, "ts": _now(),
           "outcome": dict(outcome)}
    return _append(path, row)


def read_rows(log_path) -> list[dict]:
    """Read the JSONL audit log, skipping blank, malformed or non-object lines."""
    rows: list[dict] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def summarize(rows) -> dict:
    """Counts over the audit rows, outcomes joined to decisions by id."""
    decisions = [r for r in rows if isinstance(r, dict) and r.get("event") == "decision"]
    ids = {r.get("id") for r in decisions}
    outcomes = sum(1 for r in rows if isinstance(r, dict)
                   and r.get("event") == "outcome" and r.get("id") in ids)
    agree = sum(1 for r in decisions if r.get("agree") is True)
    disagree = sum(1 for r in decisions if r.get("agree") is False)
    errors = sum(1 for r in decisions if r.get("shadow_error"))
    return {"decisions": len(decisions), "outcomes": outcomes, "agree": agree,
            "disagree": disagree, "shadow_errors": errors}
