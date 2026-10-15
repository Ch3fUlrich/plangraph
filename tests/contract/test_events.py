"""Contract 7: the event head over HTTP and the hash chain from outside.

Events are not served over HTTP.  The HTTP surface exposes only the chain head
(``head`` + ``head_seq`` in the snapshot) and the derived ``changelog``.  The
chain itself is checked from an event export: ``PG_CONTRACT_EVENTS_JSONL`` (a
JSON-lines file of event records) when set, else - for the local SQLite keeper
only - a read-only dump of its ``events`` table through the stdlib ``sqlite3``
module (no keeper code).  ``pg admin verify`` is the CLI surface for the same
check.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from contract_harness import run_cli
from contract_schema import assert_valid
from contract_world import (claim, err, head, make_plan, need, ok, show, snapshot,
                            to_done, uid)

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
FIELDS = ("ts", "actor", "role", "model", "key", "type", "payload")


def event_hash(prev_hash, event):
    """The contract's chain rule (docs/contract/keeper-contract-v1.md)."""
    body = {name: event[name] for name in FIELDS}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((prev_hash + canon).encode("utf-8")).hexdigest()


def load_events(kp):
    path = os.environ.get("PG_CONTRACT_EVENTS_JSONL")
    if path:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not (kp.local and kp.dir):
        pytest.skip("no event export: set PG_CONTRACT_EVENTS_JSONL")
    conn = sqlite3.connect(f"file:{kp.dir / 'keeper.db'}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT seq,ts,actor,role,model,key,type,payload,prev_hash,hash"
                            " FROM events ORDER BY seq").fetchall()
    finally:
        conn.close()
    names = ("seq", "ts", "actor", "role", "model", "key", "type", "payload", "prev_hash", "hash")
    return [dict(zip(names, row), payload=json.loads(row[7])) for row in rows]


def chain_problems(events):
    prev, bad = "", []
    for index, event in enumerate(events, 1):
        if event["seq"] != index:
            bad.append((index, "seq gap"))
        if event["prev_hash"] != prev:
            bad.append((index, "prev_hash"))
        if event["hash"] != event_hash(prev, event):
            bad.append((index, "hash"))
        prev = event["hash"]
    return bad


def test_head_moves_only_on_successful_writes(kp):
    plan = make_plan(kp)
    base = head(kp)
    kp.get("/v1/ready", "reader")
    kp.get("/v1/snapshot", "reader")
    assert head(kp) == base                                   # reads
    err(kp.post("claim", "reader", {"key": plan.key}), 403)
    assert head(kp) == base                                   # refusals
    epoch = claim(kp, plan.key)
    seq, digest = head(kp)
    assert seq == base[0] + 1 and digest != base[1]           # claim = one event
    ok(kp.post("heartbeat", "worker1", {"key": plan.key, "epoch": epoch}))
    assert head(kp)[0] == seq                                  # a heartbeat appends no event


def test_event_count_per_verb(kp):
    need(kp, "decisions")
    tag = uid()
    start = head(kp)[0]
    items = [{"project": "demo", "id": f"{n}-{tag}", "kind": k, "title": f"{n} {tag}",
              "depends_on": [] if n == "A" else [f"A-{tag}"], "evidence_kinds": ["decision"]}
             for n, k in (("A", "lane"), ("G", "goal"))]
    ok(kp.post("create_items", "router", {"items": items}))
    assert head(kp)[0] == start + 2                            # one item_created per item
    key = f"demo:A-{tag}"
    seq = head(kp)[0]
    for step, delta in (
            (lambda: kp.post("set_priority", "router", {"key": key, "n": 2}), 1),
            (lambda: kp.post("edit_definition", "router", {"key": key, "fields": {"title": "t2"}}), 1)):
        step()
        assert head(kp)[0] == seq + delta
        seq += delta
    epoch = claim(kp, key)
    assert head(kp)[0] == seq + 1
    ok(kp.post("add_evidence", "worker1", {"key": key, "epoch": epoch, "kind": "decision",
                                            "ref": {"id": "D-1"}}))
    ok(kp.post("submit", "worker1", {"key": key, "epoch": epoch}))
    assert head(kp)[0] == seq + 3
    ok(kp.post("verdict", "reviewer1", {"key": key, "ok": True}))
    assert head(kp)[0] == seq + 5                              # review_verdict + review_passed


def test_chain_verifies_from_outside(kp):
    need(kp, "decisions")
    plan = make_plan(kp)
    to_done(kp, plan.key)
    events = load_events(kp)
    for event in events:
        assert_valid(event, "Event")
        assert TS.match(event["ts"]), event["ts"]
    assert chain_problems(events) == []
    snap = snapshot(kp)
    assert snap["head"] == events[-1]["hash"] and snap["head_seq"] == events[-1]["seq"]
    mine = [e["type"] for e in events if e["key"] == plan.key]
    assert mine[0] == "item_created" and "claimed" in mine
    assert events[0]["prev_hash"] == ""


def test_event_payload_and_actor_attribution(kp):
    plan = make_plan(kp)
    claim(kp, plan.key)
    events = [e for e in load_events(kp) if e["key"] == plan.key]
    created, claimed = events[0], events[1]
    assert (created["type"], created["actor"], created["role"]) == ("item_created", "router", "router")
    assert (claimed["type"], claimed["actor"], claimed["role"]) == ("claimed", "worker1", "worker")
    assert claimed["payload"]["epoch"] == 1 and claimed["payload"]["claimed_by"] == "worker1"
    assert claimed["payload"]["lease_until"] == show(kp, plan.key)["lease_until"]


def test_admin_verify_passes_and_flags_tampering(kp, tmp_path):
    need(kp, "admin")
    make_plan(kp)
    out = kp.admin("verify")
    assert out.returncode == 0 and "integrity ok" in out.stdout
    if not (kp.local and kp.dir):
        pytest.skip("tamper check needs a local database copy")
    copy = tmp_path / "copy.db"
    src = sqlite3.connect(f"file:{kp.dir / 'keeper.db'}?mode=ro", uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.execute("DROP TRIGGER trg_events_append_only_update")
    dst.execute("UPDATE events SET payload='{\"forged\":1}' WHERE seq=2")
    dst.commit()
    dst.close()
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(copy)}), encoding="utf-8")
    cfg.chmod(0o600)
    bad = run_cli(["--config", str(cfg), "admin", "verify"])
    assert bad.returncode == 2 and "hash_mismatch" in bad.stdout
    shutil.rmtree(tmp_path, ignore_errors=True)
