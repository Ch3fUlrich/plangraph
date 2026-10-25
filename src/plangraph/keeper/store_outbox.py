"""M2B-OUTBOX: replay of offline-queued run-token writes + the outage grace.

When the keeper is unreachable a run's CLI queues its writes (``heartbeat``,
``add_evidence``, ``submit``; ``verdict`` for a reviewer token) in a local outbox.
``replay(principal, entries)`` delivers them later, in queue order:

* only a run-token principal may replay, and only its own role's verbs;
* every entry carries a client-generated id (32 hex); an id already in
  ``outbox_seen`` is answered ``duplicate`` and NOT executed again;
* every entry runs through the ordinary store verb, so epoch, item state,
  binding, revocation and expiry are re-checked exactly as for a live call — a
  stale epoch, a reordered submit or a token revoked meanwhile is refused;
* the result (``ok`` or the refusal code) is recorded with the id (append-only).

``record_outage(gap_s, frm, to)`` (system, at keeper start) freezes the clocks
for the outage: the leases of claimed items and the expiry of live run tokens
whose deadline fell after ``frm`` are pushed out by the gap, in one transaction
with an ``outage_recorded`` event, so a token that expired during the outage is
honoured for the outage duration (PROPOSAL §3) and ``verify_integrity`` replays it.
"""
from __future__ import annotations

import json
import re

from . import auth
from .errors import Refused

__all__ = ["OutboxMixin", "MAX_ENTRIES"]

MAX_ENTRIES = 100
#: Per run token: a token cannot grow outbox_seen without bound (its life is <= 24 h anyway).
MAX_SEEN_PER_TOKEN = 1000
TOKEN_GRACE_MAX_S = 86400      # total outage grace one run token can ever receive
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


class OutboxMixin:
    """``replay`` for run tokens and ``record_outage`` for the daemon."""

    def replay(self, principal, entries):
        # qwen-R1 class sweep: the run-token gate sits inside the audited
        # ``Store._run`` seam like every other verb's role gate, so a wrong
        # principal is refused 403 WITH its audit_log row (a gate here would
        # answer before the seam and audit nothing).
        return self._run(principal, "replay", None,
                         lambda now: self._replay(principal, entries, now))

    def _replay(self, principal, entries, now):
        if not isinstance(principal, auth.RunPrincipal):
            raise Refused("forbidden", "only a run token may replay an outbox")
        if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
            raise Refused("validation", f"entries must be a list of 1..{MAX_ENTRIES}")
        used = self.conn.execute("SELECT COUNT(*) FROM outbox_seen WHERE token_id=?",
                                 (principal.token_id,)).fetchone()[0]
        if used + len(entries) > MAX_SEEN_PER_TOKEN:
            raise Refused("conflict", f"outbox limit of {MAX_SEEN_PER_TOKEN} entries per run token")
        results = []
        for entry in entries:
            results.append(self._replay_one(principal, entry))
        return {"results": results}

    def _replay_one(self, principal, entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) \
                or not _ID_RE.fullmatch(entry["id"]):
            return {"id": entry.get("id") if isinstance(entry, dict) else None,
                    "result": "validation"}
        eid, verb, body = entry["id"], entry.get("verb"), entry.get("body")
        seen = self.conn.execute("SELECT result FROM outbox_seen WHERE entry_id=?",
                                 (eid,)).fetchone()
        if seen is not None:
            return {"id": eid, "result": "duplicate", "first": seen["result"]}
        if not isinstance(body, dict):
            return self._seen(principal, eid, verb, None, "validation")
        try:
            self._replay_call(principal, verb, body)
            result = "ok"
        except Refused as exc:
            result = exc.code
        return self._seen(principal, eid, verb, body.get("key"), result)

    def _replay_call(self, principal, verb, body):
        key = body.get("key")
        if verb == "heartbeat":
            return self.heartbeat(principal, key, body.get("epoch"))
        if verb == "add_evidence":
            return self.add_evidence(principal, key, body.get("epoch"), body.get("kind"),
                                     body.get("ref") or {})
        if verb == "submit":
            return self.submit(principal, key, body.get("epoch"))
        if verb == "verdict":
            if not isinstance(body.get("ok"), bool):
                raise Refused("validation", "ok must be a boolean")
            return self.verdict(principal, key, body["ok"], body.get("reason") or "")
        raise Refused("forbidden", f"{verb!r} cannot be replayed")

    # Note: the HTTP server serialises every request under one lock, so two replays of the
    # same id never interleave between the outbox_seen check and the insert.
    def _seen(self, principal, eid, verb, key, result):
        with self._write():
            self.conn.execute(
                "INSERT INTO outbox_seen(entry_id,token_id,verb,key,result,seen_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (eid, principal.token_id, str(verb)[:32], key, result, self._iso(self.now())))
        return {"id": eid, "result": result}

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
