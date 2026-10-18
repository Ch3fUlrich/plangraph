"""Worker verbs: the calls the Rust worker (rust/worker/src/keeper.rs) makes.

A worker/spawner/l1 principal is scoped to the card's project via
``self._may_act_in(principal, item["project"])`` (defined in ``CardsMixin``);
router and system principals pass anywhere.

``attempts`` rows are written ONLY by the projector, which reads Temporal
history: it numbers them ``MAX(n)+1`` and carries the cost.  ``worker_attempt``
therefore records an audit row only — inserting here would double the rows.

All SQL is portable (``?`` placeholders, ids assigned as ``MAX(id)+1`` inside
the write lock) so it runs unchanged on SQLite and Postgres.
"""
from __future__ import annotations

import re

from .errors import Refused
from .schema import EXIT_CLASSES
from .store_cards import _count, _number, _text

__all__ = ["WorkerMixin"]

_ATTEMPT_KEYS = frozenset(("card", "n", "leg", "exit_class", "diff_hash",
                           "check_tail", "cost", "tokens_in", "tokens_out"))
_PARK_KEYS = frozenset(("card", "tier", "exit_class", "check_tail"))
_COMPLETE_KEYS = frozenset(("card", "merged_sha"))
_ALARM_KEYS = frozenset(("card", "reason"))
_ACQUIRE_KEYS = frozenset(("owner", "ttl_s"))
_RELEASE_KEYS = frozenset(("token", "owner"))
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_LOCK_ROLES = ("router", "system", "l1", "worker", "spawner")


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


class WorkerMixin:
    """The worker-facing verbs for :class:`plangraph.keeper.store.Store`."""

    # ------------------------------------------------------------- helpers
    def _resolve_scoped(self, principal, card):
        """Resolve *card* to its live key, refuse if unknown, check scope."""
        key = self.resolve_key(card)
        item = self._get_or_refuse(key)
        self._may_act_in(principal, item["project"])
        return key

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
        return {"card": key, "n": number, "exit_class": exit_class}

    # -------------------------------------------------------------- park
    def worker_park(self, principal, body):
        return self._run(principal, "worker_park", _card_name(body),
                         lambda now: self._worker_park(principal, body, now))

    def _worker_park(self, principal, body, now):
        _check_body(body, _PARK_KEYS, "worker_park")
        card = _text("card", body.get("card"), required=True)
        tier = _text("tier", body.get("tier"), key=card) or ""
        exit_class = body.get("exit_class")
        if exit_class not in EXIT_CLASSES:
            raise Refused("validation",
                          f"exit_class must be one of {list(EXIT_CLASSES)}")
        _strings(body, "check_tail")   # not stored: type only (a tail can be long)
        key = self._resolve_scoped(principal, card)
        why = f"parked: {exit_class} ({tier})"[:500]
        result = self._apply_state(principal, key, "parked", why, now,
                                   lambda project: self._may_act_in(principal,
                                                                    project))
        return {"card": key, "state": "parked", "changed": result["changed"]}

    # ---------------------------------------------------------- complete
    def worker_complete(self, principal, body):
        return self._run(principal, "worker_complete", _card_name(body),
                         lambda now: self._worker_complete(principal, body, now))

    def _worker_complete(self, principal, body, now):
        _check_body(body, _COMPLETE_KEYS, "worker_complete")
        card = _text("card", body.get("card"), required=True)
        merged_sha = body.get("merged_sha")
        if not isinstance(merged_sha, str) or not _SHA_RE.fullmatch(merged_sha):
            raise Refused("validation",
                          "merged_sha must be 7 to 64 hex characters")
        key = self._resolve_scoped(principal, card)
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
    def lock_acquire(self, principal, repo, body):
        return self._run(principal, "lock_acquire", None,
                         lambda now: self._lock_acquire(principal, repo, body,
                                                        now))

    def _lock_acquire(self, principal, repo, body, now):
        self._require_role(principal, *_LOCK_ROLES)
        repo = _text("repo", repo, required=True, cap=512)
        _check_body(body, _ACQUIRE_KEYS, "lock_acquire")
        owner = _text("owner", body.get("owner"), required=True, cap=256)
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
                raise Refused("conflict", "merge lock for repo is held")
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
        return self._run(principal, "lock_release", None,
                         lambda now: self._lock_release(principal, repo, body,
                                                        now))

    def _lock_release(self, principal, repo, body, now):
        self._require_role(principal, *_LOCK_ROLES)
        repo = _text("repo", repo, required=True, cap=512)
        _check_body(body, _RELEASE_KEYS, "lock_release")
        owner = _text("owner", body.get("owner"), required=True, cap=256)
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
