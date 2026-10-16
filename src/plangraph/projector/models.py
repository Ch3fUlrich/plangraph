"""Records the pure history parser produces and ``apply`` writes (docs/projector.md)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from plangraph.keeper.schema import EXIT_CLASSES
from plangraph.keeper.store_cards import SETTABLE_STATES

__all__ = ["AttemptRec", "StateRec", "ShapeIssue", "RunProjection", "ATTEMPT_ACTIVITY_TYPES",
           "KNOWN_ACTIVITY_TYPES",
           "EXIT_CLASSES", "SETTABLE_STATES"]

#: Activity type names that count as one workflow-level attempt.
ATTEMPT_ACTIVITY_TYPES = frozenset({"attempt"})

#: Every activity type the Card workflow may schedule; anything else is an ``unknown_shape``.
KNOWN_ACTIVITY_TYPES = ATTEMPT_ACTIVITY_TYPES | frozenset(
    f"CardActivities::{n}" for n in
    ("tick", "probe", "merge", "report", "park", "complete", "alarm", "janitor"))


@dataclass(frozen=True)
class ShapeIssue:
    """One history shape the contract does not cover; apply raises an ``unknown_shape`` alarm."""
    workflow_id: str
    run_id: str
    activity_id: str
    detail: str


@dataclass(frozen=True)
class AttemptRec:
    workflow_id: str
    run_id: str
    activity_id: str
    attempt: int
    scheduled_event_id: int
    closed_event_id: int
    plan_key: str
    exit_class: str
    harness: Optional[str]
    model: Optional[str]
    combo: Optional[str]
    class_: Optional[str]
    started_at: Optional[str]
    ended_at: Optional[str]
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    cost: Optional[float]


@dataclass(frozen=True)
class StateRec:
    workflow_id: str
    run_id: str
    event_id: int
    plan_key: str
    state: str
    reason: Optional[str]


@dataclass(frozen=True)
class RunProjection:
    workflow_id: str
    run_id: str
    plan_key: Optional[str]
    status: str
    start_time: Optional[str]
    last_event_id: int
    attempts: tuple[AttemptRec, ...]
    states: tuple[StateRec, ...]
    outcome: Optional[str] = None
    next_run_id: Optional[str] = None
    shape_issues: tuple[ShapeIssue, ...] = ()
