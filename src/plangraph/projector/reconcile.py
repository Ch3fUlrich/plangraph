"""Nightly reconciler: open Temporal workflows vs keeper states (docs/projector.md).

Pure read of Temporal and of the keeper tables; the only writes are rows of
``projector_alarms`` (plain SQL inside the store's write transaction, like ``apply``).
Every pass recomputes the full set of alarm conditions, raises the ones not yet open
(dedupe on ``(kind, workflow_id, key)`` while open) and clears open ones whose condition
is gone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .history import project_history

__all__ = ["ReconcileReport", "reconcile", "open_alarms"]

GLOBAL_WATERMARK = "*"
_LIVE_STATES = ("running", "waiting", "needs_input")


@dataclass
class ReconcileReport:
    raised: list = field(default_factory=list)       # alarm dicts opened by this pass
    cleared: list = field(default_factory=list)      # alarm dicts closed by this pass
    open_alarms: list = field(default_factory=list)  # every alarm open after this pass
    counts: dict = field(default_factory=dict)       # open alarms per kind

    def as_dict(self) -> dict:
        return {"raised": self.raised, "cleared": self.cleared,
                "open_alarms": self.open_alarms, "counts": self.counts}


def open_alarms(store) -> list:
    rows = store.conn.execute(
        "SELECT id, kind, workflow_id, key, detail, raised_at FROM projector_alarms"
        " WHERE cleared_at IS NULL ORDER BY id").fetchall()
    return [{"id": r[0], "kind": r[1], "workflow_id": r[2], "key": r[3], "detail": r[4],
             "raised_at": r[5]} for r in rows]


def _age_s(store, stamp, now_ts):
    moment = store._parse_iso(stamp)
    return None if moment is None else now_ts - moment.timestamp()


def _caught_up(wm_row, proj) -> bool:
    """The projector applied this run up to its last event."""
    if wm_row is None:
        return False
    run_id, event_id = wm_row
    return run_id == proj.run_id and int(event_id) >= proj.last_event_id


def _wanted(store, source, now_ts, grace_s, stale_s) -> dict:
    """``{(kind, workflow_id, key): detail}`` for every condition that holds now."""
    conn = store.conn
    wanted: dict = {}
    open_keys = set()
    refs = source.list_workflows(open_only=True)
    for ref in refs:
        proj = project_history(source.fetch_history(ref.workflow_id, ref.run_id),
                               workflow_id=ref.workflow_id, run_id=ref.run_id)
        key = proj.plan_key
        if key is None:
            wanted[("unmapped_workflow", ref.workflow_id, "")] = \
                f"open workflow {ref.workflow_id} has no plan key"
            continue
        open_keys.add(key)
        row = conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            wanted[("missing_item", ref.workflow_id, key)] = \
                f"open workflow {ref.workflow_id} names {key}, which is not a keeper item"
            continue
        expected = proj.states[-1].state if proj.states else None
        if expected is None or expected == row[0]:
            continue
        wm = conn.execute("SELECT run_id, event_id FROM projector_watermark WHERE workflow_id=?",
                          (ref.workflow_id,)).fetchone()
        if _caught_up(wm, proj):
            wanted[("state_drift", ref.workflow_id, key)] = \
                f"workflow {ref.workflow_id} projects {expected}, keeper has {row[0]}"
    for key, state, updated_at in conn.execute(
            "SELECT key, state, updated_at FROM items WHERE state IN (?,?,?)", _LIVE_STATES):
        if key in open_keys:
            continue
        age = _age_s(store, updated_at, now_ts)
        if age is not None and age > grace_s:
            wanted[("orphan_running", "", key)] = \
                f"{key} is {state} for {int(age)}s with no open workflow"
    if refs:
        row = conn.execute("SELECT updated_at FROM projector_watermark WHERE workflow_id=?",
                           (GLOBAL_WATERMARK,)).fetchone()
        if row is None:
            wanted[("watermark_stale", "", "")] = "no global watermark and workflows are open"
        else:
            age = _age_s(store, row[0], now_ts)
            if age is None or age > stale_s:
                shown = "unknown" if age is None else f"{int(age)}s"
                wanted[("watermark_stale", "", "")] = \
                    f"global watermark age {shown} (limit {stale_s}s) with workflows open"
    return wanted


def reconcile(store, source, projector=None, *, now=time.time, grace_s=900,
              stale_s=1800) -> ReconcileReport:
    if projector is not None and now is time.time:
        now = projector.now
    now_ts = float(now())
    wanted = _wanted(store, source, now_ts, grace_s, stale_s)
    report = ReconcileReport()
    stamp = store._iso(now_ts)
    conn = store.conn
    with store._write():
        present = {(r["kind"], r["workflow_id"], r["key"]): r for r in open_alarms(store)}
        for ident, row in present.items():
            if ident not in wanted:
                conn.execute("UPDATE projector_alarms SET cleared_at=? WHERE id=?",
                             (stamp, row["id"]))
                report.cleared.append({k: row[k] for k in ("kind", "workflow_id", "key", "detail")})
        for ident, detail in sorted(wanted.items()):
            if ident in present:
                continue
            next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms"
                                   ).fetchone()[0]
            conn.execute(
                "INSERT INTO projector_alarms(id, kind, workflow_id, key, detail, raised_at)"
                " VALUES(?,?,?,?,?,?)", (next_id, *ident, detail, stamp))
            report.raised.append({"kind": ident[0], "workflow_id": ident[1], "key": ident[2],
                                  "detail": detail})
    report.open_alarms = open_alarms(store)
    for row in report.open_alarms:
        report.counts[row["kind"]] = report.counts.get(row["kind"], 0) + 1
    return report
