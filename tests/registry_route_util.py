"""Shared helpers for the SPAWNER-REGISTRY-ROUTE tests (test_registry_route, test_registry_seats)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from dataclasses import replace

import pytest

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


#: The live route policy as it read before D-3552: no review lift, so
#: deepseek/glm stay banned/off for every role (the seat set these tests pin).
PRE_D3552_POLICY = json.dumps({
    "banned_families": ["kimi", "deepseek"],
    "off_families": ["glm"],
    "paused_harnesses": {
        "qodercli": {"allowed_roles": ["review"]},
        "agy": {"allowed_roles": ["implement", "orchestrate"]},
    },
})


#: The five D-3552 opencode-go legs' catalog models: their benchmark rows rank
#: a tools-mode leg above the inline-only luna/mimo-pro seats the tests below pin.
NEW_LEG_MODELS = frozenset({
    "opencode-go/gemini-4-argon-contributor",
    "opencode-go/glm-5.2-max",
    "opencode-go/qwen3.8-max",
    "opencode-go/muse-spark-1.3-contributor",
    "opencode-go/deepseek-v4-flash-max",
})


@pytest.fixture
def pre_d3552_policy(tmp_path, monkeypatch):
    """Pin the pre-D-3552 routing these seat tests assume.

    Route policy without ``review_allowed_families`` (deepseek/glm stay
    banned/off for every role) plus benchmark data without the five D-3552
    rows.  The seat set is then the one the assertions pin: qodercli/qwen plus
    the inline-only luna/gpt (the benchmark may still permute luna/mimo-pro).
    """
    path = tmp_path / "pre-d3552-route-policy.json"
    path.write_text(PRE_D3552_POLICY)
    monkeypatch.setenv("PLANGRAPH_ROUTE_POLICY", str(path))
    from plangraph.spawner import bench_entry
    real_rows = bench_entry.benchmark_rows_from_db

    def pre_d3552_rows(db_path):
        found, errors = real_rows(db_path)
        return {key: row for key, row in found.items() if key not in NEW_LEG_MODELS}, errors
    monkeypatch.setattr(bench_entry, "benchmark_rows_from_db", pre_d3552_rows)
    return path


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
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two\n")
    run("commit", "-qam", "head")
    head = run("rev-parse", "HEAD").stdout.strip()
    return repo, base, head
