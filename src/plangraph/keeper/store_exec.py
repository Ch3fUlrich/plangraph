"""Exec ownership verbs, separated from the card data verbs."""
from __future__ import annotations

import json

from .errors import Refused


class ExecMixin:
    """Transactional exec ownership and retired-run fencing."""

    def _exec_project(self, conn, item, run, principal, now, run_ord=None):
        """Install the watermark-ordered history owner in the state transaction."""
        from .store_cards import _exec_parts, _load_list
        self._require_role(principal, "system")
        wf, run_id, attempt = _exec_parts(item["key"], run)
        same = (wf, run_id) == (item["exec_workflow_id"], item["exec_run_id"])
        retired = _load_list(item["exec_retired_runs"])
        if any((r.get("workflow_id"), r.get("run_id")) == (wf, run_id) for r in retired):
            raise Refused("exec_stale", "projected run has been retired")
        if same and attempt <= item["exec_attempt"]:
            return
        # A worker-recorded owner has no proven history order yet. Never let
        # an unrelated/older history retire it just because its watermark lags.
        holder = item["exec_principal"] if same else (item["delegate"] or item["claimed_by"])
        if not same and item["exec_run_id"] is not None:
            mark = conn.execute(
                "SELECT run_id, run_ord FROM projector_watermark WHERE workflow_id=?",
                (wf,)).fetchone()
            if (wf != item["exec_workflow_id"] or mark is None
                    or mark["run_id"] != item["exec_run_id"] or run_ord is None
                    or run_ord <= mark["run_ord"]):
                raise Refused("exec_stale", "projected run is not proven newer than owner")
            self._exec_clear(conn, item["key"], principal, now)
        conn.execute("UPDATE items SET exec_workflow_id=?, exec_run_id=?, exec_attempt=?,"
                     " exec_principal=?, updated_at=? WHERE key=?",
                     (wf, run_id, attempt, holder, self._iso(now), item["key"]))
        self._append(principal, item["key"], "exec_owner_changed",
                     {"action": "advance" if same else "record", "workflow_id": wf,
                      "run_id": run_id, "attempt": attempt}, now)

    def _exec_clear(self, conn, key, principal, now):
        """Free the exec owner inside the caller's keeper transaction.

        RT-P1 sq5 fix 2 (V1-1/V5-1): the clear RETIRES the run it frees -- the
        ``(workflow_id, run_id, max attempt)`` triple joins the card's
        retired-run fence (``exec_retired_runs``), kept in the same UPDATE that
        drops the owner, so a late call from that run is refused ``exec_stale``
        and cannot reacquire the card as its first writer.  Every clear door
        (reap, release, both review changes, reopen, withdraw and the explicit
        ``set_state`` hand-back) goes through here, so the fence is written for
        all of them at once.
        """
        from .store_cards import _dump, _load_list

        row = conn.execute(
            "SELECT exec_workflow_id, exec_run_id, exec_attempt, exec_retired_runs"
            " FROM items WHERE key=?", (key,)).fetchone()
        if row is None or row["exec_run_id"] is None:
            # Nothing to retire: an unowned card only drops the (already NULL)
            # triple and keeps whatever fence earlier clears left behind.
            conn.execute("UPDATE items SET exec_workflow_id=NULL, exec_run_id=NULL,"
                         " exec_attempt=NULL WHERE key=?", (key,))
            return
        retired = [entry for entry in _load_list(row["exec_retired_runs"])
                   if isinstance(entry, dict)]
        by_run = [(entry.get("workflow_id"), entry.get("run_id"))
                  for entry in retired]
        run_key = (row["exec_workflow_id"], row["exec_run_id"])
        if run_key in by_run:
            entry = retired[by_run.index(run_key)]
            entry["attempt"] = max(entry.get("attempt") or 0,
                                   row["exec_attempt"] or 0)
        else:
            retired.append({"workflow_id": run_key[0], "run_id": run_key[1],
                            "attempt": row["exec_attempt"]})
        conn.execute("UPDATE items SET exec_workflow_id=NULL, exec_run_id=NULL,"
                     " exec_attempt=NULL, exec_principal=NULL, exec_retired_runs=? WHERE key=?",
                     (_dump(retired), key))
        self._append(principal, key, "exec_owner_changed",
                     {"action": "clear", "retired": [retired[by_run.index(run_key)]
                      if run_key in by_run else retired[-1]]}, now)

    def _exec_record(self, conn, item_id, wf, run, attempt, principal=None, now=None):
        """Record the exec owner of *item_id*: the first owner of an unowned card.

        Only ``_exec_check`` calls this, and only while no owner is recorded, so
        an existing owner is never replaced here.

        RT-P1 sq5 fix 1 (V2-1): the owner is stored WITH the principal that
        presented the triple (``exec_principal``), and only a worker principal
        may record one -- a router/l1/system/spawner token planting a triple on
        an unowned card is refused, never recorded.
        """
        from .store_cards import _exec_parts

        if principal is None or getattr(principal, "role", None) != "worker":
            name = getattr(principal, "name", None) or "<none>"
            raise Refused("forbidden",
                          f"{item_id}: only the worker principal may record an"
                          f" exec owner, not {name}")
        wf, run, attempt = _exec_parts(item_id, (wf, run, attempt))
        touch = ", updated_at=?" if now is not None else ""
        values = (wf, run, attempt, principal.name)
        if now is not None:
            values += (self._iso(now),)
        conn.execute("UPDATE items SET exec_workflow_id=?, exec_run_id=?,"
                     f" exec_attempt=?, exec_principal=?{touch} WHERE key=?",
                      (*values, item_id))
        self._append(principal, item_id, "exec_owner_changed",
                     {"action": "record", "workflow_id": wf, "run_id": run,
                      "attempt": attempt}, now if now is not None else self.clock())

    def _exec_check(self, conn, item_id, wf, run, attempt, principal=None,
                    now=None, record=True):
        """RT-P1-EXEC-OWNERSHIP: enforce the recorded exec owner of *item_id*.

        An unowned card records ``(wf, run, attempt)`` -- but only from a worker
        principal (sq5 fix 1, V2-1), bound to that principal, and never for a run
        the card's retired-run fence holds (sq5 fix 2, V1-1/V5-1): a retired run
        is refused ``exec_stale`` whether or not the card has an owner, so a
        clear can never be reacquired by the run it retired.  The same run may
        only move its attempt forward (never backwards); a different run is
        stale; a worker other than the owner's principal is refused ``forbidden``
        (the stale/lower-attempt checks decide first, as everywhere).
        Runs inside the caller's write lock; callers pass ``conn`` (== ``self.conn``)
        so the helper reads as a pure check against one connection.
        """
        from .store_cards import _exec_parts, _load_list

        wf, run, attempt = _exec_parts(item_id, (wf, run, attempt))
        row = conn.execute(
            "SELECT exec_workflow_id, exec_run_id, exec_attempt, exec_principal,"
            " exec_retired_runs, claimed_by, delegate FROM items WHERE key=?",
            (item_id,)).fetchone()
        recorded = row["exec_run_id"] if row is not None else None
        retired = [(entry.get("workflow_id"), entry.get("run_id"))
                   for entry in _load_list(row["exec_retired_runs"] or "[]")
                   if isinstance(entry, dict)] if row is not None else []
        is_owner = (recorded is not None
                    and (wf, run) == (row["exec_workflow_id"], recorded))
        if not is_owner and (wf, run) in retired:
            # The fence: whatever the card's state now, this run is done here.
            raise Refused("exec_stale",
                          f"{item_id}: run {run!r} is retired by an owner clear"
                          " and cannot reacquire the card")
        if recorded is None:
            if not record:
                return
            if principal is None or getattr(principal, "role", None) != "worker":
                name = getattr(principal, "name", None) or "<none>"
                raise Refused("forbidden",
                              f"{item_id}: only the worker principal may record"
                              f" an exec owner, not {name}")
            holder = row["claimed_by"] if row is not None else None
            if holder is None or holder != principal.name:
                raise Refused("forbidden",
                              f"{item_id} is held by {holder}: only the holder"
                              " may record its exec owner")
            self._exec_record(conn, item_id, wf, run, attempt, principal, now=now)
            return
        if run != recorded or wf != row["exec_workflow_id"]:
            raise Refused("exec_stale",
                          f"{item_id} is owned by run {recorded!r}, not {run!r}")
        at = row["exec_attempt"] or 0
        if attempt < at:
            raise Refused("exec_stale",
                          f"{item_id} is at attempt {at}; {attempt} would go backwards")
        owner_principal = row["exec_principal"]
        if (principal is not None and owner_principal is not None
                and principal.role in ("worker", "spawner")
                and principal.name != owner_principal):
            raise Refused("forbidden",
                          f"{item_id} is owned for {owner_principal}, not"
                          f" {principal.name}")
        if attempt > at and record:
            touch = ", updated_at=?" if now is not None else ""
            values = (attempt,) if now is None else (attempt, self._iso(now))
            conn.execute(f"UPDATE items SET exec_attempt=?{touch} WHERE key=?",
                         (*values, item_id))
            self._append(principal, item_id, "exec_owner_changed",
                         {"action": "advance", "workflow_id": wf, "run_id": run,
                          "attempt": attempt}, now if now is not None else self.clock())

    def _exec_state_after_owner(self, conn, item_id):
        """Whether a state write followed the current attempt record/advance.

        No new event fields: historical replay stays unchanged on both backends.
        Without an audited start, projector away-state reasons fence blind parks.
        """
        from .store_cards import STATE_EVENT_TYPES

        owner = conn.execute(
            "SELECT seq, payload FROM events WHERE key=? AND type='exec_owner_changed'"
            " ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
        if owner is not None and json.loads(owner["payload"] or "{}").get("action") == "clear":
            owner = None
        # Claim bookkeeping and the worker's own park are not projector moves.
        kinds = tuple(sorted(STATE_EVENT_TYPES - {"claimed", "released", "lease_expired"}))
        state = None
        for row in conn.execute(
                "SELECT seq, type, payload FROM events WHERE key=? AND type IN ("
                + ",".join("?" for _ in kinds) + ") ORDER BY seq DESC",
                (item_id, *kinds)):
            payload = json.loads(row["payload"] or "{}")
            if (row["type"] == "state_set" and payload.get("state") == "parked"
                    and payload.get("reason", "").startswith("parked:")):
                continue
            state = row
            break
        # Without an audited owner start, a state event is still an ordering
        # signal: a blind caller cannot prove it observed that write. Require
        # expected_state rather than silently treating the card as unordered.
        if owner is None:
            payload = json.loads(state["payload"] or "{}") if state else {}
            # Projector history already emits these durable ordering signals;
            # a plain administrative state write retains its legacy semantics.
            reason = payload.get("reason", "")
            return (payload.get("state") in ("waiting", "needs_input")
                    and (reason.startswith("exit_class=") or reason == "timer started"))
        return state is not None and state["seq"] > owner["seq"]
