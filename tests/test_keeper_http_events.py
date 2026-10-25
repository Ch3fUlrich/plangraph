"""The event chain checked through the keeper's HTTP surface only.

Replaces, for the Postgres CI job, the three frozen contract tests that read the keeper's
database file directly (``tests/contract/test_events.py``).  The keeper serves no event
export, so the chain is checked from outside with what it does expose:

* ``/v1/snapshot`` carries the chain head (``head_seq`` + ``head``); after every write the
  new head must equal ``sha256(previous head + canonical event)`` for the event we expect
  (type, actor, role, key, payload), which also proves attribution and canonical JSON;
* reads, refusals and heartbeats must not move the head;
* ``python3 -m plangraph.keeper.cli admin verify`` (the adapter, so it works on either
  backend) recomputes the whole chain and compares the cached rows with the events.

Starts its own keeper through the contract harness (a subprocess on a free localhost port;
the backend follows ``PG_KEEPER_BACKEND``).  No database driver is imported here.
"""
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "contract"))
import contract_harness as harness  # noqa: E402

FIELDS = ("ts", "actor", "role", "model", "key", "type", "payload")


@pytest.fixture(scope="module")
def kp(tmp_path_factory):
    keeper = harness.start_local(tmp_path_factory.mktemp("http_events") / "k")
    try:
        yield keeper
    finally:
        keeper.stop()


# ------------------------------------------------------------------ helpers
def chain_hash(prev_hash, event):
    """The documented chain rule: sha256(prev_hash + canonical JSON of the 7 fields)."""
    body = {name: event[name] for name in FIELDS}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((prev_hash + canon).encode("utf-8")).hexdigest()


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def head(kp):
    resp = kp.get("/v1/snapshot", "reader")
    assert resp.status == 200, resp.raw[:200]
    snap = resp.json
    return snap["head_seq"], snap["head"]


def write(kp, verb, who, body):
    resp = kp.post(verb, who, body)
    assert resp.status == 200, f"{verb}: {resp.status} {resp.raw[:300]!r}"
    return resp.json


def link(prev_hash, new_hash, event, t_before, t_after):
    """The ``ts`` for which *event* chains *prev_hash* to *new_hash*, else ``None``."""
    for second in range(int(t_before) - 1, int(t_after) + 2):
        candidate = dict(event, ts=stamp(second))
        if chain_hash(prev_hash, candidate) == new_hash:
            return candidate["ts"]
    return None


def make_plan(kp, n=1):
    tag = uuid.uuid4().hex[:7].upper()
    lanes = [{"project": "demo", "id": f"L{i}-{tag}", "kind": "lane", "title": f"lane {i} {tag}",
              "description": "a lane", "done_criteria": "criteria",
              "evidence_kinds": ["decision"], "depends_on": []} for i in range(n)]
    goal = {"project": "demo", "id": f"G-{tag}", "kind": "goal", "title": f"goal {tag}",
            "description": "a goal", "depends_on": [i["id"] for i in lanes]}
    write(kp, "create_items", "router", {"items": [*lanes, goal]})
    return [f"demo:{i['id']}" for i in lanes]


def expect_event(kp, before, event, step):
    """Run *step*, then require exactly one new event equal to *event* on the chain."""
    seq0, hash0 = before
    t0 = time.time()
    out = step()
    t1 = time.time()
    seq1, hash1 = head(kp)
    assert seq1 == seq0 + 1 and hash1 != hash0
    assert link(hash0, hash1, event, t0, t1) is not None, (
        f"head after {event['type']} is not sha256(previous head + that event)")
    return out, (seq1, hash1)


# -------------------------------------------------------------------- tests
def test_each_write_extends_the_chain_by_the_documented_rule(kp):
    (key,) = make_plan(kp)
    state = head(kp)
    router = {"actor": "router", "role": "router", "model": None, "key": key}
    _, state = expect_event(
        kp, state, {**router, "type": "priority_set", "payload": {"priority": 4}},
        lambda: write(kp, "set_priority", "router", {"key": key, "n": 4}))
    _, state = expect_event(
        kp, state, {**router, "type": "priority_set", "payload": {"priority": 0}},
        lambda: write(kp, "set_priority", "router", {"key": key, "n": 0}))
    _, state = expect_event(
        kp, state, {**router, "type": "cancelled", "payload": {"reason": "chain check é中"}},
        lambda: write(kp, "cancel", "router", {"key": key, "reason": "chain check é中"}))


def test_reads_refusals_and_heartbeats_leave_the_head_alone(kp):
    (key,) = make_plan(kp)
    base = head(kp)
    for path in ("/v1/ready", "/v1/snapshot", "/v1/blocked", f"/v1/show?key={key}&fmt=json",
                 "/v1/changelog?fmt=json&since=0"):
        assert kp.get(path, "reader").status == 200, path
    assert head(kp) == base
    assert kp.post("claim", "reader", {"key": key}).status == 403          # forbidden
    assert kp.post("set_priority", "router", {"key": key, "n": "high"}).status == 400
    assert kp.post("set_priority", "router", {"key": "demo:NOPE", "n": 1}).status == 404
    assert head(kp) == base
    claimed = write(kp, "claim", "worker1", {"key": key})
    seq, digest = head(kp)
    assert seq == base[0] + 1 and digest != base[1]                          # a claim is one event
    write(kp, "heartbeat", "worker1", {"key": key, "epoch": claimed["epoch"]})
    assert head(kp)[0] == seq                                                # a heartbeat is none


def test_lifecycle_to_done_moves_the_head_per_event_and_verifies(kp):
    (key,) = make_plan(kp)
    start = head(kp)[0]
    epoch = write(kp, "claim", "worker1", {"key": key})["epoch"]
    seen = [head(kp)]
    write(kp, "add_evidence", "worker1", {"key": key, "epoch": epoch, "kind": "decision",
                                           "ref": {"id": "D-1"}})
    seen.append(head(kp))
    write(kp, "submit", "worker1", {"key": key, "epoch": epoch})
    seen.append(head(kp))
    assert write(kp, "verdict", "reviewer1", {"key": key, "ok": True})["state"] == "done"
    seen.append(head(kp))
    seqs = [s for s, _ in seen]
    assert seqs == sorted(set(seqs)) and seqs[0] == start + 1
    assert seqs[-1] == seqs[-2] + 2                    # review_verdict + review_passed
    assert len({h for _, h in seen}) == len(seen)      # every step changed the head
    snap = kp.get("/v1/snapshot", "reader").json
    changelog = kp.get("/v1/changelog?fmt=json&since=0", "reader").json
    assert changelog["head_seq"] == snap["head_seq"] == seen[-1][0]
    out = kp.admin("verify")
    assert out.returncode == 0 and "integrity ok" in out.stdout, out.stdout + out.stderr


def test_admin_verify_covers_the_whole_chain_after_many_writes(kp):
    keys = make_plan(kp, n=3)
    for number, key in enumerate(keys):
        write(kp, "set_priority", "router", {"key": key, "n": number})
    write(kp, "cancel", "router", {"key": keys[0], "reason": "x"})
    out = kp.admin("verify")
    assert out.returncode == 0 and "integrity ok" in out.stdout, out.stdout + out.stderr
