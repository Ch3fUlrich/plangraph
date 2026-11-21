"""Transactional write receipts and keeper outage grace.

``record_outage(gap_s, frm, to)`` (system, at keeper start) freezes the clocks
for the outage: the leases of claimed items and the expiry of live run tokens
whose deadline fell after ``frm`` are pushed out by the gap, in one transaction
with an ``outage_recorded`` event, so a token that expired during the outage is
honoured for the outage duration (PROPOSAL §3) and ``verify_integrity`` replays it.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
import re

from . import auth
from .errors import Refused

__all__ = ["OutboxMixin", "MAX_ENTRIES"]

MAX_ENTRIES = 100
#: Bound the number of new durable outcomes a single run token can create.
MAX_SEEN_PER_TOKEN = 1000
TOKEN_GRACE_MAX_S = 86400      # total outage grace one run token can ever receive
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


RECEIPT_VERBS = frozenset({"heartbeat", "add_evidence", "submit"})


class _StoredReceipt(Exception):
    def __init__(self, result):
        self.result = result


class OutboxMixin:
    """``replay`` for run tokens and ``record_outage`` for the daemon."""

    def replay(self, principal, entries):
        """Compatibility for old clients: expose history, without sending an uncertain write."""
        self._receipt_principal(principal)
        if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
            raise Refused("validation", f"entries must be a list of 1..{MAX_ENTRIES}")
        results, recovery = [], []
        for entry in entries:
            try:
                eid, verb, body, digest = self._receipt_entry(entry)
                result = self._find_receipt(eid, principal.token_id, verb, digest)
                if result is not None:
                    results.append(result)
                else:
                    recovery.append({"id": eid, "result": "recovery_required"})
            except Refused as exc:
                results.append({"id": entry.get("id") if isinstance(entry, dict) else None,
                                "result": exc.code})
        return {"results": results, "recovery": recovery}

    def write_context(self, principal, key):
        """Return the epoch and source round of the current writer claim."""
        self._receipt_principal(principal)
        if key is None:
            key = principal.key
        self._check_run_principal(principal, "submit", key, self.now())
        item = self._get_or_refuse(key)
        return {"key": key, "epoch": item["epoch"], "source_round": item["round"]}

    @staticmethod
    def _receipt_principal(principal):
        if not isinstance(principal, auth.RunPrincipal):
            raise Refused("forbidden", "only a run token may use write receipts")

    @staticmethod
    def _receipt_entry(entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) \
                or not _ID_RE.fullmatch(entry["id"]):
            raise Refused("validation", "invalid invocation id")
        eid, verb, body = entry["id"], entry.get("verb"), entry.get("body")
        if not isinstance(verb, str) or not isinstance(body, dict):
            raise Refused("validation", "invalid receipt verb or body")
        canonical = json.dumps([verb, body], sort_keys=True, separators=(",", ":"))
        return eid, verb, body, hashlib.sha256(canonical.encode()).hexdigest()

    def _find_receipt(self, eid, token_id, verb, digest):
        row = self.conn.execute(
            "SELECT token_id,verb,request_hash,result FROM op_receipts WHERE entry_id=?",
            (eid,)).fetchone()
        if row is None:
            # A copy from an older SQLite database can populate this table after migration.
            row = self.conn.execute(
                "SELECT token_id,verb,NULL AS request_hash,result FROM outbox_seen WHERE entry_id=?",
                (eid,)).fetchone()
        if row is None:
            return None
        if row["token_id"] != token_id:
            raise Refused("forbidden", "receipt belongs to another credential")
        if row["verb"] != verb or row["request_hash"] not in (None, digest):
            raise Refused("conflict", "invocation already bound to another request")
        if row["request_hash"] is None:
            return {"id": eid, "result": "duplicate", "first": row["result"], "legacy": True}
        stored = json.loads(row["result"])
        return {"id": eid, **stored, "first": stored["result"], "result": "duplicate"}

    def read_receipt_for_bearer(self, bearer, entry):
        """A revoked bearer can retrieve an exact outcome without regaining write authority."""
        if not bearer:
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) \
                or not _ID_RE.fullmatch(entry["id"]):
            return None
        token = self.conn.execute(
            "SELECT id FROM run_tokens WHERE token_sha256=?",
            (auth.hash_token(bearer),)).fetchone()
        if token is None:
            return None
        eid, verb, _, digest = self._receipt_entry(entry)
        return self._find_receipt(eid, token["id"], verb, digest)

    def write_receipt(self, principal, entry):
        self._receipt_principal(principal)
        eid, verb, body, digest = self._receipt_entry(entry)
        stored = self._find_receipt(eid, principal.token_id, verb, digest)
        if stored is not None:
            return stored
        if entry.get("legacy") is True:
            return {"id": eid, "result": "recovery_required"}
        if verb not in RECEIPT_VERBS:
            raise Refused("forbidden", f"{verb!r} cannot use write receipts")
        key = body.get("key")
        if not isinstance(key, str):
            raise Refused("validation", "receipt write requires a key")
        key = self.resolve_key(key)
        context = (eid, principal.token_id, verb, digest, key)
        try:
            try:
                result = self._receipt_call(principal, verb, key, body, context)
            except Refused as exc:
                if exc.code == "unavailable" or exc.msg == "keeper database is busy":
                    raise Refused("unavailable", exc.msg) from exc
                outcome = {"result": exc.code, "msg": exc.msg}
                with self._receipt_transaction(context) as record:
                    record(outcome)
                return {"id": eid, **outcome}
        except _StoredReceipt as settled:
            return settled.result
        return {"id": eid, "result": "ok", "reply": result}

    def _receipt_call(self, principal, verb, key, body, context):
        if verb == "heartbeat":
            return self.heartbeat(principal, key, body.get("epoch"), receipt=context)
        if verb == "add_evidence":
            return self.add_evidence(principal, key, body.get("epoch"), body.get("kind"),
                                     body.get("ref") or {}, receipt=context)
        source_round = body.get("source_round")
        if type(source_round) is not int or source_round < 0:
            raise Refused("validation", "submit receipt requires a nonnegative source_round")
        return self.submit(principal, key, body.get("epoch"), source_round=source_round,
                           receipt=context)

    @contextmanager
    def _receipt_transaction(self, context):
        """Use the verb's transaction for the mutation and its terminal result."""
        with self._write():
            if context is not None:
                eid, token_id, verb, digest, _ = context
                stored = self._find_receipt(eid, token_id, verb, digest)
                if stored is not None:
                    raise _StoredReceipt(stored)
                used = self.conn.execute("SELECT COUNT(*) FROM op_receipts WHERE token_id=?",
                                         (token_id,)).fetchone()[0]
                if used >= MAX_SEEN_PER_TOKEN:
                    raise Refused("conflict", f"outbox limit of {MAX_SEEN_PER_TOKEN} entries per run token")
            yield lambda outcome: self._insert_receipt(context, outcome) if context else None

    def _insert_receipt(self, context, outcome):
        eid, token_id, verb, digest, key = context
        self.conn.execute(
            "INSERT INTO op_receipts(entry_id,token_id,verb,key,request_hash,result,seen_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (eid, token_id, verb, key, digest, json.dumps(outcome), self._iso(self.now())))

    # ------------------------------------------------------------- outage
    def record_outage(self, principal, gap_s, frm, to):
        """System only: push leases + live token expiries past *frm* out by *gap_s*."""
        if getattr(principal, "role", None) != "system":
            raise Refused("forbidden", "only the keeper records an outage")
        gap = min(int(gap_s), 86400)            # hard cap, as outage.OUTAGE_MAX_S
        if gap <= 0:
            return {"gap_s": 0}
        now = self.now()
        frm_iso, to_iso = self._iso(frm), self._iso(to)
        with self._write():
            granted = {}                          # token id -> outage grace already given
            for row in self.conn.execute(
                    "SELECT payload FROM events WHERE type='outage_recorded'"):
                prior = json.loads(row["payload"] or "{}")
                if prior.get("from") == frm_iso:
                    return {"gap_s": 0, "duplicate": True}   # this outage was already applied
                for tid in prior.get("tokens") or []:
                    granted[tid] = granted.get(tid, 0) + int(prior.get("gap_s") or 0)
            leases = [(r["key"], r["lease_until"]) for r in self.conn.execute(
                "SELECT key, lease_until FROM items WHERE state='claimed'"
                " AND lease_until IS NOT NULL AND lease_until > ?", (frm_iso,))]
            for key, lease in leases:
                self.conn.execute("UPDATE items SET lease_until=? WHERE key=?",
                                  (self._shift(lease, gap), key))
            # Grace is cumulative per token and capped: repeated restarts cannot keep a token alive
            # past mint + 24 h + TOKEN_GRACE_MAX_S; a token over the cap is simply not extended.
            toks = [(r["id"], r["expires_at"]) for r in self.conn.execute(
                "SELECT id, expires_at FROM run_tokens WHERE revoked_at IS NULL"
                " AND expires_at > ?", (frm_iso,))
                if granted.get(r["id"], 0) + gap <= TOKEN_GRACE_MAX_S]
            for tid, exp in toks:
                self.conn.execute("UPDATE run_tokens SET expires_at=? WHERE id=?",
                                  (self._shift(exp, gap), tid))
            self._append(principal, None, "outage_recorded",
                         {"gap_s": gap, "from": frm_iso, "to": to_iso,
                          "leases": len(leases), "tokens": [t for t, _ in toks]}, now)
        return {"gap_s": gap, "leases": len(leases), "tokens": len(toks)}

    def _shift(self, value, gap):
        parsed = self._parse_iso(value)
        return self._iso(parsed.timestamp() + gap) if parsed is not None else value
