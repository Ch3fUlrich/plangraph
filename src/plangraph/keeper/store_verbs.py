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


class VerbsMixin:
    """Every Store verb that moves an item through its lifecycle."""

    # --------------------------------------------------------- evidence
    def add_evidence_router(self, principal, key, kind, ref):
        return self._run(principal, "add_evidence_router", key,
                         lambda now: self._router_evidence(principal, key, kind,
                                                           ref, now))

    def _router_evidence(self, principal, key, kind, ref, now):
        self._require_role(principal, "router")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] not in ("claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}")
        with self._write():
            return self._record_evidence(principal, item, kind, ref, now)

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
        with self._write():
            return self._record_evidence(principal, item, kind, ref, now)

    def _record_evidence(self, principal, item, kind, ref, now):
        verifier = self.verifiers.get(kind)
        if verifier is None:
            raise Refused("validation", f"unknown evidence kind {kind!r}")
        result = verifier.verify(item, ref or {}, self._ctx(item, now))
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
    def claim(self, principal, key, lease_s=None):
        return self._run(principal, "claim", key,
                         lambda now: self._claim(principal, key, lease_s, now))

    def _claim(self, principal, key, lease_s, now):
        self._require_role(principal, "l1", "worker")
        lease_seconds = self._lease_seconds(lease_s)
        with self._write():
            item = self._item(key)
            if item is None:
                raise Refused("not_found", f"no item {key!r}")
            self._assert_unlocked(item)
            if principal.role in ("l1", "worker"):
                self._require_project(principal, item["project"])
            if item["state"] == "claimed":
                raise Refused("conflict", f"{key} already claimed")
            if item["state"] != "open":
                raise Refused("bad_state", f"{key} is {item['state']}, not open")
            if not self._ready(item):
                raise Refused("bad_state", f"{key} has unfinished dependencies")
            epoch = item["epoch"] + 1
            lease = self._iso(now + lease_seconds)
            self._append(principal, key, "claimed",
                         {"epoch": epoch, "claimed_by": principal.name,
                          "lease_until": lease}, now)
            self.conn.execute(
                "UPDATE items SET state='claimed', epoch=?, claimed_by=?,"
                " delegate=NULL, reviewer=NULL, lease_until=?, updated_at=?"
                " WHERE key=?",
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
            self._append(principal, key, "delegated", {"worker": name}, now)
            self.conn.execute(
                "UPDATE items SET delegate=?, updated_at=? WHERE key=?",
                (name, self._iso(now), key))
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
        lease = self._iso(now + self.default_lease)
        with self._write():
            self.conn.execute(
                "UPDATE items SET lease_until=?, updated_at=? WHERE key=?",
                (lease, self._iso(now), key))
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
        with self._write():
            self._append(principal, key, "released", {}, now)
            self.conn.execute(
                "UPDATE items SET state='open', claimed_by=NULL, delegate=NULL,"
                " reviewer=NULL, lease_until=NULL, updated_at=? WHERE key=?",
                (self._iso(now), key))
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
            self._append(principal, key, "submitted", {"epoch": epoch}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='in_review', lease_until=NULL,"
                " updated_at=? WHERE key=? AND state='claimed' AND epoch=?",
                (self._iso(now), key, epoch))
            self._row_guard(cursor, key, "claimed", epoch)
        return {"key": key, "epoch": epoch, "state": "in_review"}

    # ----------------------------------------------------------- verdict
    def verdict(self, principal, key, ok, reason=""):
        return self._run(principal, "verdict", key,
                         lambda now: self._verdict(principal, key, ok, reason, now))

    def _verdict(self, principal, key, ok, reason, now):
        self._require_role(principal, "reviewer")
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "in_review":
            raise Refused("bad_state", f"{key} is {item['state']}, not in review")
        if principal.name in (item["claimed_by"], item["delegate"]):
            raise Refused("same_principal", "reviewer is the claimant")
        self._require_project(principal, item["project"])
        self._reviewer_family_ok(principal, item)
        if ok:
            self._require_evidence(item)
            self._reverify_all(item, now)
            with self._write():
                self.conn.execute(
                    "UPDATE items SET state='done', reviewer=?, updated_at=?"
                    " WHERE key=?", (principal.name, self._iso(now), key))
                self._append(principal, key, "review_passed", {"reason": reason}, now)
            return {"key": key, "state": "done"}

        prior = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE key=? AND type='changes_requested'",
            (key,)).fetchone()[0]
        second = prior >= 1
        state = "open" if second else "claimed"
        with self._write():
            self._append(principal, key, "changes_requested",
                         {"state": state, "reason": reason}, now)
            if second:
                self.conn.execute(
                    "UPDATE items SET state='open', claimed_by=NULL, delegate=NULL,"
                    " reviewer=?, lease_until=NULL, updated_at=? WHERE key=?",
                    (principal.name, self._iso(now), key))
                self._finding(key, "changes_requested_twice",
                              "two reviews requested changes in a row", now)
            else:
                self.conn.execute(
                    "UPDATE items SET state='claimed', reviewer=?, updated_at=?"
                    " WHERE key=?", (principal.name, self._iso(now), key))
        return {"key": key, "state": state}

    # ------------------------------------------------------------ cancel
    def cancel(self, principal, key, reason=""):
        return self._run(principal, "cancel", key,
                         lambda now: self._cancel(principal, key, reason, now))

    def _cancel(self, principal, key, reason, now):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        self._may_manage(principal, item["project"])
        if item["state"] not in ("open", "claimed", "in_review"):
            raise Refused("bad_state", f"{key} is {item['state']}")
        with self._write():
            if principal.role != "router":
                dependent = self.conn.execute(
                    "SELECT 1 FROM edges e JOIN items i ON i.key=e.src"
                    " WHERE e.dst=? AND i.state NOT IN ('done','cancelled')"
                    " LIMIT 1", (key,)).fetchone()
                if dependent is not None:
                    raise Refused("conflict", f"{key} has active dependents")
            self._append(principal, key, "cancelled", {"reason": reason}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='cancelled', lease_until=NULL,"
                " updated_at=? WHERE key=? AND state=?",
                (self._iso(now), key, item["state"]))
            self._row_guard(cursor, key, item["state"], item["epoch"])
        return {"key": key, "state": "cancelled"}

    # -------------------------------------------------------------- reap
    def reap(self, principal, now=None):
        return self._run(principal, "reap", None,
                         lambda audit_now: self._reap(
                             principal, now if now is not None else audit_now))

    def _reap(self, principal, now):
        self._require_role(principal, "system")
        expired = []
        overdue = []
        with self._write():
            for item in self._all_items():
                if item["state"] == "claimed" and item["lease_until"]:
                    due = self._parse_iso(item["lease_until"])
                    if due is not None and due.timestamp() < now:
                        self._append(principal, item["key"], "lease_expired", {}, now)
                        self.conn.execute(
                            "UPDATE items SET state='open', claimed_by=NULL,"
                            " delegate=NULL, lease_until=NULL, updated_at=?"
                            " WHERE key=?", (self._iso(now), item["key"]))
                        expired.append(item["key"])
                elif item["state"] == "in_review":
                    since = self._parse_iso(item["updated_at"])
                    if since is not None and now - since.timestamp() > self.review_overdue_s:
                        if not self._has_finding(item["key"], "review_overdue"):
                            self._finding(item["key"], "review_overdue",
                                          "in review for over 4h", now)
                        overdue.append(item["key"])
        return {"expired": expired, "overdue": overdue}
