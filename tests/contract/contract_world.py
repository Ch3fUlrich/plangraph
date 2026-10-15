"""Graph-building and lifecycle helpers shared by the contract tests.

Every helper talks to the keeper through ``Keeper.call`` only.  Each test builds
its own uniquely named items (the keeper is shared by the whole session), so
assertions about collections use membership, never equality.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from contract_schema import assert_valid


def uid():
    return uuid.uuid4().hex[:7].upper()


def need(kp, cap):
    """Skip unless the keeper under test offers the server-side fixture *cap*."""
    if cap not in kp.caps:
        pytest.skip(f"keeper under test lacks capability {cap!r}")


def ok(resp, schema=None, status=200):
    """Assert a successful response, validate it, return the parsed JSON."""
    assert resp.status == status, f"{resp.status}: {resp.raw[:300]!r}"
    assert "application/json" in resp.ctype
    body = resp.json
    if schema:
        assert_valid(body, schema)
    return body


def err(resp, status, code):
    """Assert a refusal: HTTP *status*, contract error *code*, non-empty ``msg``.

    The ``error`` code is normative; ``msg`` is free text and only checked to
    be a non-empty string (implementations word it differently).
    """
    assert resp.status == status, f"{resp.status}: {resp.raw[:300]!r}"
    assert "application/json" in resp.ctype
    body = resp.json
    assert_valid(body, "Error")
    assert body["error"] == code, body
    assert isinstance(body["msg"], str) and body["msg"].strip(), body
    return body


@dataclass
class Plan:
    lanes: list = field(default_factory=list)
    goal: str = ""

    @property
    def key(self):
        return self.lanes[0]


def make_plan(kp, n=1, chain=False, project="demo", who="router", goal=True,
              evidence=("decision",), **fields):
    """Create *n* lanes (optionally chained) plus a goal that depends on them."""
    tag = uid()
    items, lanes = [], []
    for i in range(n):
        item = {"project": project, "id": f"L{i}-{tag}", "kind": "lane",
                "title": f"lane {i} {tag}", "description": "a lane",
                "done_criteria": "criteria", "evidence_kinds": list(evidence),
                "depends_on": [f"L{i - 1}-{tag}"] if chain and i else []}
        item.update(fields)
        items.append(item)
        lanes.append(f"{project}:L{i}-{tag}")
    if goal:
        items.append({"project": project, "id": f"G-{tag}", "kind": "goal",
                      "title": f"goal {tag}", "description": "a goal",
                      "depends_on": [i["id"] for i in items]})
    ok(kp.post("create_items", who, {"items": items}), "Created")
    return Plan(lanes, f"{project}:G-{tag}" if goal else "")


def show(kp, key, who="reader"):
    return ok(kp.get(f"/v1/show?key={key}&fmt=json", who), "Show")


def claim(kp, key, who="worker1", **body):
    return ok(kp.post("claim", who, {"key": key, **body}), "Claim")["epoch"]


def evidence(kp, key, epoch, who="worker1", did="D-1"):
    return ok(kp.post("add_evidence", who, {
        "key": key, "epoch": epoch, "kind": "decision", "ref": {"id": did}}), "Evidence")


def submit(kp, key, epoch, who="worker1"):
    return ok(kp.post("submit", who, {"key": key, "epoch": epoch}), "Submit")


def to_in_review(kp, key, who="worker1"):
    need(kp, "decisions")
    epoch = claim(kp, key, who)
    assert evidence(kp, key, epoch, who)["verified"] is True
    submit(kp, key, epoch, who)
    return epoch


def verdict(kp, key, who="reviewer1", good=True, reason="reviewed"):
    return kp.post("verdict", who, {"key": key, "ok": good, "reason": reason})


def to_done(kp, key, who="worker1", reviewer="reviewer1"):
    epoch = to_in_review(kp, key, who)
    assert ok(verdict(kp, key, reviewer), "Verdict")["state"] == "done"
    return epoch


def snapshot(kp, who="reader"):
    return ok(kp.get("/v1/snapshot", who), "Snapshot")


def head(kp):
    snap = snapshot(kp)
    return snap["head_seq"], snap["head"]
