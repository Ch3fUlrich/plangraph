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
            "lease_until": None, "reviewer": None, "model": None, "priority": 0}


def fold(events):
    """Replay *events* (oldest first) into ``key -> cached state``.

    Only the fields the cache is compared against are guaranteed; the rest are
    best-effort mirrors of the events.
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
            for field in ("priority", "model"):
                if field in payload:
                    item[field] = payload[field]
        elif kind == "item_defined":
            if "priority" in payload:
                item["priority"] = payload["priority"]
        elif kind == "claimed":
            item["state"] = "claimed"
            item["epoch"] = payload.get("epoch", item["epoch"] + 1)
            item["claimed_by"] = payload.get("claimed_by", event.get("actor"))
            item["delegate"] = None
            item["lease_until"] = payload.get("lease_until")
            item["reviewer"] = None
        elif kind == "delegated":
            item["delegate"] = payload.get("worker")
        elif kind in ("released", "lease_expired"):
            item["state"] = "open"
            item["claimed_by"] = None
            item["delegate"] = None
            item["lease_until"] = None
        elif kind == "submitted":
            item["state"] = "in_review"
            item["lease_until"] = None
        elif kind == "changes_requested":
            item["state"] = payload.get("state", "claimed")
            item["reviewer"] = event.get("actor")
        elif kind == "review_passed":
            item["state"] = "done"
            item["reviewer"] = event.get("actor")
        elif kind == "cancelled":
            item["state"] = "cancelled"
        elif kind == "priority_set":
            item["priority"] = payload.get("priority", item["priority"])
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


def verify_integrity(conn):
    """Compare cached ``state/epoch/claimed_by`` against ``fold``.

    Returns a list of ``{key, code, field, expected, got}`` problems.
    """
    folded = fold(read_events(conn))
    cached = {row["key"]: row for row in conn.execute(
        "SELECT key,state,epoch,claimed_by FROM items")}
    problems = []
    for key, expected in folded.items():
        row = cached.get(key)
        if row is None:
            problems.append({"key": key, "code": "cache_missing",
                             "field": None, "expected": expected["state"],
                             "got": None})
            continue
        for field in ("state", "epoch", "claimed_by"):
            if row[field] != expected[field]:
                problems.append({"key": key, "code": "cache_mismatch",
                                 "field": field, "expected": expected[field],
                                 "got": row[field]})
    for key in cached:
        if key not in folded:
            problems.append({"key": key, "code": "cache_extra",
                             "field": None, "expected": None, "got": None})
    return problems
