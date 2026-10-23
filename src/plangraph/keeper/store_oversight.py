"""RT-OVERSIGHT-KEEPER-SCOPE: the oversight token lifecycle and audit.

``mint_oversight_token`` is a local admin operation (router ruling R2: never an
HTTP/MCP verb).  It refuses unless BOTH halves of the router GO are present:
``PG_OVERSIGHT_LIVE=1`` on this host and a ``--go D-NNN`` naming a ``decisions``
row with ``kind='operator'`` and the whole ``oversight-live`` scope in its
``waives`` list.  The default deployment therefore has no live token at all.

Minting revokes every live token of the principal and inserts the next
generation in one transaction; only the sha256 is stored and the plaintext is
returned exactly once.  ``OVERSIGHT_TOKEN_TTL_S`` is absolute (no extension on
use) and rotation is simply a new mint.

Every use re-reads the token row (``_check_oversight_principal``): revoked,
expired, a generation mismatch or a scope drift is a 403 before any write.
The principal's ``active=0`` revoke is already enforced by
``Store._check_principal``.

Audit rows are written by the store: applied rows inside the batch transaction
(so an audit row exists iff the mutation exists) and denied rows after the
rollback (so a denial survives the batch).  Crossing the derived hourly caps is
alarm-only; only an explicit ``pg admin revoke`` kills the identity (R4).
"""
from __future__ import annotations

import json
import os
import re
import secrets

from . import auth, events
from .credential_guard import guard
from .errors import Refused

__all__ = ["OversightMixin", "OVERSIGHT_TOKEN_TTL_S", "OVERSIGHT_ROTATE_AFTER_S",
           "OVERSIGHT_PREFIX", "GRANT_SCOPE", "grants"]

OVERSIGHT_PREFIX = auth.OVERSIGHT_PREFIX
OVERSIGHT_TOKEN_TTL_S = 3600
OVERSIGHT_ROTATE_AFTER_S = 3300
GRANT_SCOPE = "oversight-live"
_REASON_CAP = 200
_FINDING_CAP = 120
_REASON_RE = re.compile(r"oversight:([a-z_]+)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _waives(row) -> list:
    try:
        values = json.loads(row["waives"] or "[]")
    except (ValueError, TypeError):
        return []
    return values if isinstance(values, list) else []


def grants(conn, decision_id, scope=GRANT_SCOPE, decisions=None) -> bool:
    """Does ``decision_id`` grant *scope* (R7: table + adapter, adapter wins).

    The ``decisions`` table is the source that works in the admin CLI.  When the
    configured DECISIONS.md adapter knows the same id it is authoritative on a
    conflict; an unknown/unreadable adapter falls back to the table.
    """
    row = conn.execute(
        "SELECT kind,waives FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None or row["kind"] != "operator":
        return False
    from_table = scope in _waives(row)
    has = getattr(decisions, "has", None)
    waives = getattr(decisions, "waives", None)
    if callable(has) and callable(waives) and has(decision_id):
        return bool(waives(decision_id, scope))
    return from_table


class OversightMixin:
    """Mint/rotate oversight tokens and write their audit rows."""

    oversight_token_ttl = OVERSIGHT_TOKEN_TTL_S

    # ------------------------------------------------------------- minting
    def mint_oversight_token(self, name, *, go_decision=None, reason=""):
        return self._mint_oversight(name, go_decision, reason, self.now())

    def _mint_oversight(self, name, go_decision, reason, now):
        if os.environ.get("PG_OVERSIGHT_LIVE") != "1":
            raise Refused("forbidden",
                          "oversight:not_live PG_OVERSIGHT_LIVE is not set on this host")
        if not isinstance(go_decision, str) or not go_decision.strip():
            raise Refused("forbidden",
                          "oversight:go_required --go D-NNN is required")
        decisions = getattr(self.ctx, "decisions", None)
        if not grants(self.conn, go_decision, GRANT_SCOPE, decisions):
            raise Refused(
                "forbidden",
                f"oversight:go_required {go_decision!r} is not an operator"
                f" decision granting {GRANT_SCOPE}")
        reason = self._oversight_text("reason", reason, _REASON_CAP)
        row = self.conn.execute(
            "SELECT role,active,projects FROM principals WHERE name=?",
            (name,)).fetchone()
        if row is None or row["role"] != "oversight" or not row["active"]:
            raise Refused("forbidden",
                          f"oversight:principal_denied {name!r} is not an active"
                          " oversight principal")
        token = OVERSIGHT_PREFIX + secrets.token_urlsafe(32)
        digest = auth.hash_token(token)
        with self._write():
            fresh = self.conn.execute(
                "SELECT role,projects,active FROM principals WHERE name=?",
                (name,)).fetchone()
            if fresh is None or fresh["role"] != "oversight" or not fresh["active"]:
                raise Refused("forbidden",
                              f"oversight:principal_denied {name!r} is not an active"
                              " oversight principal")
            generation = self.conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM oversight_tokens"
                " WHERE principal=?", (name,)).fetchone()[0]
            self.conn.execute(
                "UPDATE oversight_tokens SET revoked_at=?"
                " WHERE principal=? AND revoked_at IS NULL",
                (self._iso(now), name))
            expires = now + float(self.oversight_token_ttl)
            token_id = self.conn.execute(
                "INSERT INTO oversight_tokens(id,token_sha256,principal,generation,"
                "scope,minted_by,go_decision,mint_reason,created_at,expires_at)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_tokens),"
                "?,?,?,?,?,?,?,?,?) RETURNING id",
                (digest, name, generation, "plan-write", "router", go_decision,
                 reason, self._iso(now), self._iso(expires))).fetchone()[0]
            # The plaintext never leaves this return; the chain carries only the
            # sha256/gen/expiry (precedent: token_minted).
            events.append_event(
                self.conn, self._iso(now), name, "oversight", None, None,
                "oversight_token_minted",
                {"token_id": token_id, "token_sha256": digest,
                 "generation": generation, "expires_at": self._iso(expires),
                 "go_decision": go_decision})
        return {"token": token, "generation": generation,
                "expires_at": self._iso(expires), "scope": "plan-write"}

    # ------------------------------------------------------------- per use
    def _check_oversight_principal(self, principal, action, key, now):
        row = self.conn.execute(
            "SELECT id,principal,generation,scope,expires_at,revoked_at"
            " FROM oversight_tokens WHERE id=?", (principal.token_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise Refused("forbidden", "oversight:token_revoked token is revoked")
        if row["principal"] != principal.name:
            raise Refused("forbidden", "oversight:token_denied token changed owner")
        if row["generation"] != principal.generation:
            raise Refused("forbidden",
                          "oversight:token_rotated token was rotated")
        if row["scope"] != principal.scope:
            raise Refused("forbidden", "oversight:token_denied token scope drifted")
        expires = self._parse_iso(row["expires_at"])
        if expires is None or expires.timestamp() <= now:
            raise Refused("forbidden", "oversight:token_expired token has expired")

    # ---------------------------------------------------------- rate limit
    def _oversight_budget(self, principal, now):
        """Refuse when the derived hourly applied/denied caps are reached.

        No counter table: ``oversight_ops`` is the ledger.  A breach is
        alarm-only (R4); this method never revokes anything.
        """
        from . import oversight_policy as policy

        since = self._iso(now - 3600)
        counts = {}
        for outcome in ("applied", "denied"):
            counts[outcome] = self.conn.execute(
                "SELECT COUNT(*) FROM oversight_ops WHERE principal=?"
                " AND outcome=? AND ts>?", (principal.name, outcome, since)
            ).fetchone()[0]
        if counts["applied"] >= policy.OVERSIGHT_APPLIED_PER_HOUR:
            self._oversight_fail = {
                "index": 0, "op": {"op": "apply_ops"}, "key": None,
                "reason": "rate_limited", "finding": ""}
            raise Refused("forbidden",
                          "oversight:rate_limited applied budget for this hour"
                          " is exhausted")
        if counts["denied"] >= policy.OVERSIGHT_DENIED_PER_HOUR:
            self._oversight_fail = {
                "index": 0, "op": {"op": "apply_ops"}, "key": None,
                "reason": "rate_limited", "finding": ""}
            raise Refused("forbidden",
                          "oversight:rate_limited denied budget for this hour"
                          " is exhausted")

    # ---------------------------------------------------------- audit rows
    def _oversight_meta(self, op, index):
        """Validate and return one op's (reason, finding) audit inputs.

        They are data, never authority: capped, control-character free,
        credential-guarded, stored verbatim and never consulted by the policy.
        """
        try:
            reason = self._oversight_text("reason", op.get("reason", ""),
                                          _REASON_CAP)
            finding = self._oversight_text("finding", op.get("finding", ""),
                                           _FINDING_CAP)
        except Refused as exc:
            raise Refused(exc.code, f"op {index + 1} "
                                    f"{op.get('op')} "
                                    f"{oversight_policy_key(op)}: {exc.msg}") from None
        return {"reason": reason, "finding": finding}

    @staticmethod
    def _oversight_text(name, value, cap):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise Refused("validation", f"oversight:field_denied {name} must be text")
        if len(value) > cap:
            raise Refused("validation",
                          f"oversight:field_denied {name} exceeds {cap} characters")
        if _CONTROL_RE.search(value):
            raise Refused("validation",
                          f"oversight:field_denied {name} has control characters")
        guard(name, value)
        return value

    def _record_oversight_applied(self, principal, batch_ref, index, op, meta,
                                  before_seq, now):
        after_seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        event_seq = after_seq if after_seq > before_seq else None
        self.conn.execute(
            "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
            "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
            "?,?,?,?,?,?,?,?,?,?,'applied',NULL,?)",
            (self._iso(now), principal.token_id, principal.name,
             principal.generation, batch_ref or "", index + 1, op.get("op"),
             oversight_policy_key(op) if oversight_policy_key(op) != "-" else None,
             meta.get("finding", ""), meta.get("reason", ""), event_seq))

    def _record_oversight_denied(self, principal, batch_ref, exc, now):
        """One denied row + one open alarm after the batch has rolled back."""
        if "oversight:" not in str(getattr(exc, "msg", "") or ""):
            return
        fail = getattr(self, "_oversight_fail", None) or {}
        op = fail.get("op") or {"op": "apply_ops"}
        index = fail.get("index", 0)
        key = fail.get("key") or (oversight_policy_key(op)
                                  if oversight_policy_key(op) != "-" else None)
        reason = fail.get("reason") or self._reason_from_msg(exc.msg)
        finding = fail.get("finding", "")
        code = f"oversight:{reason}"
        self.conn.execute(
            "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
            "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
            "?,?,?,?,?,?,?,?,?,?,'denied',?,NULL)",
            (self._iso(now), principal.token_id, principal.name,
             principal.generation, batch_ref or "", index + 1,
             op.get("op"), key, finding, fail.get("input_reason", ""), code))
        detail = (f"oversight:{reason} principal={principal.name}"
                  f" gen={principal.generation} op={op.get('op')}"
                  f" finding={finding}")
        self.conn.execute(
            "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,raised_at)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms),"
            "'oversight_denied','oversight-guard',?,?,?)"
            " ON CONFLICT (kind, workflow_id, key) WHERE cleared_at IS NULL"
            " DO NOTHING",
            (finding, detail, self._iso(now)))

    @staticmethod
    def _reason_from_msg(msg):
        match = _REASON_RE.search(str(msg or ""))
        return match.group(1) if match else "op_denied"


def oversight_policy_key(op):
    """The key an op names, without importing the policy's private helper."""
    from .oversight_policy import key_of
    return key_of(op) if isinstance(op, dict) else "-"
