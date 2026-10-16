"""Where run histories come from: the ``HistorySource`` protocol and the file-backed source."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = ["WorkflowRef", "HistorySource", "FileSource"]

_PREFIX = "EVENT_TYPE_WORKFLOW_EXECUTION_"
_END_STATUS = frozenset({"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT",
                         "CONTINUED_AS_NEW"})


@dataclass(frozen=True)
class WorkflowRef:
    workflow_id: str
    run_id: str
    start_time: str
    status: str                      # Temporal status name, upper case ("RUNNING", "COMPLETED", ...)


class HistorySource(Protocol):
    def list_workflows(self, *, open_only: bool = False) -> list[WorkflowRef]:
        """One ref per run, sorted by (start_time, workflow_id, run_id)."""

    def fetch_history(self, workflow_id: str, run_id: str) -> dict:
        """The proto-JSON history ``{"events": [...]}`` of one run."""


def _sort_key(ref: WorkflowRef):
    return (ref.start_time, ref.workflow_id, ref.run_id)


def _status(events: list) -> str:
    last = str(events[-1].get("eventType", "")) if events else ""
    if last.startswith(_PREFIX) and last[len(_PREFIX):] in _END_STATUS:
        return last[len(_PREFIX):]
    return "RUNNING"


class FileSource:
    """``*.json`` files of ``{"workflow_id", "run_id", "history"}`` (tests, offline rebuild)."""

    def __init__(self, directory):
        self.dir = Path(directory)

    def _load(self):
        for path in sorted(self.dir.glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            yield str(doc["workflow_id"]), str(doc["run_id"]), doc["history"]

    def list_workflows(self, *, open_only: bool = False) -> list[WorkflowRef]:
        refs = []
        for workflow_id, run_id, history in self._load():
            events = history.get("events") or []
            start = str(events[0].get("eventTime", "")) if events else ""
            refs.append(WorkflowRef(workflow_id, run_id, start, _status(events)))
        if open_only:
            refs = [ref for ref in refs if ref.status == "RUNNING"]
        return sorted(refs, key=_sort_key)

    def fetch_history(self, workflow_id: str, run_id: str) -> dict:
        for wid, rid, history in self._load():
            if wid == workflow_id and rid == run_id:
                return history
        raise KeyError(f"no history file for {workflow_id}/{run_id}")
