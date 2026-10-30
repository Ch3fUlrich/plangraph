"""Lifecycle mutation verbs for :class:`plangraph.keeper.store.Store`.

Ordering rule that the locking triggers force: for a transition that ends in
``done`` the cache row is written *before* the ``review_passed`` event, because
the event is what makes the row locked; for every other transition the event is
appended first and the cache follows.  Both orderings keep the cache and the
event log consistent while never tripping the store's own lock triggers.

The definition verbs live in ``store_create`` and the shared helpers (validation,
normalisation, evidence checks, authorisation) live on the ``Store`` base class.
"""
from __future__ import annotations

from . import auth, events
from .errors import Refused
from .phase_merge import merge_subject
from .schema import STATES

#: every live state (the design-3 ones included); done/cancelled are final.
_CANCELLABLE = tuple(s for s in STATES if s not in ("done", "cancelled"))


class VerbsMixin:
    """Every Store verb that moves an item through its lifecycle."""

    # --------------------------------------------------------- evidence
    def add_evidence_router(self, principal, key, kind, ref):
        return self._run(principal, "add_evidence_router", key,
                         lambda now: self._router_evidence(principal, key, kind,
                                                           ref, now))

    def _router_evidence(self, principal, key, kind, ref, now):
        self._require_role(principal, "router", "system")
        # System access exists only for phase merge proof.
        if principal.role == "system" and kind != "commit+ci":
            raise Refused("forbidden", "system may attach only commit+ci evidence")
        item = self._get_or_refuse(key)
        if principal.role == "system":
            gates = self._phase_evidence_scope(item)
            git = self._adapter("git")
            sha = ref.get("commit") if isinstance(ref, dict) else None
            repo = git.repo_path(item["project"]) if git is not None else None
            inspect = getattr(git, "two_parent_merge_subject", None)
            subject = inspect(repo, sha) if repo and inspect else None
            if subject not in {merge_subject(gate) for gate in gates}:
                raise Refused("forbidden", "system evidence requires a two-parent phase merge")
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}")
        # Slow verification (git/gh/http) happens before the write lock.
        result = self._verify_evidence(item, kind, ref, now)
        if principal.role == "system" and not result.ok:
            raise Refused("evidence_unverified", result.detail)
        with self._write():
            fresh = self._get_or_refuse(key)
            if principal.role == "system":
                gates = self._phase_evidence_scope(fresh)
                if subject not in {merge_subject(gate) for gate in gates}:
                    raise Refused("forbidden", "phase merge no longer matches the card's gate")
            self._assert_unlocked(fresh)
            if fresh["state"] not in ("claimed", "in_review"):
                raise Refused("conflict",
                              f"{fresh['key']} is {fresh['state']}")
            self._guarded_touch(fresh["key"], ("claimed", "in_review"),
                                fresh["epoch"])
            return self._record_evidence(principal, fresh, kind, ref, now,
                                         result)

    def _phase_evidence_scope(self, item):
        # Recheck under the write lock: cancellation/state changes revoke this door.
        gates = self.conn.execute(
            "SELECT i.key FROM edges e JOIN items i ON i.key=e.src"
            " WHERE e.dst=? AND i.kind='gate' AND i.state!='cancelled'",
            (item["key"],)).fetchall()
        if item["kind"] != "lane" or item["state"] != "in_review" or not gates:
            raise Refused("forbidden", "system evidence requires an in_review phase card")
        return [gate["key"] for gate in gates]

    def add_evidence(self, principal, key, epoch, kind, ref):
        return self._run(principal, "add_evidence", key,
                         lambda now: self._claimant_evidence(principal, key, epoch,
                                                             kind, ref, now))

    def _claimant_evidence(self, principal, key, epoch, kind, ref, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "claimed":
            raise Refused("bad_state", f"{key} is {item['state']}, not claimed")
        self._actor_or_delegate(principal, item)
        if principal.role == "worker":
            self._require_project(principal, item["project"])
        self._epoch_ok(item, epoch)
        # Slow verification runs outside the write lock; the row is inserted
        # only under the state/epoch guard below.
        result = self._verify_evidence(item, kind, ref, now)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._epoch_ok(fresh, epoch)
            if fresh["state"] != "claimed":
                raise Refused("conflict",
                              f"{fresh['key']} is {fresh['state']}, not claimed")
            self._actor_or_delegate(principal, fresh)
            self._guarded_touch(fresh["key"], ("claimed",), epoch)
            return self._record_evidence(principal, fresh, kind, ref, now,
                                         result)

    def _record_evidence(self, principal, item, kind, ref, now, result):
        self.conn.execute(
            "INSERT INTO evidence(key,epoch,kind,ref,verified,detail,at)"
            " VALUES(?,?,?,?,?,?,?)",
            (item["key"], item["epoch"], kind, events.canonical(ref or {}),
             1 if result.ok else 0, result.detail, self._iso(now)))
        self._append(principal, item["key"], "evidence_added",
                     {"kind": kind, "ref": result.ref or ref or {},
                      "verified": bool(result.ok), "detail": result.detail}, now)
        return {"key": item["key"], "kind": kind, "verified": bool(result.ok),
                "detail": result.detail, "ref": result.ref or ref or {}}

    # ------------------------------------------------------------ claim
    def claim(self, principal, key, lease_s=None, force_stale=None):
        """TASK-CONTEXT-GUARD: a stale claim is refused unless *force_stale*."""
        return self._run(principal, "claim", key,
                         lambda now: self._claim(principal, key, lease_s, now,
                                                 force_stale=force_stale))

    def worker_claim_lease(self, principal, key, lease_s=None):
        return self._run(principal, "claim", key,
                         lambda now: self._claim(principal, key, lease_s, now, allow_system=True))

    def _claim(self, principal, key, lease_s, now, *, allow_system=False,
               force_stale=None):
        # Only the worker_claim door admits the system principal (the phase worker runs as it); /v1/claim stays l1/worker.
        self._require_role(principal, *(("l1", "worker", "system") if allow_system else ("l1", "worker")))
        lease_seconds = self._lease_seconds(lease_s)
        if force_stale is not None and not isinstance(force_stale, str):
            raise Refused("validation", "force_stale must be a reason string")
        override = None
        first = self._item(key)
        if first is not None and first["state"] == "open" and self._ready(first) \
                and principal.role in ("l1", "worker"):
            # The slow repo/decision reads run before the write lock.
            self._require_project(principal, first["project"])
            override = self._claim_guard(first, force_stale)
        with self._write():
            item = self._item(key)
            if item is None:
                raise Refused("not_found", f"no item {key!r}")
            if item["state"] == "cancelled":
                raise Refused("locked", f"{key} is cancelled: "
                              + self._reasons([key]).get(key, "no reason recorded"))
            self._assert_unlocked(item)
            if principal.role in ("l1", "worker"):
                self._require_project(principal, item["project"])
            if item["state"] == "claimed":
                raise Refused("conflict", f"{key} already claimed")
            if item["state"] != "open" or not self._ready(item):
                index = {it["key"]: it for it in self._all_items()}
                facts = self._not_open_facts(item, index)
                if item["state"] != "open":
                    raise Refused("bad_state", f"{key} is {item['state']}, not open"
                                  + (f" ({facts})" if facts else ""))
                raise Refused("bad_state", f"{key} has unfinished dependencies: {facts}")
            epoch = item["epoch"] + 1
            lease = self._iso(now + lease_seconds)
            payload = {
                "epoch": epoch,
                "claimed_by": principal.name,
                "lease_until": lease,
            }
            if allow_system and principal.role == "system":
                payload["via"] = "worker_claim"
            if override is not None:
                payload["force_stale"] = override
            base = self._ref_head(item["project"])
            if base is not None:
                payload["base"] = base
            self._append(principal, key, "claimed", payload, now)
            self.conn.execute(
                "UPDATE items SET state='claimed', epoch=?, round=0,"
                " claimed_by=?, delegate=NULL, reviewer=NULL, lease_until=?,"
                " review_waiver=NULL, updated_at=? WHERE key=?",
                (epoch, principal.name, lease, self._iso(now), key))
        return {"key": key, "epoch": epoch, "lease_until": lease}

    def delegate(self, principal, key, worker_principal):
        return self._run(principal, "delegate", key,
                         lambda now: self._delegate(principal, key,
                                                    worker_principal, now))

    def _delegate(self, principal, key, worker_principal, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "claimed":
            raise Refused("bad_state", f"{key} is {item['state']}, not claimed")
        if principal.name != item["claimed_by"]:
            raise Refused("forbidden", "only the claimant may delegate")
        name = getattr(worker_principal, "name", worker_principal)
        worker = auth.get_principal(self.conn, name)
        if worker is None or worker.role != "worker":
            raise Refused("validation", f"{name!r} is not a worker principal")
        if item["project"] not in (worker.projects or []):
            raise Refused("forbidden",
                          f"worker {name!r} is not scoped to {item['project']!r}")
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            if fresh["state"] != "claimed":
                raise Refused("conflict",
                              f"{key} is {fresh['state']}, not claimed")
            if principal.name != fresh["claimed_by"]:
                raise Refused("forbidden", "only the claimant may delegate")
            self._append(principal, key, "delegated", {"worker": name}, now)
            cursor = self.conn.execute(
                "UPDATE items SET delegate=?, updated_at=?"
                " WHERE key=? AND state='claimed' AND epoch=?",
                (name, self._iso(now), key, item["epoch"]))
            self._race_guard(cursor, key, "claimed", item["epoch"])
        return {"key": key, "delegate": name}

    def heartbeat(self, principal, key, epoch):
        return self._run(principal, "heartbeat", key,
                         lambda now: self._heartbeat(principal, key, epoch, now))

    def _heartbeat(self, principal, key, epoch, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "claimed":
            raise Refused("bad_state", f"{key} is {item['state']}, not claimed")
        self._actor_or_delegate(principal, item)
        self._epoch_ok(item, epoch)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._epoch_ok(fresh, epoch)
            if fresh["state"] != "claimed":
                raise Refused("conflict",
                              f"{key} is {fresh['state']}, not claimed")
            self._actor_or_delegate(principal, fresh)
            # D-837: a heartbeat extends, never shortens - a claim may hold a lease longer than
            # the default (claim lease_s), and the fold expects the cache lease >= the claimed one.
            current = self._parse_iso(fresh["lease_until"]) if fresh["lease_until"] else None
            lease = self._iso(max(now + self.default_lease,
                                  current.timestamp() if current is not None else 0))
            cursor = self.conn.execute(
                "UPDATE items SET lease_until=?, updated_at=?"
                " WHERE key=? AND state='claimed' AND epoch=?",
                (lease, self._iso(now), key, epoch))
            self._race_guard(cursor, key, "claimed", epoch)
            if isinstance(principal, auth.RunPrincipal):
                self._extend_run_token(principal, dict(fresh, lease_until=lease), now)
        return {"key": key, "epoch": epoch, "lease_until": lease}

    def release(self, principal, key, epoch):
        return self._run(principal, "release", key,
                         lambda now: self._release(principal, key, epoch, now))

    def _release(self, principal, key, epoch, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "claimed":
            raise Refused("bad_state", f"{key} is {item['state']}, not claimed")
        if principal.role != "router" and principal.name != item["claimed_by"]:
            raise Refused("forbidden", "only the claimant or router may release")
        self._epoch_ok(item, epoch)
        base = self._ref_head(item["project"])
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._epoch_ok(fresh, epoch)
            if fresh["state"] != "claimed":
                raise Refused("conflict",
                              f"{key} is {fresh['state']}, not claimed")
            if principal.role != "router" and principal.name != fresh["claimed_by"]:
                raise Refused("forbidden",
                              "only the claimant or router may release")
            payload = {"base": base} if base is not None else {}
            self._append(principal, key, "released", payload, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='open', claimed_by=NULL, delegate=NULL,"
                " reviewer=NULL, lease_until=NULL, review_waiver=NULL,"
                " updated_at=?"
                " WHERE key=? AND state='claimed' AND epoch=?",
                (self._iso(now), key, epoch))
            self._race_guard(cursor, key, "claimed", epoch)
            self._exec_clear(self.conn, key, principal, now)
        return {"key": key}

    # ----------------------------------------------------------- submit
    def submit(self, principal, key, epoch):
        return self._run(principal, "submit", key,
                         lambda now: self._submit(principal, key, epoch, now))

    def _submit(self, principal, key, epoch, now):
        # Read and validate inside one immediate transaction so a competing
        # writer cannot slip between the check and the guarded UPDATE.
        with self._write():
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            if item["state"] != "claimed":
                raise Refused("bad_state",
                              f"{key} is {item['state']}, not claimed")
            self._actor_or_delegate(principal, item)
            if principal.role == "worker":
                self._require_project(principal, item["project"])
            self._epoch_ok(item, epoch)
            self._require_evidence(item)
            round_no = int(item.get("round") or 0) + 1
            self._append(principal, key, "submitted",
                         {"epoch": epoch, "round": round_no}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='in_review', round=?, lease_until=NULL,"
                " updated_at=? WHERE key=? AND state='claimed' AND epoch=?",
                (round_no, self._iso(now), key, epoch))
            self._row_guard(cursor, key, "claimed", epoch)
        return {"key": key, "epoch": epoch, "round": round_no,
                "state": "in_review"}

    # ------------------------------------------------------------ cancel
    def cancel(self, principal, key, reason=""):
        return self._run(principal, "cancel", key,
                         lambda now: self._cancel(principal, key, reason, now))

    def _cancel(self, principal, key, reason, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        self._may_manage(principal, item["project"])
        if item["state"] not in _CANCELLABLE:
            raise Refused("bad_state", f"{key} is {item['state']}")
        before = self._all_items()
        # Cancelling deliberately dangles dependents; that is the one new code
        # it is allowed to introduce.  Any other regression is refused.
        self._validate_change(self._changed(before, key, "cancelled"), before,
                              affected={key},
                              allow_new={"depends_on_cancelled"})
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            # The epoch is re-read here: a release+reclaim between the pre-read
            # and this transaction lands in the same ``claimed`` state at a new
            # epoch, and must not be cancelled by the stale request.
            if fresh["state"] != item["state"] or fresh["epoch"] != item["epoch"]:
                raise Refused("conflict",
                              f"{key} changed under the transaction")
            if principal.role != "router":
                dependent = self.conn.execute(
                    "SELECT 1 FROM edges e JOIN items i ON i.key=e.src"
                    " WHERE e.dst=? AND i.state NOT IN ('done','cancelled')"
                    " LIMIT 1", (key,)).fetchone()
                if dependent is not None:
                    raise Refused("conflict", f"{key} has active dependents")
            self._append(principal, key, "cancelled", {"reason": reason}, now)
            # Cancelling abandons the work, so the waiver that was granted for
            # it goes too; the ``cancelled`` fold branch clears the same column.
            cursor = self.conn.execute(
                "UPDATE items SET state='cancelled', lease_until=NULL,"
                " review_waiver=NULL, updated_at=? WHERE key=? AND state=?"
                " AND epoch=?",
                (self._iso(now), key, fresh["state"], fresh["epoch"]))
            self._row_guard(cursor, key, fresh["state"], fresh["epoch"])
        return {"key": key, "state": "cancelled"}
