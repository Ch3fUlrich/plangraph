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
        if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
            raise Refused("validation", f"entries must be a list of 1..{MAX_ENTRIES}")
        if not all(isinstance(entry, dict) and entry.get("verb") == "verdict" for entry in entries):
            self._receipt_principal(principal)
        results, recovery = [], []
        for entry in entries:
            try:
                eid, verb, body, digest = self._receipt_entry(entry)
                if verb == "verdict":
                    if not body.get("binding"):
                        raise Refused("forbidden", "seat verdict requires an admission binding")
                    result = self.write_receipt(principal, entry)
                else:
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
        eid, verb, body, digest = self._receipt_entry(entry)
        if verb == "verdict":
            if not body.get("binding"):
                raise Refused("forbidden", "seat verdict requires an admission binding")
            if entry.get("legacy") is True:
                return {"id": eid, "result": "recovery_required"}
            key = body.get("key")
            if not isinstance(key, str):
                raise Refused("validation", "receipt write requires a key")
            reply = self.verdict(principal, self.resolve_key(key), body.get("ok"),
                                 body.get("reason", ""), binding=body["binding"])
            return {"id": eid, "result": "ok", "reply": reply}
        self._receipt_principal(principal)
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

    @staticmethod
    def _seat_binding(binding):
        fields = {"key", "epoch", "round", "head", "seat_run"}
        if not isinstance(binding, dict) or fields != binding.keys():
            raise Refused("forbidden", "seat verdict requires an admission binding")
        if (not isinstance(binding["key"], str)
                or type(binding["epoch"]) is not int or type(binding["round"]) is not int
                or not isinstance(binding["head"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", binding["head"])
                or not isinstance(binding["seat_run"], str)
                or not re.fullmatch(r"run-[a-zA-Z0-9]{1,120}", binding["seat_run"])):
            raise Refused("validation", "malformed seat admission binding")
        return binding["seat_run"]

    def _check_seat_round(self, item, binding):
        if (item["state"] != "in_review" or item["epoch"] != binding["epoch"]
                or item["round"] != binding["round"]):
            raise Refused("round_stale", "seat admission is for a different review round")
        heads = [e["ref"].get("commit") for e in self._evidence(item["key"])
                 if e["epoch"] == item["epoch"] and e["kind"] == "commit+ci" and e["verified"]]
        if not heads or heads[-1] != binding["head"]:
            raise Refused("conflict", "seat admission head is no longer current")

    def _seat_row(self, run, phase):
        row = self.conn.execute("SELECT result FROM op_receipts WHERE entry_id=?",
                                (f"seat:{run}:{phase}",)).fetchone()
        return json.loads(row["result"]) if row else None

    def _record_seat(self, run, phase, key, value):
        self._insert_receipt((f"seat:{run}:{phase}", 0, f"seat_{phase}", None, key), value)

    def admit_seat_verdict(self, principal, binding):
        run = self._seat_binding(binding)
        key = binding["key"]

        def apply(now):
            self._require_role(principal, "spawner", "router")
            with self._write():
                item = self._get_or_refuse(key)
                if principal.role == "spawner":
                    self._require_project(principal, item["project"])
                previous = self._seat_row(run, "admission")
                if previous is not None:
                    if previous != binding:
                        raise Refused("conflict", "seat run already admitted for another review")
                    return previous
                self._check_seat_round(item, binding)
                self._record_seat(run, "admission", key, binding)
            return binding

        return self._run(principal, "admit_seat_verdict", key, apply)

    @staticmethod
    def _seat_credential(principal):
        return [principal.name, getattr(principal, "token_id", None)]

    def _seat_payload(self, principal, key, ok, reason, binding):
        run = self._seat_binding(binding)
        if key != binding["key"] or self._seat_row(run, "admission") != binding:
            raise Refused("conflict", "seat binding differs from the admitted review")
        if isinstance(principal, auth.RunPrincipal) and principal.run_id != run:
            raise Refused("forbidden", "seat run does not match the reviewer credential")
        if type(ok) is not bool or not isinstance(reason, str):
            raise Refused("validation", "seat verdict requires a boolean and a reason")
        digest = hashlib.sha256(json.dumps([ok, reason], separators=(",", ":")).encode()).hexdigest()
        return run, dict(credential=self._seat_credential(principal), digest=digest)

    def bind_seat_verdict(self, principal, key, ok, reason, binding):
        def apply(now):
            self._require_role(principal, "reviewer")
            with self._write():
                run, payload = self._seat_payload(principal, key, ok, reason, binding)
                item = self._get_or_refuse(key)
                self._require_project(principal, item["project"])
                previous = self._seat_row(run, "payload")
                if previous is not None:
                    if previous != payload:
                        raise Refused("conflict", "seat verdict payload already bound")
                else:
                    self._check_seat_round(item, binding)
                    self._record_seat(run, "payload", key, payload)
            return {"binding": binding}
        return self._run(principal, "verdict", key, apply)

    def _seat_delivery(self, principal, key, ok, reason, binding, item=None):
        if binding is None:
            seat_identity = principal.name.startswith("reviewer-")
            if isinstance(principal, auth.RunPrincipal):
                seat_identity = seat_identity or self._seat_row(principal.run_id, "admission") is not None
                if not seat_identity and principal.token_role == "reviewer":
                    request = self._check_run(self._get_or_refuse(key), principal.run_id, self.now()).get("request", {})
                    seat_identity = request.get("role") in ("review", "seat")
            if seat_identity or isinstance(reason, str) and reason.lower().startswith("seat "):
                raise Refused("forbidden", "seat verdict requires an admission binding")
            return
        run, payload = self._seat_payload(principal, key, ok, reason, binding)
        if self._seat_row(run, "payload") != payload:
            raise Refused("conflict", "seat verdict payload differs from the bound decision")
        outcome = self._seat_row(run, "outcome")
        if outcome is not None:
            raise _StoredReceipt(outcome)
        if item is not None:
            self._check_seat_round(item, binding)

    def read_revoked_seat_verdict(self, bearer, body):
        """Return only the exact settled verdict for its original reviewer run."""
        if not bearer or not isinstance(body, dict) or not isinstance(body.get("binding"), dict):
            return None
        token = self.conn.execute(
            "SELECT id,run_id,key FROM run_tokens WHERE token_sha256=?"
            " AND role='reviewer' AND revoked_at IS NOT NULL",
            (auth.hash_token(bearer),)).fetchone()
        if token is None or token["run_id"] != body["binding"].get("seat_run") or token["key"] != body.get("key"):
            return None
        outcome = self._seat_row(token["run_id"], "outcome")
        if outcome is None:
            return None
        principal = auth.RunPrincipal(auth.RUN_PREFIX + token["run_id"], "reviewer", [], None,
                                      token_id=token["id"], run_id=token["run_id"])
        try:
            self._seat_delivery(principal, body["key"], body.get("ok"), body.get("reason", ""),
                                body["binding"])
        except _StoredReceipt as settled:
            return settled.result
        return None

    def _seat_result(self, binding, result):
        if binding is not None:
            self._record_seat(binding["seat_run"], "outcome", binding["key"], result)
        return result

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
