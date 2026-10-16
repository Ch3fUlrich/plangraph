"""Idempotent apply of projected runs into the keeper (docs/projector.md, "Idempotent apply").

Keeper rows are written only through ``Store.record_attempt`` / ``Store.set_state`` with a
``system`` principal.  The ``projector_*`` ledger and watermark tables are plain SQL on the
store's connection, each write inside the store's own write transaction.  ``record_attempt``
opens its own transaction, so the attempt row and its ledger row cannot share one: a crash
between them is repaired on the next apply (the attempts row carries
``log_ref = temporal:<activity_id>:<attempt>``, which identifies it).
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, fields

from plangraph.keeper.errors import Refused

from .models import SETTABLE_STATES, AttemptRec, RunProjection, StateRec

try:
    from .history import project_history
except ModuleNotFoundError as exc:               # history.py lands separately
    if exc.name != f"{__package__}.history":
        raise
    project_history = None

__all__ = ["Projector", "ApplyResult", "project_source"]

GLOBAL_WATERMARK = "*"
_REASON_CAP = 500


@dataclass
class ApplyResult:
    attempts_written: int = 0
    attempts_existing: int = 0
    states_written: int = 0
    states_skipped_state: int = 0
    states_existing: int = 0           # already in the ledger, or the item is already in that state
    states_stale: int = 0
    missing_item: int = 0
    unmapped: int = 0

    def add(self, other: "ApplyResult") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def as_dict(self) -> dict:
        return asdict(self)


class Projector:
    def __init__(self, store, principal, *, now=time.time):
        self.store = store
        self.principal = principal
        self.now = now

    # ------------------------------------------------------------ helpers
    @property
    def conn(self):
        return self.store.conn

    def _stamp(self) -> str:
        return self.store._iso(self.now())

    def _is_applied(self, wf, run, activity_id, attempt, kind) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM projector_applied WHERE workflow_id=? AND run_id=? AND activity_id=?"
            " AND attempt=? AND kind=?", (wf, run, activity_id, attempt, kind)).fetchone() is not None

    def _mark(self, wf, run, activity_id, attempt, kind, key, n) -> None:
        with self.store._write():
            self.conn.execute(
                "INSERT INTO projector_applied(workflow_id, run_id, activity_id, attempt, kind,"
                " key, n, applied_at) VALUES(?,?,?,?,?,?,?,?)",
                (wf, run, activity_id, attempt, kind, key, n, self._stamp()))

    def _item_state(self, key):
        row = self.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def _watermark(self, workflow_id):
        row = self.conn.execute(
            "SELECT run_ord, event_id FROM projector_watermark WHERE workflow_id=?",
            (workflow_id,)).fetchone()
        return (-1, -1) if row is None else (int(row[0]), int(row[1]))

    # ------------------------------------------------------------ attempts
    def _existing_attempt(self, rec: AttemptRec, log_ref):
        row = self.conn.execute(
            "SELECT n FROM attempts WHERE key=? AND workflow_id=? AND run_id=? AND log_ref=?",
            (rec.plan_key, rec.workflow_id, rec.run_id, log_ref)).fetchone()
        return None if row is None else int(row[0])

    def _apply_attempt(self, rec: AttemptRec, result: ApplyResult) -> bool:
        wf, run, act, att = rec.workflow_id, rec.run_id, rec.activity_id, rec.attempt
        if self._is_applied(wf, run, act, att, "attempt"):
            result.attempts_existing += 1
            return True
        if self._item_state(rec.plan_key) is None:
            result.missing_item += 1
            return False
        log_ref = f"temporal:{act}:{att}"
        n = self._existing_attempt(rec, log_ref)             # crash window: row written, ledger not
        if n is not None:
            self._mark(wf, run, act, att, "attempt", rec.plan_key, n)
            result.attempts_existing += 1
            return True
        n = self.conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM attempts WHERE key=?",
                              (rec.plan_key,)).fetchone()[0]
        body = {"n": n, "harness": rec.harness, "model": rec.model, "combo": rec.combo,
                "class": rec.class_, "started_at": rec.started_at, "ended_at": rec.ended_at,
                "exit_class": rec.exit_class, "tokens_in": rec.tokens_in,
                "tokens_out": rec.tokens_out, "cost": rec.cost, "run_id": run,
                "workflow_id": wf, "log_ref": log_ref}
        try:
            self.store.record_attempt(self.principal, rec.plan_key, body)
        except Refused as exc:
            if exc.code != "conflict" or self._attempt_row_identity(rec.plan_key, n) \
                    != (wf, run, log_ref):
                raise
            self._mark(wf, run, act, att, "attempt", rec.plan_key, n)
            result.attempts_existing += 1
            return True
        self._mark(wf, run, act, att, "attempt", rec.plan_key, n)
        result.attempts_written += 1
        return True

    def _attempt_row_identity(self, key, n):
        row = self.conn.execute(
            "SELECT workflow_id, run_id, log_ref FROM attempts WHERE key=? AND n=?",
            (key, n)).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    # -------------------------------------------------------------- states
    def _apply_state(self, rec: StateRec, run_ord: int, mark, result: ApplyResult) -> bool:
        wf, run, act = rec.workflow_id, rec.run_id, f"ev{rec.event_id}"
        if self._is_applied(wf, run, act, 0, "state"):
            result.states_existing += 1
            return True
        state = self._item_state(rec.plan_key)
        if state is None:
            result.missing_item += 1
            return False
        if (run_ord, rec.event_id) <= mark:
            result.states_stale += 1
        elif state not in SETTABLE_STATES:
            result.states_skipped_state += 1
        else:
            try:
                out = self.store.set_state(self.principal, rec.plan_key, rec.state,
                                           (rec.reason or "")[:_REASON_CAP])
            except Refused as exc:
                if exc.code not in ("bad_state", "locked"):       # the item moved under us
                    raise
                result.states_skipped_state += 1
            else:
                if out["changed"]:
                    result.states_written += 1
                else:
                    result.states_existing += 1
        self._mark(wf, run, act, 0, "state", rec.plan_key, None)
        return True

    # ----------------------------------------------------------- watermark
    def _advance_watermark(self, proj: RunProjection, run_ord: int) -> None:
        event_id = max([proj.last_event_id, *(s.event_id for s in proj.states)])
        if (run_ord, event_id) <= self._watermark(proj.workflow_id):
            return
        self._upsert_watermark(proj.workflow_id, proj.run_id, run_ord, event_id, proj.status,
                               proj.plan_key or "")

    def _upsert_watermark(self, workflow_id, run_id, run_ord, event_id, status, plan_key) -> None:
        with self.store._write():
            self.conn.execute(
                "INSERT INTO projector_watermark(workflow_id, run_id, run_ord, event_id, status,"
                " plan_key, updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(workflow_id) DO UPDATE SET run_id=excluded.run_id,"
                " run_ord=excluded.run_ord, event_id=excluded.event_id, status=excluded.status,"
                " plan_key=excluded.plan_key, updated_at=excluded.updated_at",
                (workflow_id, run_id, run_ord, event_id, status, plan_key, self._stamp()))

    def touch_global_watermark(self) -> None:
        self._upsert_watermark(GLOBAL_WATERMARK, "", 0, 0, "", "")

    # ----------------------------------------------------------------- run
    def apply_run(self, proj: RunProjection, *, run_ord: int) -> ApplyResult:
        result = ApplyResult()
        if proj.plan_key is None:
            result.unmapped = 1
            return result
        complete = True
        for rec in sorted(proj.attempts, key=lambda a: (a.scheduled_event_id, a.activity_id)):
            complete &= self._apply_attempt(rec, result)
        mark = self._watermark(proj.workflow_id)
        for rec in sorted(proj.states, key=lambda s: s.event_id):
            complete &= self._apply_state(rec, run_ord, mark, result)
        if complete:                       # a missing item keeps the run re-appliable
            self._advance_watermark(proj, run_ord)
        return result


def _run_ords(refs) -> dict:
    """``run_ord`` = index of the run among its workflow's runs sorted by (start_time, run_id)."""
    by_workflow: dict = {}
    for ref in refs:
        by_workflow.setdefault(ref.workflow_id, []).append(ref)
    ords = {}
    for runs in by_workflow.values():
        for i, ref in enumerate(sorted(runs, key=lambda r: (r.start_time, r.run_id))):
            ords[(ref.workflow_id, ref.run_id)] = i
    return ords


def project_source(source, projector: Projector, *, open_only: bool = False) -> dict:
    """Project every run of ``source`` into the keeper; the same call is ``rebuild``."""
    if project_history is None:
        raise RuntimeError("plangraph.projector.history is not available")
    refs = source.list_workflows()
    ords = _run_ords(refs)                  # ordinals need every run, even when only open ones apply
    if open_only:
        refs = [ref for ref in refs if ref.status == "RUNNING"]
    total = ApplyResult()
    processed = 0
    for ref in sorted(refs, key=lambda r: (r.start_time, r.workflow_id, r.run_id)):
        proj = project_history(source.fetch_history(ref.workflow_id, ref.run_id),
                               workflow_id=ref.workflow_id, run_id=ref.run_id)
        total.add(projector.apply_run(proj, run_ord=ords[(ref.workflow_id, ref.run_id)]))
        processed += 1
    projector.touch_global_watermark()
    return {**total.as_dict(), "processed": processed}
