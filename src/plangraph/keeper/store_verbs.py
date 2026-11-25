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

from .. import views
from . import auth, context_sources as src, events, timing
from .errors import Refused
from .evidence_floor import record_bases
from .phase_merge import merge_subject
from .schema import STATES

#: every live state (the design-3 ones included); done/cancelled are final.
_CANCELLABLE = tuple(s for s in STATES if s not in ("done", "cancelled"))


class VerbsMixin:
    """Every Store verb that moves an item through its lifecycle."""

    claim_preflight_s = timing.DEFAULT_CLAIM_PREFLIGHT_S

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
        # A unit that runs after its item closed is the one proof a completed item may still take.
        states = ("claimed", "in_review") + (("open", "done") if kind == "deploy" else ())
        if not (kind == "deploy" and item["state"] == "done"):
            self._assert_unlocked(item)
        if item["state"] not in states:
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
            if not (kind == "deploy" and fresh["state"] == "done"):
                self._assert_unlocked(fresh)
            if fresh["state"] not in states:
                raise Refused("conflict",
                              f"{fresh['key']} is {fresh['state']}")
            if fresh["state"] == "done":
                # The item row is frozen: only the epoch is rechecked, no row is touched.
                if fresh["epoch"] != item["epoch"]:
                    raise Refused("conflict", f"{key} changed under the transaction")
            else:
                self._guarded_touch(fresh["key"], states, fresh["epoch"])
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

    def add_evidence(self, principal, key, epoch, kind, ref, *, receipt=None):
        return self._run(principal, "add_evidence", key,
                         lambda now: self._claimant_evidence(principal, key, epoch,
                                                             kind, ref, now, receipt))

    def _claimant_evidence(self, principal, key, epoch, kind, ref, now, receipt):
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
        with self._receipt_transaction(receipt) as record:
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._epoch_ok(fresh, epoch)
            if fresh["state"] != "claimed":
                raise Refused("conflict",
                              f"{fresh['key']} is {fresh['state']}, not claimed")
            if receipt is not None:
                self._check_run_principal(principal, "add_evidence", key, self.now())
            self._actor_or_delegate(principal, fresh)
            self._guarded_touch(fresh["key"], ("claimed",), epoch)
            reply = self._record_evidence(principal, fresh, kind, ref, now, result)
            record({"result": "ok", "reply": reply})
            return reply

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
                         lambda now: self._claim(principal, key, lease_s,
                                                 force_stale=force_stale))

    def worker_claim_lease(self, principal, key, lease_s=None):
        return self._run(principal, "claim", key,
                         lambda now: self._claim(principal, key, lease_s, allow_system=True))

    def _claim(self, principal, key, lease_s, *, allow_system=False,
               force_stale=None):
        self._require_role(principal, *(("l1", "worker", "system", "phase-worker")
                                        if allow_system else ("l1", "worker", "phase-worker")))
        lease = self._lease_seconds(lease_s)
        reason = self._force_stale_reason(force_stale)
        with timing.io_budget(self.claim_preflight_s):
            prepared = self._prepare_claim(principal, key, reason, {})
        with self._write():
            self._check_principal(principal)
            return self._finish_retryable_write(principal, self._commit_claim(
                principal, key, lease, prepared, self.now(), allow_system))

    def _prepare_claim(self, principal, key, reason, shared):
        timing.check_io_deadline("item eligibility")
        item = self._item(key)
        eligible = item is not None and item["state"] == "open" and self._claim_train_for(item) is not False
        timing.check_io_deadline("item eligibility")
        if not eligible:
            return None
        stale_checks = principal.role in ("l1", "worker")
        if stale_checks:
            self._require_project(principal, item["project"])
        override = self._claim_guard(item, reason, shared=shared, stale_checks=stale_checks)
        repo = (item["project"], item.get("track"))
        bases = shared.setdefault("bases", {})
        if repo not in bases:
            with timing.outside_request_lock():
                bases[repo] = record_bases(item, self._adapter("git"))
        timing.check_io_deadline("base recording")
        return item, override, bases[repo]

    def _commit_claim(self, principal, key, lease_seconds, prepared, now,
                      allow_system=False):
        item = self._get_or_refuse(key)
        if item["state"] == "cancelled":
            raise Refused("locked", f"{key} is cancelled: "
                          + self._reasons([key]).get(key, "no reason recorded"))
        self._assert_unlocked(item)
        if principal.role in ("l1", "worker"):
            self._require_project(principal, item["project"])
        if item["state"] == "claimed":
            raise Refused("conflict", f"{key} already claimed")
        train = self._claim_train_for(item) if item["state"] == "open" else False
        if train is False:
            facts = self._not_open_facts(item, self._item_neighborhood(item))
            if item["state"] != "open":
                raise Refused("bad_state", f"{key} is {item['state']}, not open"
                              + (f" ({facts})" if facts else ""))
            raise Refused("bad_state", f"{key} has unfinished dependencies: {facts}")
        if prepared is None or prepared[0] != item:
            raise Refused("conflict", f"{key} changed while checking claim eligibility")
        _, override, bases = prepared
        epoch = item["epoch"] + 1
        lease = self._iso(now + lease_seconds)
        payload = {"epoch": epoch, "claimed_by": principal.name, "lease_until": lease}
        if allow_system and principal.role in ("system", "phase-worker"):
            payload["via"] = "worker_claim"
        if override is not None:
            payload["force_stale"] = override
        if train is not None:
            bases = {**bases, item["project"]: train["base_sha"]}
            payload["train"] = train
        if item["project"] in bases:
            payload["base"] = bases[item["project"]]
        if bases:
            payload["bases"] = bases
        self._append(principal, key, "claimed", payload, now)
        self._record_claim_train(key, epoch, train)
        self.conn.execute(
            "UPDATE items SET state='claimed', epoch=?, round=0,"
            " claimed_by=?, delegate=NULL, reviewer=NULL, lease_until=?,"
            " review_waiver=NULL, updated_at=? WHERE key=?",
            (epoch, principal.name, lease, self._iso(now), key))
        return {"key": key, "epoch": epoch, "lease_until": lease,
                **({"base_sha": train["base_sha"], "train": train} if train is not None else {})}

    def claim_batch(self, principal, keys, lease_s=None, force_stale=None):
        """Claim up to 200 keys in one transaction, with one result per key."""
        return self._run(principal, "claim_batch", None, lambda now: self._claim_batch(
            principal, keys, lease_s, force_stale))

    def _claim_batch(self, principal, keys, lease_s, force_stale):
        self._require_role(principal, "l1", "worker")
        self._validate_lease_batch(keys)
        lease = self._lease_seconds(lease_s)
        reason = self._force_stale_reason(force_stale)
        shared, prepared = {}, {}
        with timing.io_budget(self.claim_preflight_s):
            for key in keys:
                try:
                    prepared[key] = self._prepare_claim(principal, key, reason, shared)
                except Refused as exc:
                    prepared[key] = exc

        def commit(key, now):
            found = prepared[key]
            if isinstance(found, Refused):
                raise found
            return self._commit_claim(principal, key, lease, found, now)

        return self._commit_lease_batch(principal, "claim", keys, commit)

    @staticmethod
    def _validate_lease_batch(keys):
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 200
                or any(not isinstance(key, str) or not key.strip() for key in keys)
                or len(set(keys)) != len(keys)):
            raise Refused("validation", "expected 1 to 200 distinct item keys")

    def _commit_lease_batch(self, principal, action, keys, commit):
        results = []
        with self._write():
            self._check_principal(principal)
            now = self.now()
            for key in keys:
                try:
                    result = commit(key, now)
                except Refused as exc:
                    self._audit(principal, action, key, False, exc.code, exc.msg, now)
                    result = {"key": key, "error": exc.code, "msg": exc.msg}
                else:
                    self._audit(principal, action, key, True, now=now)
                results.append(result)
            return self._finish_retryable_write(principal, {"results": results})

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

    def heartbeat(self, principal, key, epoch, *, receipt=None):
        return self._run(principal, "heartbeat", key,
                         lambda now: self._heartbeat(principal, key, epoch, now, receipt))

    def _heartbeat(self, principal, key, epoch, now, receipt):
        item = self._get_or_refuse(key)
        self._assert_unlocked(item)
        if item["state"] != "claimed":
            raise Refused("bad_state", f"{key} is {item['state']}, not claimed")
        self._actor_or_delegate(principal, item)
        self._epoch_ok(item, epoch)
        with self._receipt_transaction(receipt) as record:
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._epoch_ok(fresh, epoch)
            if fresh["state"] != "claimed":
                raise Refused("conflict",
                              f"{key} is {fresh['state']}, not claimed")
            if receipt is not None:
                self._check_run_principal(principal, "heartbeat", key, self.now())
            self._actor_or_delegate(principal, fresh)
            reply = self._renew_lease(principal, fresh, epoch, now)
            if receipt is not None:
                reply["source_round"] = fresh["round"]
            record({"result": "ok", "reply": reply})
            return self._finish_retryable_write(principal, reply)

    def heartbeat_batch(self, principal, claims):
        """Renew up to 200 claim epochs in one transaction."""
        return self._run(principal, "heartbeat_batch", None,
                         lambda now: self._heartbeat_batch(principal, claims))

    def _heartbeat_batch(self, principal, claims):
        self._require_role(principal, "l1", "worker")
        if not isinstance(claims, list) or any(
                not isinstance(row, dict) or type(row.get("epoch")) is not int
                or row["epoch"] <= 0 for row in claims):
            raise Refused("validation", "claims must be a list of keys and positive epochs")
        keys = [row.get("key") for row in claims]
        self._validate_lease_batch(keys)
        epochs = {row["key"]: row["epoch"] for row in claims}

        def commit(key, now):
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            if item["state"] != "claimed":
                raise Refused("bad_state", f"{key} is {item['state']}, not claimed")
            self._actor_or_delegate(principal, item)
            self._epoch_ok(item, epochs[key])
            return self._renew_lease(principal, item, epochs[key], now)

        return self._commit_lease_batch(principal, "heartbeat", keys, commit)

    def _renew_lease(self, principal, fresh, epoch, now):
        key = fresh["key"]
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
        bases = record_bases(item, self._adapter("git"))
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
            if bases:  # recorded only with a head, like base: legacy payloads
                payload["bases"] = bases      # stay byte-identical (D-3335)
            self._release_claim(principal, fresh, payload, now)
            self._record_claim_train(key, epoch, None)
        return {"key": key}

    def _release_claim(self, principal, item, payload, now):
        key, state, epoch = item["key"], item["state"], item["epoch"]
        self._append(principal, key, "released", payload, now)
        cursor = self.conn.execute(
            "UPDATE items SET state='open', claimed_by=NULL, delegate=NULL,"
            " reviewer=NULL, lease_until=NULL, review_waiver=NULL, updated_at=?"
            " WHERE key=? AND state=? AND epoch=?",
            (self._iso(now), key, state, epoch))
        self._row_guard(cursor, key, state, epoch)
        self._exec_clear(self.conn, key, principal, now)

    # ----------------------------------------------------------- submit
    def submit(self, principal, key, epoch, *, source_round=None, receipt=None):
        return self._run(principal, "submit", key,
                         lambda now: self._submit(principal, key, epoch, now, source_round, receipt))

    @staticmethod
    def _without_dep(items, gate_key, dep):
        """A shallow copy of *items* with *gate_key*'s edge to *dep* removed."""
        out = []
        for item in items:
            copy = dict(item)
            if copy["key"] == gate_key:
                copy["depends_on"] = [d for d in item["depends_on"] if d != dep]
            out.append(copy)
        return out

    def _drop_would_orphan(self, items, gate_key, dep):
        """True when removing the gate's edge to *dep* adds a validation problem."""
        known = {(p["code"], p["key"]) for p in views.validate(items)}
        after = views.validate(self._without_dep(items, gate_key, dep))
        return any((p["code"], p["key"]) not in known for p in after)

    def _submit(self, principal, key, epoch, now, source_round, receipt):
        # Read and validate inside one immediate transaction so a competing
        # writer cannot slip between the check and the guarded UPDATE.
        with self._receipt_transaction(receipt) as record:
            item = self._get_or_refuse(key)
            self._assert_unlocked(item)
            if item["state"] != "claimed":
                raise Refused("bad_state",
                              f"{key} is {item['state']}, not claimed")
            self._actor_or_delegate(principal, item)
            if principal.role == "worker":
                self._require_project(principal, item["project"])
            self._epoch_ok(item, epoch)
            if receipt is not None:
                self._check_run_principal(principal, "submit", key, self.now())
            current_round = int(item.get("round") or 0)
            if source_round is not None and (type(source_round) is not int or source_round != current_round):
                raise Refused("round_stale", f"{key} source round {source_round} does not match {current_round}")
            self._require_evidence(item)
            round_no = current_round + 1
            self._append(principal, key, "submitted",
                         {"epoch": epoch, "round": round_no}, now)
            cursor = self.conn.execute(
                "UPDATE items SET state='in_review', round=?, lease_until=NULL,"
                " updated_at=? WHERE key=? AND state='claimed' AND epoch=?",
                (round_no, self._iso(now), key, epoch))
            self._row_guard(cursor, key, "claimed", epoch)
            reply = {"key": key, "epoch": epoch, "round": round_no, "state": "in_review"}
            record({"result": "ok", "reply": reply})
            return reply

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
            self._append(principal, key, "cancelled", {"reason": reason}, now)
            # Cancelling abandons the work, so the waiver that was granted for
            # it goes too; the ``cancelled`` fold branch clears the same column.
            cursor = self.conn.execute(
                "UPDATE items SET state='cancelled', lease_until=NULL,"
                " review_waiver=NULL, updated_at=? WHERE key=? AND state=?"
                " AND epoch=?",
                (self._iso(now), key, fresh["state"], fresh["epoch"]))
            self._row_guard(cursor, key, fresh["state"], fresh["epoch"])
            # A reason that names a decision (D-id) says the item was superseded:
            # the cancel then repairs every dependent's edge in this same call.
            decided = isinstance(reason, str) and bool(src.decision_ids(reason))
            # (planned on the graph with the item already cancelled: dropping its
            # edges must not read as stranding the item itself)
            drops, kept = self._cancel_repairs(fresh, decided)
            scope = getattr(principal, "granted", None)
            if scope is None and principal.role != "router":
                scope = set(principal.projects or [])
            for drop in drops if scope is not None else ():
                if drop["key"].split(":", 1)[0] not in scope:
                    raise Refused("forbidden",
                                  f"cancelling {key} would rewrite {drop['key']}, "
                                  "outside the projects this principal may manage")
            if principal.role != "router":
                owner_repair = decided and fresh["state"] == "open"
                dependent = self.conn.execute(
                    "SELECT 1 FROM edges e JOIN items i ON i.key=e.src"
                    " WHERE e.dst=? AND i.state NOT IN ('done','cancelled')"
                    " LIMIT 1", (key,)).fetchone()
                if dependent is not None and not owner_repair:
                    raise Refused("conflict", f"{key} has active dependents"
                                  + ("; a D-id cancel by the owner role repairs them"
                                     " only for an open, unheld item" if decided else ""))
                if owner_repair and kept:
                    raise Refused("conflict", f"{key} has dependents whose edge this"
                                  " cancel cannot repair: " + "; ".join(
                                      f"{k['key']} ({k['why']})" for k in kept))
            for gate in drops:
                active = gate["state"] in ("claimed", "in_review")
                payload = {"fields": ["depends_on"],
                           "dropped_cancelled_dependency": key}
                if active:
                    payload.update({"while": gate["state"],
                                    "new_epoch": gate["epoch"] + 1})
                self.conn.execute("DELETE FROM edges WHERE src=? AND dst=?",
                                  (gate["key"], key))
                cursor = self.conn.execute(
                    "UPDATE items SET updated_at=?,epoch=epoch+?,round=round+?"
                    " WHERE key=? AND state=? AND epoch=?",
                    (self._iso(now), int(active), int(active), gate["key"],
                     gate["state"], gate["epoch"]))
                self._row_guard(cursor, gate["key"], gate["state"], gate["epoch"])
                self._append(principal, gate["key"], "item_defined", payload, now)
            self._validate_change(self._all_items(), before, affected={key},
                                  allow_new={"depends_on_cancelled"})
        out = {"key": key, "state": "cancelled"}
        if decided:
            out["dropped_from"] = sorted(d["key"] for d in drops)
            if kept:
                out["kept"] = kept
        return out

    def _cancel_repairs(self, item, decided):
        """``(edges to drop, edges kept)`` for the dependents of a cancelled *item*.

        Frozen gates retain their history; live gates and goals no longer wait
        for abandoned work, and an edge that is the only path from live work to
        a goal stays (legacy ``depends_on_cancelled``).  With a D-id (*decided*)
        every open dependent loses its edge too, except an edge to an operator
        item (a human step the dependent waits on), a claimed or in-review
        dependent (its epoch would reset) and an edge that would orphan work;
        those are kept and named.
        """
        key = item["key"]
        rows = self.conn.execute(
            "SELECT i.key,i.kind,i.state,i.epoch FROM edges e JOIN items i ON i.key=e.src"
            " WHERE e.dst=? AND i.state NOT IN ('done','cancelled') ORDER BY i.key",
            (key,)).fetchall()
        drops, kept, current = [], [], self._all_items()
        for row in rows:
            structural = row["kind"] in ("gate", "goal")
            if not structural and not decided:
                continue
            why = None
            if not structural and item.get("operator"):
                why = "operator edge"
            elif not structural and row["state"] != "open":
                why = row["state"]
            elif self._drop_would_orphan(current, row["key"], key):
                why = "would orphan work"
            if why is not None:
                if decided:
                    kept.append({"key": row["key"], "why": why})
                continue
            current = self._without_dep(current, row["key"], key)
            drops.append(row)
        return drops, kept
