"""RT-OVERSIGHT-KEEPER-SCOPE: the oversight token lifecycle and audit.

``mint_oversight_token`` is a local admin operation (router ruling R2: never an
HTTP/MCP verb).  It refuses unless BOTH halves of the router GO are present:
``PG_OVERSIGHT_LIVE=1`` on this host and a ``--go D-NNN`` naming a ``decisions``
row with ``kind='operator'`` and the whole ``oversight-live`` scope in its
``waives`` list.  The default deployment therefore has no live token at all.

Minting revokes every live token of the principal and inserts the next
generation in one transaction; only the sha256 is stored and the plaintext is
returned exactly once.  ``OVERSIGHT_TOKEN_TTL_S`` is absolute (no extension on
use) and rotation is simply a new mint.  The mint's two phases do not trust
each other: phase 2 re-checks the live env, the GO decision row and the TTL
bound, and only commits a digest phase 1 registered in this process.

Every use re-reads the token row (``_check_oversight_principal``): revoked,
expired, a generation mismatch or a scope drift is a 403 before any write.
The principal's ``active=0`` revoke is already enforced by
``Store._check_principal``.

Audit rows are written by the store: applied rows inside the batch transaction
(so an audit row exists iff the mutation exists) and denied rows after the
rollback (so a denial survives the batch) -- a denied ``dry_run`` batch audits
its denial too (only *applied* rows are dry-run-proof).  *Every* refusal an
oversight principal earns is audited (including an expired token whose row
resolves to a principal).  Unknown tokens stay unaudited, while expired tokens
are audited under their own hourly bucket.  The op-denied budget counts only
policy verdicts (``oversight:<reason>``) and store machinery refusals (cycle,
bad_state, validation, conflict, ...) -- so the denied budget counts them all.
Every writer consults that budget before inserting, so past the cap the answer
is ``oversight:rate_limited`` and the append-only table stops growing.
Crossing the derived hourly caps is alarm-only; only an explicit ``pg admin
revoke`` kills the identity (R4).  A denial opens ONE ``oversight_denied``
alarm per principal (the key is the principal, not the op's finding id).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from collections import OrderedDict

from . import auth, events
from .credential_guard import guard, mask as _mask_credentials
from .errors import Refused

__all__ = ["OversightMixin", "OVERSIGHT_TOKEN_TTL_S",
           "OVERSIGHT_PREFIX", "GRANT_SCOPE", "grants"]

OVERSIGHT_PREFIX = auth.OVERSIGHT_PREFIX
OVERSIGHT_TOKEN_TTL_S = 3600
GRANT_SCOPE = "oversight-live"
_REASON_CAP = 200
_FINDING_CAP = 120
#: a denial's message is store-generated, but it is still bounded and masked.
_DENIAL_TEXT_CAP = 500
_REASON_RE = re.compile(r"oversight:([a-z_]+)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
#: the ``op <n> <name> <key>: `` prefix ``store_ops._fail`` puts on a refusal.
_OP_FAIL_RE = re.compile(r"^op (\d+) (\S+) (\S+): ")
#: a plaintext oversight token must never reach the audit ledger.
_TOKEN_RE = re.compile(re.escape(OVERSIGHT_PREFIX) + r"[A-Za-z0-9_-]{8,}")

#: ``digest -> {name, digest, token_sha, expires, go_decision, reason}`` for the
#: prepares THIS process validated and has not committed yet.  The two-phase
#: mint (``prepare`` -> token file -> ``commit``) runs in one process -- the
#: admin CLI keeps it -- so ``commit_oversight_token`` can prove the dict it was
#: handed came from a prepare that passed every check, instead of trusting it.
#: Bounded: a prepare whose commit never happens ages out.
_PREPARED: "OrderedDict[str, dict]" = OrderedDict()
_PREPARED_CAP = 64


def _audit_text(value):
    """Bounded, control-free audit text with secrets and tokens masked."""
    text = "" if value is None else str(value)
    text = _CONTROL_RE.sub(" ", text)
    text = _TOKEN_RE.sub("[redacted]", text)
    text, _ = _mask_credentials(text)
    return text[:_DENIAL_TEXT_CAP]


def _waives(row) -> list:
    try:
        values = json.loads(row["waives"] or "[]")
    except (ValueError, TypeError):
        return []
    return values if isinstance(values, list) else []


def _remember_prepared(digest, record) -> None:
    """Register one validated prepare for the commit phase (this process)."""
    _PREPARED[digest] = record
    _PREPARED.move_to_end(digest)
    while len(_PREPARED) > _PREPARED_CAP:
        _PREPARED.popitem(last=False)


def _peek_prepared(digest):
    return _PREPARED.get(digest)


def _forget_prepared(digest) -> None:
    _PREPARED.pop(digest, None)


def _commit_txn(conn):
    if getattr(conn, "isolation_level", None) is None:
        conn.execute("COMMIT")
    else:
        conn.commit()


def _rollback_txn(conn):
    try:
        if getattr(conn, "isolation_level", None) is None:
            conn.execute("ROLLBACK")
        else:
            conn.rollback()
    except Exception:
        pass


def audit_oversight_expired_auth(conn, principal_name, token_id, generation, now):
    """Audit an expired oversight token at authenticate; unknown tokens stay unaudited.

    Consults the hourly expired-token denied budget first: past the cap this raises
    ``oversight:rate_limited`` and writes nothing to the append-only table.
    """
    from datetime import datetime, timezone
    from . import oversight_policy as policy

    since_iso = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat()
    now_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM oversight_ops WHERE principal=?"
            " AND code='oversight:token_expired' AND ts>?",
            (principal_name, since_iso)
        ).fetchone()[0]
        if count >= policy.OVERSIGHT_DENIED_PER_HOUR:
            _rollback_txn(conn)
            raise Refused("forbidden",
                          "oversight:rate_limited denied budget for this hour"
                          " is exhausted")
        conn.execute(
            "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
            "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
            "?,?,?,?,?,?,?,?,?,?,'denied',?,NULL)",
            (now_iso, token_id, principal_name, generation, "", 1,
             "authenticate", None, "",
             "oversight:token_expired token has expired",
             "oversight:token_expired"))
        detail = (f"oversight:token_expired principal={principal_name}"
                  f" gen={generation} op=authenticate"
                  f" finding=")
        conn.execute(
            "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,raised_at)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms),"
            "'oversight_denied','oversight-guard',?,?,?)"
            " ON CONFLICT (kind, workflow_id, key) WHERE cleared_at IS NULL"
            " DO NOTHING",
            (principal_name, detail, now_iso))
        _commit_txn(conn)
    except BaseException:
        _rollback_txn(conn)
        raise


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
        return self.commit_oversight_token(self.prepare_oversight_token(
            name, go_decision=go_decision, reason=reason))

    def prepare_oversight_token(self, name, *, go_decision=None, reason=""):
        """Phase 1: validate and generate token WITHOUT DB changes.

        build-L1 LOW: allows the caller to write the token file atomically
        BEFORE any DB operations (revoke old tokens, insert new token).
        Returns the token info needed for the file write and the subsequent
        commit_oversight_token call.
        """
        now = self.now()
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
        expires = now + float(self.oversight_token_ttl)
        # The commit phase re-validates everything validated here, and it can
        # only do that when it recognises this prepare's fingerprint.
        _remember_prepared(digest, {"name": name, "digest": digest,
                                    "token_sha": digest, "expires": expires,
                                    "go_decision": go_decision,
                                    "reason": reason})
        return {"token": token, "digest": digest,
                "expires": expires, "now": now, "go_decision": go_decision,
                "reason": reason, "name": name}

    def commit_oversight_token(self, prepared):
        """Phase 2: revoke old tokens and insert the new one (after file write).

        build-L1 LOW: called AFTER the token file has been written atomically.
        If this fails, the file exists but the DB has no new token (the old
        tokens are still valid).

        D-2458: phase 2 never trusts ``prepared``.  It repeats every check
        phase 1 made -- the live env half of the router GO, the ``decisions``
        row behind ``--go``, an expiry that is neither dead nor longer than
        ``OVERSIGHT_TOKEN_TTL_S`` -- and it only mints a digest this process
        actually prepared, so a forged dict cannot mint a ten-year token under
        an invented GO.
        """
        if not isinstance(prepared, dict):
            raise Refused("validation",
                          "oversight:prepared_denied a prepared mint is required")
        try:
            name = prepared["name"]
            digest = prepared["digest"]
            expires = float(prepared["expires"])
            go_decision = prepared["go_decision"]
            reason = prepared["reason"]
            token = prepared["token"]
        except (KeyError, TypeError, ValueError):
            raise Refused("validation",
                          "oversight:prepared_denied a prepared mint is required") from None
        if not isinstance(name, str) or not isinstance(token, str) \
                or not isinstance(reason, str):
            raise Refused("validation",
                          "oversight:prepared_denied a prepared mint is required")
        if os.environ.get("PG_OVERSIGHT_LIVE") != "1":
            raise Refused("forbidden",
                          "oversight:not_live PG_OVERSIGHT_LIVE is not set on this host")
        with self._write():
            decisions = getattr(self.ctx, "decisions", None)
            if not isinstance(go_decision, str) or not grants(self.conn, go_decision,
                                                              GRANT_SCOPE, decisions):
                raise Refused(
                    "forbidden",
                    f"oversight:go_required {go_decision!r} is not an operator"
                    f" decision granting {GRANT_SCOPE}")
            remembered = _peek_prepared(digest)
            if remembered is None or any(
                    remembered.get(field) != value for field, value in (
                        ("name", name), ("token_sha", auth.hash_token(token)),
                        ("expires", expires), ("go_decision", go_decision),
                        ("reason", reason))):
                raise Refused("forbidden",
                              "oversight:prepared_denied prepared mint does not match"
                              " a prepare made by this process")
            now = self.now()
            if not now < expires <= now + float(self.oversight_token_ttl):
                raise Refused("forbidden",
                              "oversight:token_invalid prepared expiry is outside"
                              " OVERSIGHT_TOKEN_TTL_S")
            fresh = self.conn.execute(
                "SELECT role,projects,active FROM principals WHERE name=?",
                (name,)).fetchone()
            if fresh is None or fresh["role"] != "oversight" or not fresh["active"]:
                raise Refused("forbidden",
                              f"oversight:principal_denied {name!r} is not an active"
                              " oversight principal")
            # Consumed exactly once: check + forget inside the write lock (D-2458)
            _forget_prepared(digest)
            generation = self.conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM oversight_tokens"
                " WHERE principal=?", (name,)).fetchone()[0]
            self.conn.execute(
                "UPDATE oversight_tokens SET revoked_at=?"
                " WHERE principal=? AND revoked_at IS NULL",
                (self._iso(now), name))
            token_id = self.conn.execute(
                "INSERT INTO oversight_tokens(id,token_sha256,principal,generation,"
                "scope,minted_by,go_decision,mint_reason,created_at,expires_at)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_tokens),"
                "?,?,?,?,?,?,?,?,?) RETURNING id",
                (digest, name, generation, "plan-write", "router", go_decision,
                 reason, self._iso(now), self._iso(expires))).fetchone()[0]
            events.append_event(
                self.conn, self._iso(now), name, "oversight", None, None,
                "oversight_token_minted",
                {"token_id": token_id, "token_sha256": digest,
                 "generation": generation, "expires_at": self._iso(expires),
                 "go_decision": go_decision})
        return {"token": prepared["token"], "generation": generation,
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
    def _oversight_count(self, principal, outcome, now):
        """*outcome* rows of this principal in the last hour: no counter table.

        ``oversight_ops`` IS the ledger (R4), so every seam that writes a row
        consults exactly this before writing.  Expired-auth denials have their
        own separate hourly bucket and never count toward the op-denied budget.
        """
        since = self._iso(now - 3600)
        if outcome == "denied":
            return self.conn.execute(
                "SELECT COUNT(*) FROM oversight_ops WHERE principal=?"
                " AND outcome='denied' AND (code IS NULL OR code != 'oversight:token_expired')"
                " AND ts>?", (principal.name, since)
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM oversight_ops WHERE principal=?"
            " AND outcome=? AND ts>?", (principal.name, outcome, since)
        ).fetchone()[0]

    def _deny_within_budget(self, principal, now):
        """Write gate for one DENIED row: the denied cap, nothing else.

        An ``applied``-cap refusal is still a denial and must be recorded (with
        its reason) like any other; only the denied cap bounds denial ROWS --
        past it this raises ``oversight:rate_limited`` and nothing is written,
        so denied-target probing stops growing the append-only table.
        """
        from . import oversight_policy as policy

        if self._oversight_count(principal, "denied", now) >= \
                policy.OVERSIGHT_DENIED_PER_HOUR:
            raise Refused("forbidden",
                          "oversight:rate_limited denied budget for this hour"
                          " is exhausted")

    def _oversight_budget(self, principal, now):
        """Refuse when the derived hourly applied/denied caps are reached.

        No counter table: ``oversight_ops`` is the ledger.  A breach is
        alarm-only (R4); this method never revokes anything.  It is called at
        every seam that would WRITE a row (the batch entry, each applied row,
        and both denial writers), so the append-only table cannot be filled by
        probing and the caller always sees ``oversight:rate_limited``.
        """
        from . import oversight_policy as policy

        for outcome, cap in (("applied", policy.OVERSIGHT_APPLIED_PER_HOUR),
                             ("denied", policy.OVERSIGHT_DENIED_PER_HOUR)):
            if self._oversight_count(principal, outcome, now) >= cap:
                self._oversight_fail = {
                    "index": 0, "op": {"op": "apply_ops"}, "key": None,
                    "reason": "rate_limited", "finding": "",
                    "input_reason": f"{outcome} budget for this hour is exhausted"}
                raise Refused("forbidden",
                              f"oversight:rate_limited {outcome} budget for this"
                              " hour is exhausted")

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
        # Re-read both caps before every row: the entry check only sees the
        # count the batch STARTED at, so one large batch could otherwise walk
        # past the applied cap.  Crossing it refuses with rate_limited (R3).
        self._oversight_budget(principal, now)
        after_seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        event_seq = after_seq if after_seq > before_seq else None
        self.conn.execute(
            "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
            "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
            "?,?,?,?,?,?,?,?,?,?,'applied',NULL,?)",
            (self._iso(now), principal.token_id, principal.name,
             principal.generation, batch_ref or "", index + 1, _op_name(op),
             oversight_policy_key(op) if oversight_policy_key(op) != "-" else None,
             meta.get("finding", ""), meta.get("reason", ""), event_seq))

    def _record_oversight_denied(self, principal, batch_ref, exc, now):
        """One denied row + one open alarm after the batch has rolled back.

        Runs for EVERY refusal an oversight principal earns, not only the
        policy's ``oversight:<reason>`` verdicts.  A refusal the shared store
        machinery raises (a cycle in ``_final_validation``, a ``bad_state``
        from a verb, a ``validation`` from the shape check, ...) has no policy
        verdict behind it, so the row names the failing op from the ``op <n>
        ...`` prefix and stores the exception's own code and message.

        The denied cap is consulted FIRST: once it is reached no further row is
        written (the append-only table stays bounded by the cap per window) and
        the caller is refused ``oversight:rate_limited`` instead (R3/R4).
        """
        with self._write():
            self._deny_within_budget(principal, now)
            msg = str(getattr(exc, "msg", "") or "")
            fail = getattr(self, "_oversight_fail", None) or {}
            index, op, key, finding, input_reason = self._denial_context(exc, fail)
            policy_reason = self._reason_from_msg(msg) if "oversight:" in msg else None
            code = getattr(exc, "code", None) or "op_denied"
            if policy_reason is not None and code == "forbidden":
                # A forbidden policy denial has no other specific code; the audit
                # keeps naming the granted-scope reason (the contract tests pin
                # this shape) and the op's own reason as its human text.  A budget
                # refusal carries no op reason, so it falls back to the refusal
                # text -- an empty reason would log nothing human-readable.
                code = f"oversight:{policy_reason}"
                stored_reason = input_reason or _audit_text(msg)
            else:
                # Everything else stores the contract code and the actual message.
                stored_reason = _audit_text(msg)
            self.conn.execute(
                "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
                "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
                "?,?,?,?,?,?,?,?,?,?,'denied',?,NULL)",
                (self._iso(now), principal.token_id, principal.name,
                 principal.generation, batch_ref or "", index + 1,
                 _op_name(op), key, finding, stored_reason, code))
            self._oversight_guard_alarm(principal, finding,
                                        policy_reason or code, _op_name(op), now)

    def _record_oversight_refusal(self, principal, action, key, exc, now):
        """Audit a refusal raised at the VERB seam, after the refusal.

        The verb/token checks (``check_action``, revoked/expired/rotated/scope)
        run before any batch exists, so there is no op context: the row names
        the attempted verb and its key, ``finding_id`` stays empty and the
        reason is the (bounded, masked) refusal text.  A refusal whose message
        carries ``oversight:<reason>`` stores that reason as its code, matching
        the policy rows; anything else stores the contract code.

        Like the batch writer it consults the denied cap FIRST: a seam
        refusal past the cap answers ``oversight:rate_limited`` and writes
        nothing, so denied-target probing is bounded whichever door it uses.
        """
        with self._write():
            self._deny_within_budget(principal, now)
            msg = str(getattr(exc, "msg", "") or "")
            policy_reason = self._reason_from_msg(msg) if "oversight:" in msg else None
            code = f"oversight:{policy_reason}" if policy_reason else \
                (getattr(exc, "code", None) or "op_denied")
            self.conn.execute(
                "INSERT INTO oversight_ops(id,ts,token_id,principal,generation,"
                "batch_ref,op_index,op,key,finding_id,reason,outcome,code,event_seq)"
                " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM oversight_ops),"
                "?,?,?,?,?,?,?,?,?,?,'denied',?,NULL)",
                (self._iso(now), principal.token_id, principal.name,
                 principal.generation, "", 1, action, key or None, "",
                 _audit_text(msg), code))
            self._oversight_guard_alarm(principal, "", policy_reason or code,
                                        action, now)

    def _oversight_guard_alarm(self, principal, finding, reason, action, now):
        """Open (or update) ONE ``oversight-guard`` alarm for this principal.

        The key is the principal, never the op-supplied ``finding``: the
        contract promises ONE open ``projector_alarms`` row, and rotating
        finding ids must not mint unbounded open alarms (routing-00 routes one
        card).  ON CONFLICT DO UPDATE refreshes the open alarm's detail to the
        latest finding + an occurrence count.
        """
        row = self.conn.execute(
            "SELECT detail FROM projector_alarms"
            " WHERE kind='oversight_denied' AND workflow_id='oversight-guard'"
            " AND key=? AND cleared_at IS NULL",
            (principal.name,)
        ).fetchone()
        count = 1
        if row is not None:
            raw_detail = row["detail"] if (isinstance(row, dict) or hasattr(row, "__getitem__")) else row[0]
            m = re.search(r"\bcount\s+(\d+)", str(raw_detail or ""))
            count = (int(m.group(1)) + 1) if m else 2

        base_detail = (f"oversight:{reason} principal={principal.name}"
                       f" gen={principal.generation} op={action}"
                       f" finding={finding}")
        detail_on_conflict = f"{base_detail} count {count}"
        self.conn.execute(
            "INSERT INTO projector_alarms(id,kind,workflow_id,key,detail,raised_at)"
            " VALUES((SELECT COALESCE(MAX(id), 0) + 1 FROM projector_alarms),"
            "'oversight_denied','oversight-guard',?,?,?)"
            " ON CONFLICT (kind, workflow_id, key) WHERE cleared_at IS NULL"
            " DO UPDATE SET detail=?",
            (principal.name, base_detail, self._iso(now), detail_on_conflict))

    def _denial_context(self, exc, fail):
        """``(index, op, key, finding, input_reason)`` for a denied batch.

        A policy verdict (``_oversight_fail``) already names them; a refusal
        from the shared machinery carries only the ``op <n> <name> <key>:``
        prefix, so the op is recovered from the batch the caller passed.
        """
        if isinstance(fail.get("op"), dict):
            op = fail["op"]
            return (int(fail.get("index") or 0), op,
                    fail.get("key") or _policy_key(op),
                    _audit_text(fail.get("finding", "")),
                    _audit_text(fail.get("input_reason", "")))
        ops = getattr(self, "_oversight_ops", None) or []
        match = _OP_FAIL_RE.match(str(getattr(exc, "msg", "") or ""))
        index = 0
        op = {"op": "apply_ops"}
        key = None
        if match is not None:
            index = max(int(match.group(1)) - 1, 0)
            raw = ops[index] if index < len(ops) else None
            op = raw if isinstance(raw, dict) else {"op": match.group(2)}
            key = None if match.group(3) == "-" else match.group(3)
        return (index, op, key or _policy_key(op),
                _audit_text(op.get("finding", "")),
                _audit_text(op.get("reason", "")))

    @staticmethod
    def _reason_from_msg(msg):
        match = _REASON_RE.search(str(msg or ""))
        return match.group(1) if match else "op_denied"


def _policy_key(op):
    """The op's key when it names one, else ``None`` (never the ``-`` marker)."""
    key = oversight_policy_key(op)
    return None if key == "-" else key


def _op_name(op):
    """The op's name as text for the audit ledger.

    A malformed name (a list/dict where a name belongs) must never reach a SQL
    parameter: it is stringified instead of raising ``InterfaceError`` while
    recording an already-denied batch.
    """
    name = op.get("op") if isinstance(op, dict) else None
    if isinstance(name, str):
        return name
    return "-" if name is None else str(name)


def oversight_policy_key(op):
    """The key an op names, without importing the policy's private helper."""
    from .oversight_policy import key_of
    return key_of(op) if isinstance(op, dict) else "-"
