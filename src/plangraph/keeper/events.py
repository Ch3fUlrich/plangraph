"""Append-only event log: hash chain, fold, and integrity checks.

Each event's hash is ``sha256(prev_hash + canonical_json(event))`` where the
canonical event is the ordered dict of ``ts, actor, role, model, key, type,
payload`` (``prev_hash`` and ``hash`` are deliberately excluded).  The genesis
event has an empty ``prev_hash``.  ``fold`` replays events into the cached item
state; ``verify_integrity`` proves the cache equals that replay.
"""
from __future__ import annotations

import hashlib
import json

__all__ = ["canonical", "event_hash", "append_event", "read_events", "fold",
           "verify_chain", "verify_integrity", "FIELDS"]

FIELDS = ("ts", "actor", "role", "model", "key", "type", "payload")


def canonical(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ASCII only."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def event_hash(prev_hash: str, event: dict) -> str:
    """Chain hash for one event; *event* carries the FIELDS above."""
    body = {field: event.get(field) for field in FIELDS}
    digest = hashlib.sha256((prev_hash + canonical(body)).encode("utf-8"))
    return digest.hexdigest()


def append_event(conn, ts, actor, role, model, key, type, payload=None):
    """Append one event and return ``(seq, hash)``.

    Must run inside the caller's transaction so the event and its cache write
    commit together.
    """
    row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row[0] if row is not None else ""
    payload = payload or {}
    event = {"ts": ts, "actor": actor, "role": role, "model": model, "key": key,
             "type": type, "payload": payload}
    digest = event_hash(prev_hash, event)
    cursor = conn.execute(
        "INSERT INTO events(ts,actor,role,model,key,type,payload,prev_hash,hash)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (ts, actor, role, model, key, type, canonical(payload), prev_hash, digest))
    return cursor.lastrowid, digest


def read_events(conn):
    """All events, oldest first, with ``payload`` parsed."""
    rows = conn.execute(
        "SELECT seq,ts,actor,role,model,key,type,payload,prev_hash,hash"
        " FROM events ORDER BY seq")
    out = []
    for row in rows:
        out.append({
            "seq": row["seq"], "ts": row["ts"], "actor": row["actor"],
            "role": row["role"], "model": row["model"], "key": row["key"],
            "type": row["type"], "payload": json.loads(row["payload"] or "{}"),
            "prev_hash": row["prev_hash"], "hash": row["hash"],
        })
    return out


def _new_state():
    return {"state": "open", "epoch": 0, "claimed_by": None, "delegate": None,
            "lease_until": None, "reviewer": None, "model": None, "priority": 0,
            "round": 0, "evidence": [], "review_waiver": None,
            "review_tier": "standard", "description": None,
            "host": None}


def fold(events):
    """Replay *events* (oldest first) into ``key -> cached state``.

    Every field ``verify_integrity`` compares is reconstructed here, including
    the evidence events (so a raw ``UPDATE evidence`` cannot hide) and the
    lifecycle columns a raw writer might tamper with.  Events that the store
    does not emit yet (``heartbeat``) are still handled, so the fold is the
    contract the store must meet.
    """
    state = {}
    for event in events:
        key = event.get("key")
        if key is None:
            continue
        payload = event.get("payload") or {}
        item = state.setdefault(key, _new_state())
        kind = event["type"]
        if kind == "item_created":
            item["state"] = "open"
            item["epoch"] = 0
            for field in ("priority", "model", "review_tier", "description",
                          "host"):
                if field in payload:
                    item[field] = payload[field]
        elif kind == "item_defined":
            if "priority" in payload:
                item["priority"] = payload["priority"]
            if "review_tier" in payload:
                item["review_tier"] = payload["review_tier"]
            for name in ("description", "host"):
                if name in payload:
                    item[name] = payload[name]
            if "while" in payload:
                # D-434: the router re-tiered an active item, so the store
                # bumped ``round`` in the same UPDATE; replay it or the cache
                # would keep counting the verdicts this edit just voided.
                item["round"] += 1
        elif kind == "claimed":
            item["state"] = "claimed"
            item["epoch"] = payload.get("epoch", item["epoch"] + 1)
            item["claimed_by"] = payload.get("claimed_by", event.get("actor"))
            item["delegate"] = None
            item["lease_until"] = payload.get("lease_until")
            item["reviewer"] = None
            item["round"] = 0
            # A waiver belongs to one work epoch; a fresh claim drops it.
            item["review_waiver"] = None
        elif kind == "delegated":
            item["delegate"] = payload.get("worker")
        elif kind in ("released", "lease_expired"):
            item["state"] = "open"
            item["claimed_by"] = None
            item["delegate"] = None
            item["lease_until"] = None
            item["reviewer"] = None
            item["review_waiver"] = None
        elif kind == "heartbeat":
            item["lease_until"] = payload.get("lease_until",
                                              item["lease_until"])
        elif kind == "submitted":
            item["state"] = "in_review"
            item["lease_until"] = None
            item["round"] = payload.get("round", item["round"])
        elif kind == "changes_requested":
            item["state"] = payload.get("state", "claimed")
            item["reviewer"] = event.get("actor")
            # A second changes-requested sends the item back to the open pool
            # and the store clears the holder; mirror that exactly.  The first
            # returns it to ``claimed`` under a fresh lease, which the store
            # records in the payload.
            if item["state"] == "open":
                item["claimed_by"] = None
                item["delegate"] = None
                item["lease_until"] = None
            else:
                item["lease_until"] = payload.get("lease_until")
        elif kind == "review_passed":
            item["state"] = "done"
            item["reviewer"] = event.get("actor")
        elif kind == "closed_on_decision":
            # X7b: the router's decision close is the tier-`none` equivalent of
            # a passed review -- same final state, same lock, same attribution.
            item["state"] = "done"
            item["reviewer"] = event.get("actor")
        elif kind == "legacy_state_set":
            # X7c: the router opens an ``open`` legacy row into an active state
            # with no claim.  Only the state and holder change; the owner is
            # carried in the payload so the cache row folds back identically.
            item["state"] = payload.get("state", item["state"])
            item["claimed_by"] = payload.get("claimed_by", item["claimed_by"])
            if "epoch" in payload:
                # The move opens a fresh work epoch at a fresh round and
                # clears the holder/lease/waiver columns; replay exactly what
                # the store wrote so ``verify_integrity`` agrees.  Payloads
                # written before the epoch bump carry no epoch and keep the
                # old all-else-unchanged replay.
                item["epoch"] = payload["epoch"]
                item["round"] = payload.get("round", item["round"])
                item["delegate"] = None
                item["reviewer"] = None
                item["lease_until"] = None
                item["review_waiver"] = None
        elif kind == "waiver_set":
            item["review_waiver"] = payload.get("decision_id")
        elif kind == "cancelled":
            item["state"] = "cancelled"
            item["lease_until"] = None
            # The work is abandoned, so the waiver that was raised for it dies
            # here too -- ``_cancel`` clears the same column.
            item["review_waiver"] = None
        elif kind == "priority_set":
            item["priority"] = payload.get("priority", item["priority"])
        elif kind == "evidence_added":
            item["evidence"].append({
                "epoch": item["epoch"],
                "kind": payload.get("kind"),
                "verified": bool(payload.get("verified")),
            })
    return state


def verify_chain(conn):
    """Recompute the chain; return a list of ``{seq, code, msg}`` problems."""
    problems = []
    prev_hash = ""
    for event in read_events(conn):
        if event["prev_hash"] != prev_hash:
            problems.append({"seq": event["seq"], "code": "prev_hash_mismatch",
                             "msg": f"expected prev_hash {prev_hash!r}, stored "
                                    f"{event['prev_hash']!r}"})
        want = event_hash(prev_hash, event)
        if event["hash"] != want:
            problems.append({"seq": event["seq"], "code": "hash_mismatch",
                             "msg": f"stored {event['hash']!r}, recomputed {want!r}"})
        prev_hash = event["hash"]
    return problems


def folded_aliases(events):
    """``old_key -> new_key`` as the ``item_created.rehomed_from`` events say."""
    return {e["payload"]["rehomed_from"]: e["key"] for e in events
            if e["type"] == "item_created"
            and (e.get("payload") or {}).get("rehomed_from")}


def verify_integrity(conn):
    """Compare the cached rows against ``fold`` and the evidence table.

    Compares the lifecycle columns (``state``, ``epoch``, ``claimed_by``,
    ``delegate``, ``reviewer``) exactly, the lease with the heartbeat
    tolerance described below, and the recomputed per-key evidence counts
    (total and verified).

    Returns a list of ``{key, code, field, expected, got}`` problems.
    """
    all_events = read_events(conn)
    folded = fold(all_events)
    cached = {row["key"]: row for row in conn.execute(
        "SELECT key,state,epoch,round,claimed_by,delegate,lease_until,reviewer,"
        "review_waiver,review_tier,description,host FROM items")}
    counted = {row["key"]: row for row in conn.execute(
        "SELECT key, COUNT(*) AS total, COALESCE(SUM(verified), 0) AS verified"
        " FROM evidence GROUP BY key")}
    problems = []
    for key, expected in folded.items():
        row = cached.get(key)
        if row is None:
            problems.append({"key": key, "code": "cache_missing",
                             "field": None, "expected": expected["state"],
                             "got": None})
            continue
        for field in ("state", "epoch", "round", "claimed_by", "delegate",
                      "reviewer", "review_waiver", "review_tier", "description",
                      "host"):
            if row[field] != expected[field]:
                problems.append({"key": key, "code": "cache_mismatch",
                                 "field": field, "expected": expected[field],
                                 "got": row[field]})
        # ``heartbeat`` extends a lease without emitting an event, so the cache
        # may legitimately be *later* than the fold; clearing it or rolling it
        # back is not.  (The store should emit a heartbeat event; until then
        # this is the only comparison that does not flag honest heartbeats.)
        want_lease, got_lease = expected["lease_until"], row["lease_until"]
        if want_lease is None:
            lease_bad = got_lease is not None
        else:
            lease_bad = got_lease is None or got_lease < want_lease
        if lease_bad:
            problems.append({"key": key, "code": "cache_mismatch",
                             "field": "lease_until", "expected": want_lease,
                             "got": got_lease})
        want_total = len(expected["evidence"])
        want_verified = sum(1 for e in expected["evidence"] if e["verified"])
        actual = counted.get(key)
        got_total = actual["total"] if actual is not None else 0
        got_verified = int(actual["verified"]) if actual is not None else 0
        if (got_total, got_verified) != (want_total, want_verified):
            problems.append({"key": key, "code": "evidence_mismatch",
                             "field": "evidence",
                             "expected": {"count": want_total,
                                          "verified": want_verified},
                             "got": {"count": got_total,
                                     "verified": got_verified}})
    for key in cached:
        if key not in folded:
            problems.append({"key": key, "code": "cache_extra",
                             "field": None, "expected": None, "got": None})
    want = folded_aliases(all_events)
    have = {r["old_key"]: r["new_key"]
            for r in conn.execute("SELECT old_key,new_key FROM aliases")}
    for old in sorted(set(want) | set(have)):
        if want.get(old) != have.get(old):
            problems.append({"key": old, "code": "alias_mismatch",
                             "field": "alias", "expected": want.get(old),
                             "got": have.get(old)})
    return problems
