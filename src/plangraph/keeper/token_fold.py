"""M2B-TOKEN-INTEGRITY: run-token rows replayed from the hash-chained events.

``token_minted`` events (no token value) create a token; the store's revocation rules
(``store_tokens._revoke_for_event`` and the re-mint rule) are replayed from the item
events, so a raw ``UPDATE run_tokens SET revoked_at=NULL`` (un-revoke), a forged row,
a deleted row or a changed binding (key/role/run/epoch) shows in ``verify_integrity``.

Residual (documented): writer heartbeats renew ``expires_at`` without an event, so a writer
token's expiry is only checked against the hard cap (mint event time + 24 h); reviewer tokens
never renew and must keep their minted expiry exactly.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

__all__ = ["fold_tokens", "verify_tokens"]

#: Must equal store_tokens._REVOKE_ALL (a test pins the two together).
REVOKE_ALL = frozenset({
    "claimed", "released", "lease_expired", "cancelled", "review_passed",
    "closed_on_decision", "answered", "changes_requested", "legacy_state_set",
    "review_withdrawn"})
MAX_LIFE_S = 86400
MAX_GRACE_S = 86400   # cumulative outage grace per token, as store_outbox.TOKEN_GRACE_MAX_S
RUN_PREFIX = "run:"


def fold_tokens(events):
    """``token_id -> {key, role, run_id, epoch, revoked}`` from the event chain."""
    tokens = {}
    dupes = []

    def revoke(pred, ts):
        for tok in tokens.values():
            if not tok["revoked"] and pred(tok):
                tok["revoked"] = True
                tok["revoked_at"] = ts

    for ev in events:
        key, kind, ts = ev.get("key"), ev.get("type"), ev.get("ts")
        payload = ev.get("payload") or {}
        if kind == "outage_recorded":            # M2B-OUTBOX: live tokens pushed out by the gap
            for tid in payload.get("tokens") or []:
                if tid in tokens:
                    tokens[tid]["grace_s"] += int(payload.get("gap_s") or 0)
            continue
        if key is None:
            continue
        if kind == "token_minted":
            role, run_id, epoch = payload.get("role"), payload.get("run_id"), payload.get("epoch")
            # re-mint: the store first revokes the live token of the same (key, epoch, role, run)
            revoke(lambda t: (t["key"], t["epoch"], t["role"], t["run_id"]) == (key, epoch, role, run_id), ts)
            if payload.get("token_id") in tokens:
                dupes.append(payload.get("token_id"))      # ids are AUTOINCREMENT: never reused
            tokens[payload.get("token_id")] = {"key": key, "role": role, "run_id": run_id,
                                               "epoch": epoch, "revoked": False,
                                               "minted_at": ts, "revoked_at": None,
                                               "token_sha256": payload.get("token_sha256"),
                                               "expires_at": payload.get("expires_at"), "grace_s": 0}
        elif kind in REVOKE_ALL:
            revoke(lambda t: t["key"] == key, ts)
        elif kind == "submitted":
            revoke(lambda t: t["key"] == key and t["role"] == "writer", ts)
        elif kind == "review_verdict" and str(ev.get("actor") or "").startswith(RUN_PREFIX):
            run_id = str(ev["actor"])[len(RUN_PREFIX):]
            # At most one live reviewer token per (key, run): a re-mint revokes the previous one and a
            # new epoch (claimed/legacy_state_set) revokes all, so this equals the store's id match.
            revoke(lambda t: t["key"] == key and t["role"] == "reviewer" and t["run_id"] == run_id, ts)
    tokens["__dupes__"] = dupes
    return tokens


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def verify_tokens(conn, events):
    """Problems (``{key, code, field, expected, got}``) between ``run_tokens`` and the fold."""
    try:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id,key,role,run_id,epoch,created_at,expires_at,revoked_at,token_sha256"
            " FROM run_tokens")}
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise                     # anything else must surface, never a silent clean
        rows = {}                     # a DB from before M2B-RUN-TOKENS (minted tokens still checked)
    folded = fold_tokens(events)
    problems = [{"key": None, "code": "token_mismatch", "field": "token_id",
                 "expected": "unique", "got": tid} for tid in folded.pop("__dupes__", [])]
    for tid, want in folded.items():
        row = rows.get(tid)
        if row is None:
            problems.append({"key": want["key"], "code": "token_missing", "field": "run_tokens",
                             "expected": tid, "got": None})
            continue
        # Strict: every mint chains its hash (the live keeper minted none before this lane), so an
        # event without one cannot anchor the row and is itself a problem.
        for field in ("key", "role", "run_id", "epoch", "token_sha256"):
            if row[field] != want[field]:
                problems.append({"key": want["key"], "code": "token_mismatch", "field": field,
                                 "expected": want[field], "got": row[field]})
        if (row["revoked_at"] is not None) != want["revoked"]:
            problems.append({"key": want["key"], "code": "token_mismatch", "field": "revoked",
                             "expected": want["revoked"], "got": row["revoked_at"] is not None})
        elif want["revoked"] and row["revoked_at"] != want["revoked_at"]:
            problems.append({"key": want["key"], "code": "token_mismatch", "field": "revoked_at",
                             "expected": want["revoked_at"], "got": row["revoked_at"]})
        if row["created_at"] != want["minted_at"]:          # anchored to the hash-chained event
            problems.append({"key": want["key"], "code": "token_mismatch", "field": "created_at",
                             "expected": want["minted_at"], "got": row["created_at"]})
        want_exp = want["expires_at"]
        if want["grace_s"] and want_exp:
            base = _ts(want_exp)
            want_exp = (datetime.fromtimestamp(base + want["grace_s"], tz=timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z")) if base else want_exp
        if want["role"] != "writer" and row["expires_at"] != want_exp:
            # only writer heartbeats renew; any other token keeps its minted expiry exactly
            problems.append({"key": want["key"], "code": "token_mismatch", "field": "expires_at",
                             "expected": want_exp, "got": row["expires_at"]})
        if want["grace_s"] > MAX_GRACE_S:   # the keeper never grants more (store_outbox cap)
            problems.append({"key": want["key"], "code": "token_mismatch", "field": "grace_s",
                             "expected": f"<= {MAX_GRACE_S}s", "got": want["grace_s"]})
        created, expires = _ts(want["minted_at"]), _ts(row["expires_at"])
        if created is None or expires is None or expires > created + MAX_LIFE_S + want["grace_s"]:
            problems.append({"key": want["key"], "code": "token_mismatch", "field": "expires_at",
                             "expected": f"<= created_at + {MAX_LIFE_S}s", "got": row["expires_at"]})
    for tid, row in rows.items():
        if tid not in folded:
            problems.append({"key": row["key"], "code": "token_extra", "field": "run_tokens",
                             "expected": None, "got": tid})
    return problems
