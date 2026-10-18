"""Pure parser: proto-JSON Temporal history -> ``RunProjection`` (docs/projector.md)."""
from __future__ import annotations

import base64
import json
from typing import Any, Optional

from plangraph.projector.models import (
    ATTEMPT_ACTIVITY_TYPES, EXIT_CLASSES, KNOWN_ACTIVITY_TYPES, AttemptRec, RunProjection,
    ShapeIssue, StateRec)

__all__ = ["project_history", "decode_payload"]

_PREFIX = "EVENT_TYPE_"
_TIMEOUT_CLASS = {"TIMEOUT_TYPE_HEARTBEAT": "lease_lost"}
_WAITING = ("usage_limit", "outage")
_CLOSING = ("ACTIVITY_TASK_COMPLETED", "ACTIVITY_TASK_FAILED", "ACTIVITY_TASK_TIMED_OUT",
            "ACTIVITY_TASK_CANCELED")
_STATUS = {"WORKFLOW_EXECUTION_COMPLETED": "completed", "WORKFLOW_EXECUTION_FAILED": "failed",
           "WORKFLOW_EXECUTION_TIMED_OUT": "timed_out", "WORKFLOW_EXECUTION_CANCELED": "canceled",
           "WORKFLOW_EXECUTION_TERMINATED": "terminated",
           "WORKFLOW_EXECUTION_CONTINUED_AS_NEW": "continued_as_new"}


def _b64(value: Any) -> Optional[bytes]:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        return None


def decode_payload(payload: Any) -> Any:
    """Decode a proto-JSON ``json/plain`` payload; anything else (or garbage) -> None."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("metadata")
    enc = meta.get("encoding") if isinstance(meta, dict) else None
    if _b64(enc) != b"json/plain":
        return None
    raw = _b64(payload.get("data"))
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _first_payload(container: Any) -> Any:
    payloads = container.get("payloads") if isinstance(container, dict) else None
    if isinstance(payloads, list) and payloads:
        return decode_payload(payloads[0])
    return None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _num(value: Any, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return kind(value)


def _plan_key(started: dict, workflow_id: str) -> Optional[str]:
    fields = _dict(_dict(started.get("memo")).get("fields"))
    key = decode_payload(fields.get("plan_key"))
    if isinstance(key, str) and key:
        return key
    for prefix in ("card:", "card/"):
        if workflow_id.startswith(prefix) and len(workflow_id) > len(prefix):
            return workflow_id[len(prefix):]
    return None


#: Workflow type names of this contract (the rust worker records Card, the python one CardWorkflow).
_CARD_TYPES = ("Card", "CardWorkflow")


def _is_card(started: dict) -> bool:
    """True for a Card workflow of this contract: workflow type Card or a plan_key memo."""
    if _dict(started.get("workflowType")).get("name") in _CARD_TYPES:
        return True
    return decode_payload(_dict(_dict(started.get("memo")).get("fields")).get("plan_key")) is not None


def _events(history: Any) -> list[tuple[int, str, dict]]:
    events = history.get("events") if isinstance(history, dict) else None
    if not isinstance(events, list) or not events:
        raise ValueError("history has no events")
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        eid, etype = _int(ev.get("eventId")), ev.get("eventType")
        if eid is None or not isinstance(etype, str):
            continue
        out.append((eid, etype[len(_PREFIX):] if etype.startswith(_PREFIX) else etype, ev))
    out.sort(key=lambda t: t[0])
    if not out or out[0][1] != "WORKFLOW_EXECUTION_STARTED":
        raise ValueError("first history event is not WorkflowExecutionStarted")
    return out


def _attrs(ev: dict, name: str) -> dict:
    head, *rest = name.lower().split("_")
    return _dict(ev.get(head + "".join(p.capitalize() for p in rest) + "EventAttributes"))


def _exit_class(name: str, attrs: dict, result: dict) -> str:
    if name == "ACTIVITY_TASK_CANCELED":
        return "cancelled"
    if name == "ACTIVITY_TASK_TIMED_OUT":
        ttype = _dict(_dict(attrs.get("failure")).get("timeoutFailureInfo")).get("timeoutType")
        return _TIMEOUT_CLASS.get(ttype, "timeout")
    if name == "ACTIVITY_TASK_FAILED":
        ftype = _dict(_dict(attrs.get("failure")).get("applicationFailureInfo")).get("type")
        return ftype if ftype in EXIT_CLASSES else "worker_crash"
    exit_class = result.get("exit_class", "ok")
    return exit_class if exit_class in EXIT_CLASSES else "worker_crash"


def project_history(history: dict, *, workflow_id: str, run_id: str) -> RunProjection:
    events = _events(history)
    started = _attrs(events[0][2], events[0][1])
    plan_key = _plan_key(started, workflow_id)
    card = _is_card(started)       # an unrelated workflow in the namespace never raises shape alarms

    acts: dict[int, dict] = {}
    open_acts: set[int] = set()
    attempts: list[AttemptRec] = []
    states: list[StateRec] = []
    issues: list[ShapeIssue] = []
    status, outcome, next_run_id = "running", None, None

    def emit(event_id: int, state: str, reason: Optional[str]) -> None:
        if plan_key is None or (states and states[-1].state == state):
            return
        states.append(StateRec(workflow_id, run_id, event_id, plan_key, state, reason))

    for eid, name, ev in events:
        attrs = _attrs(ev, name)
        if name == "ACTIVITY_TASK_SCHEDULED":
            atype = _dict(attrs.get("activityType")).get("name")
            if card and atype not in KNOWN_ACTIVITY_TYPES:
                issues.append(ShapeIssue(workflow_id, run_id, str(attrs.get("activityId", "")),
                                         f"unknown activity type {atype!r}"))
            if atype in ATTEMPT_ACTIVITY_TYPES:
                acts[eid] = {"activity_id": str(attrs.get("activityId", "")), "attempt": 1,
                             "started_at": None}
                open_acts.add(eid)
                emit(eid, "running", "attempt scheduled")
        elif name == "ACTIVITY_TASK_STARTED":
            act = acts.get(_int(attrs.get("scheduledEventId")))
            if act is not None:
                act["attempt"] = _int(attrs.get("attempt")) or 1
                act["started_at"] = _str(ev.get("eventTime"))
        elif name in _CLOSING:
            sched = _int(attrs.get("scheduledEventId"))
            if sched not in open_acts:
                continue
            open_acts.discard(sched)
            act = acts[sched]
            raw = _first_payload(attrs.get("result"))
            result = _dict(raw)
            if name == "ACTIVITY_TASK_COMPLETED":
                has_payload = bool(_dict(attrs.get("result")).get("payloads"))
                if has_payload and not isinstance(raw, dict):
                    problem = "unparsable attempt result"
                elif not has_payload:
                    # Only a void result is "missing"; a JSON object without exit_class keeps the
                    # documented default (``ok``, fixture 13_second_attempt_started).
                    problem = "attempt result lacks exit_class"
                else:
                    problem = None
                if problem and card:
                    issues.append(ShapeIssue(workflow_id, run_id, act["activity_id"], problem))
            exit_class = _exit_class(name, attrs, result)
            if plan_key is not None:
                attempts.append(AttemptRec(
                    workflow_id, run_id, act["activity_id"], act["attempt"], sched, eid, plan_key,
                    exit_class, _str(result.get("harness")), _str(result.get("model")),
                    _str(result.get("combo")), _str(result.get("class")),
                    _str(result.get("started_at")) or act["started_at"],
                    _str(result.get("ended_at")) or _str(ev.get("eventTime")),
                    _num(result.get("tokens_in"), int), _num(result.get("tokens_out"), int),
                    _num(result.get("cost"), float)))
            if exit_class == "needs_input":
                emit(eid, "needs_input", "exit_class=needs_input")
            elif exit_class in _WAITING:
                emit(eid, "waiting", f"exit_class={exit_class}")
        elif name == "TIMER_STARTED":
            if not open_acts and not (states and states[-1].state == "needs_input"):
                emit(eid, "waiting", "timer started")
        elif name in _STATUS:
            status = _STATUS[name]
            if name == "WORKFLOW_EXECUTION_COMPLETED":
                out = _dict(_first_payload(attrs.get("result"))).get("outcome")
                outcome = out if isinstance(out, str) else None
                if outcome in ("parked", "failed"):
                    emit(eid, outcome, f"workflow outcome {outcome}")
            elif name in ("WORKFLOW_EXECUTION_FAILED", "WORKFLOW_EXECUTION_TIMED_OUT"):
                emit(eid, "failed", f"workflow {status}")
            elif name in ("WORKFLOW_EXECUTION_CANCELED", "WORKFLOW_EXECUTION_TERMINATED"):
                emit(eid, "parked", f"workflow {status}")
            else:
                next_run_id = _str(attrs.get("newExecutionRunId")) or None

    attempts.sort(key=lambda a: a.scheduled_event_id)
    return RunProjection(workflow_id, run_id, plan_key, status, _str(events[0][2].get("eventTime")),
                         events[-1][0], tuple(attempts), tuple(states), outcome, next_run_id,
                         tuple(issues))
