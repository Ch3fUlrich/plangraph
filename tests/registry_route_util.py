"""Shared helpers for the SPAWNER-REGISTRY-ROUTE tests (test_registry_route, test_registry_seats)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from dataclasses import replace

import pytest

from git_identity import identity_env
from plangraph.planner.legs import DEFAULT_LEGS, HARNESSES, TOOL_MODES
from plangraph.planner.route_policy import RoutePolicy, load_route_policy
from plangraph.planner.routing import SpawnRefused, plan_spawn
from plangraph.routing_support import resolver
from plangraph.routing_support.catalog_db import CatalogDB
from plangraph.catalog_data import SEED_MODELS

CARD = {"estimate_tokens": 20000}
DIFF = {"repo": "/srv/repo", "base": "a1b2", "head": "c3d4"}
RCARD = dict(CARD, diff_ref=DIFF)
HIGH = {"estimate_tokens": 20000, "risk": "high", "reviewer_count": 2, "diff_ref": DIFF}


@pytest.fixture(autouse=True)
def live_policy(monkeypatch):
    # conftest pins a neutral policy for mechanics tests; these pin the live file.
    monkeypatch.delenv("PLANGRAPH_ROUTE_POLICY", raising=False)


def _seeded():
    db = CatalogDB(":memory:")
    db.init_and_seed()
    return db


def _row(db, model_id):
    return next(r for r in resolver.get_catalog_models(db) if r["id"] == model_id)


class _Runner:
    def __init__(self):
        self.jobs, self._processes, self._tasks, self.provider_responses = {}, {}, {}, {}
        self.calls = []

    async def spawn(self, **kw):
        from plangraph.spawner.runner import JobInfo
        self.calls.append(kw)
        job = JobInfo("job-%d" % len(self.calls), "dry_run" if kw.get("dry_run") else "completed",
                      kw["prompt"], kw["model_or_combo"])
        self.jobs[job.job_id] = job
        return job


def _service(tmp_path, monkeypatch, db=None):
    from plangraph.spawner.service import SpawnService
    db = db or _seeded()
    monkeypatch.setattr(resolver, "_get_default_catalog_db", lambda: db)
    return SpawnService(_Runner(), state=tmp_path)


def _families(plan):
    return [leg.family for leg in plan.review_seats]


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=identity_env())
    run("init", "-q")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two\n")
    run("commit", "-qam", "head")
    head = run("rev-parse", "HEAD").stdout.strip()
    return repo, base, head
