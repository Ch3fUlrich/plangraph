"""Shared fixtures/helpers for the keeper store tests.

Everything here is deterministic: the clock is injected and all verifiers are
fakes, so no test touches the network, the real clock, or system state.
"""
from __future__ import annotations

from pathlib import Path

from plangraph.keeper import auth, db as kdb
from plangraph.keeper.evidence_types import Result
from plangraph.keeper.store import Store

# name, role, projects, family
PRINCIPALS = [
    ("router", "router", [], None),
    ("l1main", "l1", ["demo"], "teamA"),
    ("l1other", "l1", ["other"], "teamA"),
    ("workerA", "worker", ["demo"], "teamB"),
    ("workerB", "worker", ["demo"], "teamB"),
    ("workerC", "worker", ["demo"], "teamC"),
    ("reviewer", "reviewer", [], "teamC"),
    ("reviewerSame", "reviewer", [], "teamA"),
    ("reviewerNoFam", "reviewer", [], None),
    ("reader", "reader", [], "teamD"),
    ("system", "system", [], None),
]


class Clock:
    """A deterministic monotonic clock; tests call ``advance`` to move it."""

    def __init__(self, t=1_700_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)
        return self.t


class FakeVerifier:
    """A pluggable verifier double.  ``ok`` may be flipped mid-test."""

    def __init__(self, ok=True, detail="verified"):
        self.ok = ok
        self.detail = detail
        self.calls = []

    def verify(self, item, ref, ctx):
        self.calls.append((dict(item or {}), dict(ref or {}), ctx))
        return Result(self.ok, self.detail, dict(ref or {}))


class World:
    """A seeded keeper DB with principals, a store and fake verifiers."""

    def __init__(self, tmp_path, *, create=True):
        self.path = Path(tmp_path) / "keeper.db"
        self.conn = kdb.open_db(self.path, create=create)
        self.clock = Clock()
        self.verifiers = {
            "decision": FakeVerifier(),
            "commit+ci": FakeVerifier(),
            "run-record": FakeVerifier(),
            "semaphore-task": FakeVerifier(),
        }
        self.store = Store(self.conn, clock=self.clock, verifiers=self.verifiers)
        self.tokens = {}
        self.principals = {}
        for name, role, projects, family in PRINCIPALS:
            token = auth.add_principal(self.conn, name, role, projects, family=family)
            self.tokens[name] = token
            self.principals[name] = auth.authenticate(self.conn, token)

    def p(self, name):
        return self.principals[name]

    def tok(self, name):
        return self.tokens[name]

    def close(self):
        self.conn.close()


def defn(pid, deps=(), project="demo", kind="lane", evidence_kinds=("any",),
         **kw):
    """Build one create_items definition."""
    item = {
        "project": project, "id": pid, "title": f"title {pid}", "kind": kind,
        "track": "T0", "owner": "tester", "estimate_h": 1,
        "depends_on": [d if ":" in d else f"{project}:{d}" for d in deps],
        "done_criteria": "criteria", "evidence_kinds": list(evidence_kinds),
        "operator": False,
    }
    item.update(kw)
    return item


def seed(world, lanes, project="demo", evidence_kinds=("any",), goal=True):
    """Create ``lanes`` plus a goal that depends on every lane.

    Returns the fully-qualified lane keys.  A goal is required because
    ``views.validate`` refuses work that cannot reach one.
    """
    items = [defn(pid, deps, project=project, evidence_kinds=evidence_kinds)
             for pid, deps in lanes]
    if goal:
        items.append(defn("GOAL", [pid for pid, _ in lanes], project=project,
                          kind="goal"))
    world.store.create_items(world.p("router"), items)
    return [f"{project}:{pid}" for pid, _ in lanes]


def to_claimed(world, key, who="l1main", lease_s=7200):
    result = world.store.claim(world.p(who), key, lease_s=lease_s)
    return result["epoch"]


def to_in_review(world, key, who="l1main", kind="decision", ref=None,
                 lease_s=7200):
    epoch = to_claimed(world, key, who, lease_s=lease_s)
    world.store.add_evidence(world.p(who), key, epoch, kind,
                             ref or {"id": "D-1"})
    world.store.submit(world.p(who), key, epoch)
    return epoch


def to_done(world, key, who="l1main", reviewer="reviewer", kind="decision"):
    epoch = to_in_review(world, key, who, kind)
    world.store.verdict(world.p(reviewer), key, True, "passed")
    return epoch
