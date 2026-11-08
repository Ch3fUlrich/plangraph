"""Worker verbs: the calls the Rust worker (rust/worker/src/keeper.rs) makes.

A worker/spawner/l1 principal is scoped to the card's project via
``self._may_act_in(principal, item["project"])`` (defined in ``CardsMixin``);
router and system principals pass anywhere.

``worker_park`` is bound to the card's current claim: the caller must be the
claim holder (or its delegate) and the lease must still be alive.  Merge-lock
owners are bound to the calling principal: the stored and echoed owner is
``<escaped principal>:<owner>``, where ``%`` and ``:`` in the principal name
are percent-escaped so the first ``:`` always ends the principal segment, and
the lock is a lease lock: mutual exclusion by expiry, with ``token`` a
monotonically growing advisory generation.  The keeper does not fence writes
store-side — it never rejects a stale writer's later writes.  The Rust client
names this value ``Fence``; the keeper guarantees a lease lock, not store-side
fencing of writes.

``attempts`` rows are written ONLY by the projector, which reads Temporal
history: it numbers them ``MAX(n)+1`` and carries the cost.  ``worker_attempt``
therefore records an audit row only — inserting here would double the rows.

RT-P1-EXEC-OWNERSHIP: park, complete, alarm and audit-only attempt calls
are checked against the card's recorded exec owner (``_exec_gate``).  A body
that names ``workflow_id``, ``run_id`` or ``attempt`` must present all three
and match the owner — an unowned card records its first owner, a different run
or a lower attempt is refused as ``exec_stale`` — and a card that already has
an owner refuses bodies that omit all three, so one run cannot act on another
run's card.  Bodies without the fields keep the historical behaviour on
unowned cards.

Order inside the park/complete guards: the stale-run and lower-attempt checks
run first (validating only), then a DUPLICATE -- the card already reached the
verb's final state (``parked``/``done``) and the body either names the
recorded owner or, with no owner, names no triple -- is a success no-op that
short-circuits before ``expected_state``.  A retried park whose response was
lost therefore succeeds instead of failing every retry with
``exec_stale_state``; a different run is still ``exec_stale``.

An open ``worker_alarm`` row is a DUPLICATE FACT, never a fence: ``alarm``
records on an OWNED card even when that row already exists -- the attempt
advances, a stale run or a backwards attempt is refused as ``exec_stale`` --
while on an UNOWNED card an existing open alarm still acquires no owner.

All SQL is portable (``?`` placeholders, ids assigned as ``MAX(id)+1`` inside
the write lock) so it runs unchanged on SQLite and Postgres.
"""
from __future__ import annotations

import json
import re

from .errors import Refused
from .schema import EXIT_CLASSES
from .store_cards import _forward, SETTABLE_STATES, _count, _exec_parts, _number, _text

__all__ = ["WorkerMixin"]

_ATTEMPT_KEYS = frozenset(("card", "n", "leg", "exit_class", "diff_hash",
                           "check_tail", "cost", "tokens_in", "tokens_out",
                           "workflow_id", "run_id", "attempt"))
#: The three fields that name an exec run on a worker body.
_EXEC_IDS = ("workflow_id", "run_id", "attempt")
#: EXEC-OBSERVED-STATE: the worker's live set -- the ``expected_state`` a run
#: that observed nothing falls back to, and the one state set the keeper still
#: treats as a claim holder's "any live state" on a claimed card.
_LIVE_SET = frozenset(("open", "ready", "running", "waiting", "needs_input"))
#: EXEC-OBSERVED-STATE: the states a projector event writes while a run is away
#: from the card (usage_limit/outage and TIMER_STARTED -> ``waiting``, a
#: needs_input exit class -> ``needs_input``). A PARK onto one of them must
#: carry that state as the run's own observation: the live-set fallback says
#: "I observed nothing" and is refused, so a delayed same-run park cannot land
#: on a state this run never saw.
_AWAY_STATES = frozenset(("waiting", "needs_input"))
# ``n`` is the projector's attempt number, distinct from the exec attempt.
_PARK_KEYS = frozenset(("card", "tier", "exit_class", "check_tail", "epoch",
                        "workflow_id", "run_id", "attempt", "expected_state"))
_COMPLETE_KEYS = frozenset(("card", "merged_sha", "workflow_id", "run_id",
                            "attempt", "expected_state"))
_ALARM_KEYS = frozenset(("card", "reason", "workflow_id", "run_id", "attempt"))
_ACQUIRE_KEYS = frozenset(("owner", "ttl_s"))
_RELEASE_KEYS = frozenset(("token", "owner"))
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_LOCK_ROLES = ("router", "system", "l1", "worker", "spawner", "phase-worker")
_OWNER_MAX = 512


def _check_body(body, allowed, name):
    if not isinstance(body, dict):
        raise Refused("validation", f"{name} body must be an object")
    unknown = set(body) - allowed
    if unknown:
        raise Refused("validation", f"unknown {name} fields: {sorted(unknown)}")


def _strings(body, *names):
    for name in names:
        if not isinstance(body.get(name, ""), str):
            raise Refused("validation", f"{name} must be a string")


def _card_name(body):
    """The raw ``card`` string of *body* for the audit key (None if absent)."""
    if not isinstance(body, dict):
        return None
    card = body.get("card")
    return card if isinstance(card, str) else None


def _escape_principal_name(name):
    """Percent-escape ``%`` and ``:`` so the first ``:`` ends the name segment."""
    return name.replace("%", "%25").replace(":", "%3A")


def _lock_owner(principal, owner):
    """The stored/echoed lock owner: bound to the calling principal."""
    return f"{_escape_principal_name(principal.name)}:{owner}"


from .store_worker_helpers import WorkerGuardsMixin
from .store_merge_locks import MergeLocksMixin


class WorkerMixin(WorkerGuardsMixin, MergeLocksMixin):
    """The worker-facing verbs for :class:`plangraph.keeper.store.Store`."""

    # ------------------------------------------------------------- helpers
    def worker_attempt(self, principal, body):
        result = self._run(principal, "worker_attempt", _card_name(body),
                           lambda now: self._worker_attempt(principal, body, now))
        self._fill_jev_outcome(principal, result["card"],
                               {"exit_class": result["exit_class"]}, attempt=result["n"])
        return result

    def _worker_attempt(self, principal, body, now):
        _check_body(body, _ATTEMPT_KEYS, "worker_attempt")
        card = _text("card", body.get("card"), required=True)
        number = body.get("n")
        if (isinstance(number, bool) or not isinstance(number, int)
                or number < 1):
            raise Refused("validation", "n must be a positive integer")
        exit_class = body.get("exit_class")
        if exit_class not in EXIT_CLASSES:
            raise Refused("validation",
                          f"exit_class must be one of {list(EXIT_CLASSES)}")
        _strings(body, "leg", "diff_hash", "check_tail")   # not stored: type only
        _count("tokens_in", body.get("tokens_in"))
        _count("tokens_out", body.get("tokens_out"))
        _number("cost", body.get("cost"))
        key = self._resolve_scoped(principal, card)
        self._exec_gate(key, body, principal, record=False)
        return {"card": key, "n": number, "exit_class": exit_class}

    # -------------------------------------------------------------- park
    def worker_park(self, principal, body):
        result = self._run(principal, "worker_park", _card_name(body),
                           lambda now: self._worker_park(principal, body, now))
        # RT-P1-JEV-SHADOW: the attempt finished - see ``_fill_jev_outcome``.
        self._fill_jev_outcome(principal, result.get("card"),
                               {"exit_class": body.get("exit_class")})
        return result

    def _worker_park(self, principal, body, now):
        _check_body(body, _PARK_KEYS, "worker_park")
        card = _text("card", body.get("card"), required=True)
        tier = _text("tier", body.get("tier"), key=card) or ""
        exit_class = body.get("exit_class")
        if exit_class not in EXIT_CLASSES:
            raise Refused("validation",
                          f"exit_class must be one of {list(EXIT_CLASSES)}")
        _strings(body, "check_tail")   # not stored: type only (a tail can be long)
        epoch = body.get("epoch")
        if epoch is not None and (isinstance(epoch, bool)
                                  or not isinstance(epoch, int)):
            raise Refused("validation", "epoch must be an integer")
        key = self._resolve_scoped(principal, card)
        why = f"parked: {exit_class} ({tier})"[:500]

        def guard(item):
            # Validate stale runs first, without acquiring ownership.
            self._exec_gate(item["key"], body, principal, record=False)
            # 2. A duplicate park is a success no-op, ahead of expected_state.
            if self._exec_duplicate(item, body, "parked"):
                return
            # A claim release is not permission to roll back a newer state.
            if (body.get("expected_state") is None
                    and self._exec_state_after_owner(self.conn, item["key"])):
                raise Refused("exec_stale", f"{key}: state changed after exec owner")
            # Observed-state CAS, then claim binding and ownership.
            self._exec_expect_state(item, body.get("expected_state"), principal,
                                    park=True)
            # Claimed is lease-bound; other states follow the forward table.
            if item["state"] != "claimed" and not _forward(item["state"], "parked"):
                raise Refused("bad_state", f"{key}: {item['state']} -> parked")
            self._park_guard(principal, item, epoch, now)
            if item["state"] != "parked":
                self._exec_gate(item["key"], body, principal, record=True)

        result = self._apply_state(principal, key, "parked", why, now,
                                   lambda project: self._may_act_in(principal,
                                                                    project),
                                   guard=guard,
                                   release_claim=True)
        return {"card": key, "state": "parked", "changed": result["changed"]}

    # ---------------------------------------------------------- complete
    def worker_complete(self, principal, body):
        result = self._run(principal, "worker_complete", _card_name(body),
                           lambda now: self._worker_complete(principal, body, now))
        # RT-P1-JEV-SHADOW: the attempt finished - see ``_fill_jev_outcome``.
        self._fill_jev_outcome(principal, result.get("card"),
                               {"outcome": "complete"})
        return result

    def _worker_complete(self, principal, body, now):
        _check_body(body, _COMPLETE_KEYS, "worker_complete")
        card = _text("card", body.get("card"), required=True)
        merged_sha = body.get("merged_sha")
        if not isinstance(merged_sha, str) or not _SHA_RE.fullmatch(merged_sha):
            raise Refused("validation",
                          "merged_sha must be 7 to 64 hex characters")
        key = self._resolve_scoped(principal, card)
        with self._write():
            item = self._get_or_refuse(key)
            # Validate stale runs first, without acquiring ownership.
            self._exec_gate(key, body, principal, record=False)
            # 2. A duplicate complete is a success no-op, ahead of expected_state.
            duplicate = self._exec_duplicate(item, body, "done")
            if duplicate:
                return {"card": key, "merged_sha": merged_sha}
            # Only proven retries bypass the lease; first completions always
            # bind the claim before any write, using this verb's clock.
            self._exec_expect_state(item, body.get("expected_state"), principal,
                                    now=now, claim_checked=True)
            self._park_guard(principal, item, None, now)
            if self._system_phase_holder(principal, item):
                # Only phase-worker claims may bypass pre-merge evidence.
                self._assert_unlocked(item)
                # Step 1 validated exec fences; system holders cannot record owners.
                round_no = int(item.get("round") or 0) + 1
                self._append(principal, key, "state_set",
                             {"state": "in_review", "was": "claimed",
                              "reason": "worker complete", "epoch": item["epoch"],
                              "round": round_no, "worker_complete": True}, now)
                cursor = self.conn.execute(
                    "UPDATE items SET state='in_review', round=?, lease_until=NULL,"
                    " updated_at=? WHERE key=? AND state='claimed' AND epoch=?",
                    (round_no, self._iso(now), key, item["epoch"]))
                self._race_guard(cursor, key, "claimed", item["epoch"])
                return {"card": key, "merged_sha": merged_sha}
            if not duplicate:
                if item["state"] != "done":
                    # 3. Ownership, then the completion write -- a state/epoch
                    #    CAS (``_guarded_touch``) in the SAME transaction as the
                    #    gate and the record above, mirroring park's single
                    #    ``_apply_state`` transaction: if the completion write
                    #    is refused or loses a race, the owner/attempt recorded
                    #    here rolls back with it, so a failed completion never
                    #    leaves a recorded owner (V1-A).
                    self._exec_gate(key, body, principal, record=True)
                    self._guarded_touch(key, (item["state"],), item["epoch"])
        return {"card": key, "merged_sha": merged_sha}

    # ------------------------------------------------------------ alarms
    def worker_alarm(self, principal, body):
        return self._run(principal, "worker_alarm", _card_name(body),
                         lambda now: self._worker_alarm(principal, body, now))

    def _worker_alarm(self, principal, body, now):
        _check_body(body, _ALARM_KEYS, "worker_alarm")
        card = _text("card", body.get("card"), required=True)
        reason = body.get("reason")
        if isinstance(reason, str):
            reason = reason[:500]   # an over-long reason is clipped, never refused
        reason = _text("reason", reason, required=True, cap=500, key=card)
        key = self._resolve_scoped(principal, card)
        with self._write():
            item = self._get_or_refuse(key)
            self._park_guard(principal, item, None, now)
            existing = self.conn.execute(
                "SELECT 1 FROM projector_alarms WHERE kind='worker_alarm'"
                " AND workflow_id='' AND key=? AND cleared_at IS NULL",
                (key,)).fetchone()
            # The open alarm is only a DUPLICATE FACT -- the INSERT below is
            # ``ON CONFLICT DO NOTHING`` -- so it never freezes the fence on an
            # OWNED card: a run-carrying alarm always RECORDS there, advancing
            # the recorded attempt (or refusing a stale run / a backwards
            # attempt as ``exec_stale``), otherwise a later call carrying an
            # older attempt of the same run would pass.  On an UNOWNED card an
            # open alarm stays what it was: a fact that acquires no owner
            # (``existing is None``), so a duplicate alarm of a new run still
            # records nothing.
            row = self.conn.execute(
                "SELECT exec_run_id FROM items WHERE key=?", (key,)).fetchone()
            owned = row is not None and row["exec_run_id"] is not None
            self._exec_gate(key, body, principal, record=existing is None or owned)
            self.conn.execute(
                "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,"
                "raised_at)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms),"
                "'worker_alarm','',?,?,?) "
                "ON CONFLICT (kind, workflow_id, key) WHERE cleared_at IS NULL"
                " DO NOTHING",
                (key, reason, self._iso(now)))
        return {"card": key, "kind": "worker_alarm"}

