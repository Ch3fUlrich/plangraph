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

All SQL is portable (``?`` placeholders, ids assigned as ``MAX(id)+1`` inside
the write lock) so it runs unchanged on SQLite and Postgres.
"""
from __future__ import annotations

import re

from .errors import Refused
from .schema import EXIT_CLASSES
from .store_cards import SETTABLE_STATES, _count, _exec_parts, _forward, _number, _text

__all__ = ["WorkerMixin"]

_ATTEMPT_KEYS = frozenset(("card", "n", "leg", "exit_class", "diff_hash",
                           "check_tail", "cost", "tokens_in", "tokens_out",
                           "workflow_id", "run_id", "attempt"))
# ``n`` is the projector's attempt number, distinct from the exec attempt.
_PARK_KEYS = frozenset(("card", "tier", "exit_class", "check_tail", "epoch",
                        "workflow_id", "run_id", "attempt", "expected_state"))
_COMPLETE_KEYS = frozenset(("card", "merged_sha", "workflow_id", "run_id",
                            "attempt", "expected_state"))
_ALARM_KEYS = frozenset(("card", "reason", "workflow_id", "run_id", "attempt"))
_ACQUIRE_KEYS = frozenset(("owner", "ttl_s"))
_RELEASE_KEYS = frozenset(("token", "owner"))
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_LOCK_ROLES = ("router", "system", "l1", "worker", "spawner")
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


class WorkerMixin:
    """The worker-facing verbs for :class:`plangraph.keeper.store.Store`."""

    # ------------------------------------------------------------- helpers
    def _exec_expect_state(self, item, expected) -> None:
        """Check observed state inside the write lock, before recording ownership."""
        if expected is None:
            return
        states = [expected] if isinstance(expected, str) else expected
        if (not isinstance(states, list) or not states
                or any(not isinstance(s, str) or s not in SETTABLE_STATES
                       for s in states)):
            raise Refused("validation",
                          f"expected_state must be a state or non-empty list from {list(SETTABLE_STATES)}")
        if item["state"] not in states:
            raise Refused("exec_stale_state",
                          f"{item['key']}: expected {expected!r}, is {item['state']!r}")

    def _resolve_scoped(self, principal, card):
        """Resolve *card* to its live key, refuse if unknown, check scope."""
        key = self.resolve_key(card)
        item = self._get_or_refuse(key)
        self._may_act_in(principal, item["project"])
        return key

    def _park_guard(self, principal, item, epoch, now):
        """Bind a park to the card's current claim (same rules as heartbeat/submit).

        Checked, inside the write lock, for every principal except router/system
        (which act anywhere, as everywhere else in the keeper):
        * a card with no claim (no ``claimed_by``: ``running`` set via set_state)
          has no holder to bind: only project scope applies;
        * otherwise the principal name must be the card's ``claimed_by`` or
          ``delegate`` (``_actor_or_delegate``, 403 forbidden): a worker whose
          claim was released and re-taken by another principal is refused;
        * if the body carried ``epoch`` it must equal the card's epoch
          (``_epoch_ok``, 409 epoch_stale) -- the Rust ParkPacket has no epoch,
          so the field is optional and holder + lease are the binding it has;
        * an expired lease (``lease_until`` at or before now) is refused as
          409 epoch_stale: the claim is dead and the reaper will reopen the card.
        """
        if principal.role in ("router", "system") or not item["claimed_by"]:
            return
        self._actor_or_delegate(principal, item)
        if epoch is not None:
            self._epoch_ok(item, epoch)
        due = self._parse_iso(item["lease_until"]) if item["lease_until"] else None
        if due is not None and due.timestamp() <= now:
            raise Refused("epoch_stale", f"{item['key']}: claim lease expired")

    def _exec_gate(self, key, body, *, record=True):
        """Check a worker verb body against the card's recorded exec owner.

        Runs INSIDE the caller's write lock (park's ``guard`` hook and
        complete/alarm's write block) because it may record the owner or bump
        the attempt.  RT-P1-EXEC-OWNERSHIP: a body naming any of
        ``workflow_id``/``run_id``/``attempt`` must present all three and match
        the owner (``_exec_check``: a first call records it; a stale run or a
        lower attempt is refused); a body without them keeps the historical
        behaviour unless the card is already owned. ``record=False`` validates
        an unowned call without acquiring ownership (audit-only or no-op).
        """
        row = self.conn.execute(
            "SELECT exec_run_id FROM items WHERE key=?", (key,)).fetchone()
        owned = row is not None and row["exec_run_id"] is not None
        if not any(body.get(name) is not None
                   for name in ("workflow_id", "run_id", "attempt")):
            if owned:
                raise Refused("exec_stale",
                              f"{key} has an exec owner: workflow_id, run_id"
                              " and attempt are required")
            return
        parts = _exec_parts(key, (body.get("workflow_id"), body.get("run_id"),
                                  body.get("attempt")))
        # Audit-only and no-op calls validate but cannot acquire a first owner.
        if owned or record:
            self._exec_check(self.conn, key, *parts, record=record)

    # ----------------------------------------------------------- attempts
    def worker_attempt(self, principal, body):
        return self._run(principal, "worker_attempt", _card_name(body),
                         lambda now: self._worker_attempt(principal, body, now))

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
        with self._write():
            self._exec_gate(key, body, record=False)
        return {"card": key, "n": number, "exit_class": exit_class}

    # -------------------------------------------------------------- park
    def worker_park(self, principal, body):
        return self._run(principal, "worker_park", _card_name(body),
                         lambda now: self._worker_park(principal, body, now))

    def _worker_park(self, principal, body, now):
        _check_body(body, _PARK_KEYS, "worker_park")
        if "expected_state" in body and body["expected_state"] is None:
            raise Refused("validation", "expected_state must be a state or non-empty list")
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
            self._exec_expect_state(item, body.get("expected_state"))
            self._park_guard(principal, item, epoch, now)
            # Claimed parks retain holder/lease-guarded claim release even when
            # exec-fenced; claimed is intentionally outside the forward table.
            fenced = item["exec_run_id"] is not None or any(
                body.get(name) is not None
                for name in ("workflow_id", "run_id", "attempt"))
            if fenced and item["state"] != "claimed" and not _forward(item["state"], "parked"):
                raise Refused("bad_transition",
                              f"{item['key']}: {item['state']} -> parked is not a forward move")
            self._exec_gate(item["key"], body, record=item["state"] != "parked")

        result = self._apply_state(principal, key, "parked", why, now,
                                   lambda project: self._may_act_in(principal,
                                                                    project),
                                   guard=guard,
                                   release_claim=True)
        return {"card": key, "state": "parked", "changed": result["changed"]}

    # ---------------------------------------------------------- complete
    def worker_complete(self, principal, body):
        return self._run(principal, "worker_complete", _card_name(body),
                         lambda now: self._worker_complete(principal, body, now))

    def _worker_complete(self, principal, body, now):
        _check_body(body, _COMPLETE_KEYS, "worker_complete")
        if "expected_state" in body and body["expected_state"] is None:
            raise Refused("validation", "expected_state must be a state or non-empty list")
        card = _text("card", body.get("card"), required=True)
        merged_sha = body.get("merged_sha")
        if not isinstance(merged_sha, str) or not _SHA_RE.fullmatch(merged_sha):
            raise Refused("validation",
                          "merged_sha must be 7 to 64 hex characters")
        key = self._resolve_scoped(principal, card)
        with self._write():
            item = self._get_or_refuse(key)
            self._exec_expect_state(item, body.get("expected_state"))
            self._exec_gate(key, body, record=item["state"] != "done")
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
            existing = self.conn.execute(
                "SELECT 1 FROM projector_alarms WHERE kind='worker_alarm'"
                " AND workflow_id='' AND key=? AND cleared_at IS NULL",
                (key,)).fetchone()
            self._exec_gate(key, body, record=existing is None)
            self.conn.execute(
                "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,"
                "raised_at)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms),"
                "'worker_alarm','',?,?,?) "
                "ON CONFLICT (kind, workflow_id, key) WHERE cleared_at IS NULL"
                " DO NOTHING",
                (key, reason, self._iso(now)))
        return {"card": key, "kind": "worker_alarm"}

    # -------------------------------------------------------- merge locks
    # A lease lock: expiry is what makes it safe, the token is only an
    # advisory generation that grows on each (re)acquisition.  The keeper
    # never fences writes store-side: a stale writer's later writes are not
    # rejected.
    # Owners are bound to the calling principal and stored as
    # ``<escaped principal>:<owner>``, with ``%`` and ``:`` in the principal
    # name percent-escaped so the first ``:`` always ends the principal
    # segment.
    def lock_acquire(self, principal, repo, body):
        """Acquire or renew the lease lock for *repo*; returns the owner string
        (already principal-prefixed) and the advisory generation token."""
        return self._run(principal, "lock_acquire", None,
                         lambda now: self._lock_acquire(principal, repo, body,
                                                        now))

    def _lock_acquire(self, principal, repo, body, now):
        self._require_role(principal, *_LOCK_ROLES)
        repo = _text("repo", repo, required=True, cap=512)
        _check_body(body, _ACQUIRE_KEYS, "lock_acquire")
        owner = _text("owner", body.get("owner"), required=True, cap=256)
        owner = _lock_owner(principal, owner)
        if len(owner) > _OWNER_MAX:
            raise Refused("validation",
                          f"owner (with the principal prefix) exceeds {_OWNER_MAX} characters")
        ttl = body.get("ttl_s")
        if (isinstance(ttl, bool) or not isinstance(ttl, int)
                or not 1 <= ttl <= 86400):
            raise Refused("validation", "ttl_s must be an integer in 1..86400")
        t = int(now)
        with self._write():
            row = self.conn.execute(
                "SELECT token, owner, expires_at FROM merge_locks WHERE repo=?",
                (repo,)).fetchone()
            if row is None:
                token = 1
                self.conn.execute(
                    "INSERT INTO merge_locks(repo,token,owner,expires_at)"
                    " VALUES(?,?,?,?)", (repo, token, owner, t + ttl))
            elif (row["owner"] != "" and row["expires_at"] > t
                  and row["owner"] != owner):
                raise Refused("conflict",
                              "merge lock for repo is held by another owner")
            elif row["owner"] != "" and row["expires_at"] > t:
                token = row["token"]
                self.conn.execute(
                    "UPDATE merge_locks SET expires_at=? WHERE repo=?",
                    (t + ttl, repo))
            else:
                token = row["token"] + 1
                self.conn.execute(
                    "UPDATE merge_locks SET token=?, owner=?, expires_at=?"
                    " WHERE repo=?", (token, owner, t + ttl, repo))
        return {"token": token, "owner": owner}

    def lock_release(self, principal, repo, body):
        """Release the lease lock; *body* carries the owner string and the
        advisory generation token that ``lock_acquire`` returned."""
        return self._run(principal, "lock_release", None,
                         lambda now: self._lock_release(principal, repo, body,
                                                        now))

    def _lock_release(self, principal, repo, body, now):
        self._require_role(principal, *_LOCK_ROLES)
        repo = _text("repo", repo, required=True, cap=512)
        _check_body(body, _RELEASE_KEYS, "lock_release")
        owner = _text("owner", body.get("owner"), required=True,
                      cap=_OWNER_MAX)
        segment, sep, _rest = owner.partition(":")
        if not sep or segment != _escape_principal_name(principal.name):
            raise Refused("conflict", "merge lock is held by another principal")
        token = body.get("token")
        if isinstance(token, bool) or not isinstance(token, int):
            raise Refused("validation", "token must be an integer")
        t = int(now)
        with self._write():
            row = self.conn.execute(
                "SELECT token, owner, expires_at FROM merge_locks WHERE repo=?",
                (repo,)).fetchone()
            if (row is None or row["owner"] == "" or row["expires_at"] <= t
                    or row["token"] != token or row["owner"] != owner):
                raise Refused("conflict", "merge lock is not held by this token")
            self.conn.execute(
                "UPDATE merge_locks SET owner='', expires_at=0 WHERE repo=?"
                " AND token=?", (repo, token))
        return {}
