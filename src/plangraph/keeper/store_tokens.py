"""M2B-RUN-TOKENS: keeper-issued run tokens for :class:`plangraph.keeper.store.Store`.

``mint_run_token(key, role, run_id)`` is the one way a run token comes to exist:

* caller is the router or an l1 of the item's project (a worker, a reviewer, a
  run token or an l1 of another project cannot mint);
* ``writer`` needs the item ``claimed``, ``reviewer`` needs it ``in_review``;
* the run id must exist in the autoos run record (``ctx.runs``, the adapter
  ``run-record`` evidence reads) and its ``job.json`` must name the item -- the
  caller supplies a run id, never claims about it;
* only the sha256 of the token is stored, with key, epoch, role, run id and
  ``expires_at = min(now + ttl, lease_until)``; the plaintext is returned once;
* the ``token_minted`` event carries no token value.

A run token authenticates as :class:`auth.RunPrincipal`.  Every use is checked
afresh against the live item (``_check_run_principal``): not revoked, not
expired, same key, same epoch, the item still in the state the role needs, and
the verb one the role may call.  Revocation is written in the same transaction
as the event that makes a token stale (``_revoke_for_event``, called from
``_append``): submit, verdict, release, lease expiry, cancel, close.
"""
from __future__ import annotations

import secrets

from . import auth
from .errors import Refused

__all__ = ["TokensMixin", "RUN_VERBS", "TOKEN_PREFIX"]

TOKEN_PREFIX = "pgr1_"
RUN_TOKEN_TTL_S = 7200
RUN_TOKEN_MAX_LIFE_S = 86400
_MAX_LIVE_PER_ITEM = 16
_ROLES = ("writer", "reviewer")

#: The only verbs each token role may call, and the item state it needs.
RUN_VERBS = {"writer": ("heartbeat", "add_evidence", "submit"),
             "reviewer": ("verdict",)}
_STATE = {"writer": "claimed", "reviewer": "in_review"}

#: Events after which every live token of the item is stale.
_REVOKE_ALL = frozenset({
    "claimed", "released", "lease_expired", "cancelled", "review_passed",
    "closed_on_decision", "answered", "changes_requested", "legacy_state_set"})


class TokensMixin:
    """``mint_run_token`` plus the per-use checks and revocation."""

    run_token_ttl = RUN_TOKEN_TTL_S

    # -------------------------------------------------------------- mint
    def mint_run_token(self, principal, key, role, run_id):
        return self._run(principal, "mint_run_token", key,
                         lambda now: self._mint(principal, key, role, run_id,
                                                now))

    def _run_family(self, job):
        """The run's own family marker from its record; unknown fails closed."""
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        try:
            return auth.validate_family(request.get("family"))
        except Refused:
            return None

    def _check_run(self, item, run_id, now):
        """The run exists in the run record and its job names this item."""
        runs = self._ctx(item, now).runs
        if runs is None:
            raise Refused("evidence_unverified", "run record is not configured")
        if not isinstance(run_id, str) or not runs.valid_run_id(run_id):
            raise Refused("validation", "invalid run id")
        try:
            job = runs.job(run_id)
        except FileNotFoundError:
            raise Refused("evidence_unverified",
                          f"run {run_id} is not in the run record") from None
        except Exception:  # noqa: BLE001 - unreadable record fails closed
            raise Refused("evidence_unverified",
                          f"could not read run {run_id}") from None
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        haystack = " ".join(str(part) for part in (request.get("title"),
                                                   job.get("task"))
                            if part is not None)
        needles = [n for n in (item.get("key"), item.get("id"))
                   if isinstance(n, str) and n]
        if not any(needle in haystack for needle in needles):
            raise Refused("evidence_unverified",
                          f"run {run_id} does not mention {item['key']}")
        return job

    def _mint(self, principal, key, role, run_id, now):
        self._require_role(principal, "router", "l1")
        if role not in _ROLES:
            raise Refused("validation", "role must be writer or reviewer")
        item = self._get_or_refuse(key)
        self._may_manage(principal, item["project"])
        self._assert_unlocked(item)
        self._mintable(item, role)
        job = self._check_run(item, run_id, now)
        with self._write():
            fresh = self._get_or_refuse(key)
            self._assert_unlocked(fresh)
            self._mintable(fresh, role)
            if int(fresh["epoch"]) != int(item["epoch"]):
                raise Refused("conflict", f"{key} changed under the transaction")
            expires = self._token_expiry(fresh, role, now)
            epoch = int(fresh["epoch"])
            self.conn.execute(
                "UPDATE run_tokens SET revoked_at=? WHERE key=? AND epoch=?"
                " AND role=? AND run_id=? AND revoked_at IS NULL",
                (self._iso(now), key, epoch, role, run_id))
            live = self.conn.execute(
                "SELECT COUNT(*) FROM run_tokens WHERE key=?"
                " AND revoked_at IS NULL AND expires_at>?",
                (key, self._iso(now))).fetchone()[0]
            if live >= _MAX_LIVE_PER_ITEM:
                raise Refused("conflict", f"{key} has too many live run tokens")
            token = TOKEN_PREFIX + secrets.token_urlsafe(32)
            family = self._run_family(job) if role == "reviewer" else None
            cursor = self.conn.execute(
                "INSERT INTO run_tokens(token_sha256,key,project,epoch,role,"
                "run_id,family,minted_by,created_at,expires_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (auth.hash_token(token), key, fresh["project"], epoch, role,
                 run_id, family, principal.name, self._iso(now),
                 self._iso(expires)))
            self._append(principal, key, "token_minted",
                         {"token_id": cursor.lastrowid, "role": role,
                          "run_id": run_id, "epoch": epoch,
                          "expires_at": self._iso(expires)}, now)
        return {"key": key, "role": role, "run_id": run_id, "epoch": epoch,
                "expires_at": self._iso(expires), "token": token}

    @staticmethod
    def _mintable(item, role):
        if item["state"] != _STATE[role]:
            raise Refused("bad_state",
                          f"{item['key']} is {item['state']}, not {_STATE[role]}")

    def _token_expiry(self, item, role, now, created=None):
        """``min(now + ttl, lease_until)`` (reviewer: the review window)."""
        limit = now + float(self.run_token_ttl)
        if role == "reviewer":
            limit = min(limit, now + float(self.review_overdue_s))
        else:
            lease = self._parse_iso(item.get("lease_until"))
            if lease is None or lease.timestamp() <= now:
                raise Refused("bad_state", f"{item['key']} has no live lease")
            limit = min(limit, lease.timestamp())
        if created is not None:
            limit = min(limit, created + RUN_TOKEN_MAX_LIFE_S)
        return limit

    # ----------------------------------------------------------- per use
    def _check_run_principal(self, principal, action, key, now):
        """Refuse (403) unless this run token may do *action* on *key* now."""
        row = self.conn.execute(
            "SELECT key,epoch,role,run_id,expires_at,revoked_at FROM run_tokens"
            " WHERE id=?", (principal.token_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise Refused("forbidden", "run token is revoked")
        expires = self._parse_iso(row["expires_at"])
        if expires is None or expires.timestamp() <= now:
            raise Refused("forbidden", "run token has expired")
        if (row["role"] != principal.token_role or row["key"] != principal.key
                or row["epoch"] != principal.epoch):
            raise Refused("forbidden", "run token changed since authentication")
        if action not in RUN_VERBS.get(row["role"], ()):
            raise Refused("forbidden", f"a run token may not {action}")
        if key != row["key"]:
            raise Refused("forbidden", "run token is bound to another item")
        item = self._item(key)
        if item is None or int(item["epoch"]) != int(row["epoch"]):
            raise Refused("forbidden", "run token epoch is stale")
        if item["state"] != _STATE[row["role"]]:
            raise Refused("forbidden", f"{key} is {item['state']}: token unusable")

    def _extend_run_token(self, principal, item, now):
        """A heartbeat through a writer token renews it within the new lease."""
        row = self.conn.execute(
            "SELECT created_at FROM run_tokens WHERE id=?",
            (principal.token_id,)).fetchone()
        created = self._parse_iso(row["created_at"]) if row else None
        if created is None:
            return
        expires = self._token_expiry(item, "writer", now, created.timestamp())
        self.conn.execute(
            "UPDATE run_tokens SET expires_at=? WHERE id=? AND revoked_at IS NULL",
            (self._iso(expires), principal.token_id))

    # -------------------------------------------------------- revocation
    def _revoke_for_event(self, principal, key, type, now):
        """Revoke the tokens an event makes stale, in the event's transaction."""
        if key is None:
            return
        token_id = getattr(principal, "token_id", None)
        if type in _REVOKE_ALL:
            where, args = "key=?", (key,)
        elif type == "submitted":
            where, args = "key=? AND role='writer'", (key,)
        elif type == "review_verdict" and token_id is not None:
            where, args = "id=?", (token_id,)
        else:
            return
        self.conn.execute(
            f"UPDATE run_tokens SET revoked_at=? WHERE {where}"
            " AND revoked_at IS NULL", (self._iso(now),) + args)
