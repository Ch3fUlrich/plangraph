"""Design-3 (W2) store verbs: cards, attempts, decisions, questions/answers, budget, states.

Every verb is portable SQL (``?`` placeholders, ``ON CONFLICT`` upserts, ids assigned
as ``MAX(id)+1`` inside the write lock) so it runs unchanged on SQLite and Postgres.
Only ``set_state`` touches the hash chain, with the new ``state_set`` event; the other
tables are plain rows (``attempts``, ``decisions`` and ``answers`` are append-only by
trigger).

* ``put_card`` / ``get_card``: the 1:1 card of an item (full replace).
* ``record_attempt``: one finished attempt (append-only; ``exit_class`` is required).
* ``add_decision``: an operator decision or judge ruling (router only).
* ``ask_question`` / ``answer_question``: a structured question with an optional default
  and deadline.  A risk-tagged question never has a default.  ``source='question_timeout'``
  applies the default once the deadline has passed.  RT-P1-EXEC-OWNERSHIP: an ask
  carrying ``(workflow_id, run_id, attempt)`` is checked against the card's recorded
  exec owner exactly like a run-carrying ``set_state`` move; a worker/spawner that
  omits them on an owned card is refused.
* ``set_budget``: absolute tokens/cost/meter for one provider and day.
* ``set_state``: move an item among the non-claim states (open, ready, running,
  needs_input, waiting, failed, parked); claim, review, done and cancel keep their verbs.
  RT-P1-EXEC-OWNERSHIP: a move that carries ``run=(workflow_id, run_id, attempt)`` is
   exec-owned -- it must stay forward per ``TRANSITIONS``, and the recorded
  owner refuses a stale run or an attempt that would go backwards.  A hand-back
  is EXPLICIT: ``set_state(..., hand_back=True)`` that changes the state and
  lands on ``open``/``ready`` frees the exec owner for the next run.  A bare
  run-less move (the projector/planner write path) never clears the owner, so a
  run-less write cannot free a live run's card (RT-P1 V1-B); on an OWNED card it
  must also stay forward -- a bare back move is ``bad_transition`` (RT-P1 sq5
  fix 3).  Owners are recorded only by a worker principal and are bound to it
  (sq5 fix 1), and every clear keeps a retired-run fence (sq5 fix 2).
"""
from __future__ import annotations

import json
import math
import re
from datetime import date

from plangraph.card_paths import normalise_path, path_problem

from .credential_guard import guard
from .errors import Refused
from .store_exec import ExecMixin
STATE_EVENT_TYPES = frozenset({"item_created", "claimed", "released", "lease_expired", "submitted", "changes_requested", "review_withdrawn", "item_reopened", "review_passed", "closed_on_decision", "answered", "legacy_state_set", "cancelled", "state_set"})
from .schema import EXIT_CLASSES, RISK_TAGS, WINDOW_CLASSES

__all__ = ["SETTABLE_STATES", "TRANSITIONS", "NO_DEFAULT_TAGS", "ExecMixin",
           "Refused", "guard", "normalise_path", "path_problem", "json", "math",
           "re", "date", "EXIT_CLASSES", "RISK_TAGS", "WINDOW_CLASSES",
           "QUESTION_TAGS", "STATE_EVENT_TYPES"]

#: States ``set_state`` moves between (none of them carries a claim, lease or epoch).
SETTABLE_STATES = ("open", "ready", "running", "needs_input", "waiting", "failed",
                   "parked")

#: RT-P1-EXEC-OWNERSHIP: the hand-back states.  An EXPLICIT ``set_state`` move
#: (``hand_back=True``) that lands on one of these and changes the state is a
#: router/operator hand-back: it frees the exec owner so the next run can claim
#: the card (same transaction as the state UPDATE).  A bare run-less move -- the
#: projector/planner write path -- is never a hand-back and keeps the owner; so
#: do run-carrying moves, no-op moves and moves to any other state.
_HAND_BACK_STATES = frozenset({"open", "ready"})

#: RT-P1-EXEC-OWNERSHIP: direct successors of each settable state (same-state always
#: allowed; reachability is the transitive closure).  A set-state move that carries a
#: ``run`` may only follow these edges forward; the back-moves (``failed``/``parked``
#: back to ``open``/``ready``) are refused. A run-carrying move never clears
#: ownership; an explicit hand-back (``hand_back=True``) to ``open``/``ready``
#: does (see ``_HAND_BACK_STATES``).
#: Moves without a ``run``
#: keep their historical behaviour. Worker parks explicitly consult this table
#: before releasing a claim; claimed is their lease-bound entry exception.
#: A claim release does not exempt other states from forward ordering.
TRANSITIONS: dict[str, frozenset[str]] = {
    "open":        frozenset({"open", "ready"}),
    "ready":       frozenset({"ready", "running"}),
    "running":     frozenset({"running", "needs_input", "waiting"}),
    "needs_input": frozenset({"needs_input", "waiting", "parked", "failed"}),
    "waiting":     frozenset({"waiting", "needs_input", "parked", "failed"}),
    "failed":      frozenset({"failed", "parked"}),
    "parked":      frozenset({"parked", "failed"}),
}

#: Question tags: the card tags plus the design's "public release".
QUESTION_TAGS = RISK_TAGS + ("public-release",)
#: design 4.8: a question carrying one of these has no default and is never timed out.
NO_DEFAULT_TAGS = frozenset({"auth", "secrets", "money", "live-step", "public-release"})

_CARD_LISTS = ("read_set", "write_set", "exports", "forbid_paths", "smoke_checks", "check_reads")
_CARD_TEXT = ("check_cmd", "heldout_check")
_CARD_KEYS = frozenset(("outcome", "window_class", "risk_tags", "estimate_tokens",
                        "check_kind", *_CARD_LISTS, *_CARD_TEXT))
_ATTEMPT_KEYS = frozenset((
    "n", "harness", "model", "combo", "class", "started_at", "ended_at", "exit_class",
    "tokens_in", "tokens_out", "cost", "run_id", "workflow_id", "log_ref"))
_ATTEMPT_TEXT = ("harness", "model", "combo", "class", "run_id", "workflow_id", "log_ref")
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_PROVIDER_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_TEXT_CAP = 2000
_LIST_CAP = 500
_ITEM_CAP = 500


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load_list(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or [])
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _text(name, value, required=False, cap=_TEXT_CAP, key=None):
    if value is None:
        if required:
            raise Refused("validation", f"{name} is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise Refused("validation", f"{name} must be a non-empty string"
                      if required else f"{name} must be a string or null")
    if len(value) > cap:
        raise Refused("validation", f"{name} exceeds {cap} characters")
    guard(name, value, key)
    return value


def _str_list(name, value, allowed=None, key=None):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > _LIST_CAP:
        raise Refused("validation", f"{name} must be a list of at most {_LIST_CAP} strings")
    out = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > _ITEM_CAP:
            raise Refused("validation", f"{name} entries must be non-empty strings"
                          f" of at most {_ITEM_CAP} characters")
        if allowed is not None and entry not in allowed:
            raise Refused("validation", f"{name} entry {entry!r} is not one of"
                          f" {list(allowed)}")
        guard(name, entry, key)
        if entry not in out:
            out.append(entry)
    return out


def _count(name, value, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refused("validation", f"{name} must be a non-negative integer"
                      + (" or null" if nullable else ""))
    return value


def _number(name, value, nullable=True):
    if value is None and nullable:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise Refused("validation", f"{name} must be a non-negative number"
                      + (" or null" if nullable else ""))
    return float(value)


def _exec_parts(key, run):
    """Validate a ``run=(workflow_id, run_id, attempt)``; return the three parts.

    Every part must be present and well-formed whether or not the item already
    has a recorded owner: recording a malformed owner would poison the card.
    """
    if not isinstance(run, (tuple, list)) or len(run) != 3:
        raise Refused("validation",
                      "run must be a (workflow_id, run_id, attempt) tuple")
    wf, run_id, attempt = run
    wf = _text("workflow_id", wf, required=True, cap=200, key=key)
    run_id = _text("run_id", run_id, required=True, cap=200, key=key)
    if (isinstance(attempt, bool) or not isinstance(attempt, int)
            or not 1 <= attempt <= 2**31 - 1):
        raise Refused("validation", "attempt must be an integer in [1, 2147483647]")
    return wf, run_id, attempt


def _forward(was, state):
    """True when *state* is *was* or forward-reachable from it per TRANSITIONS."""
    todo, seen = [was], set()
    while todo:
        here = todo.pop()
        if here == state:
            return True
        for nxt in TRANSITIONS.get(here, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return False

