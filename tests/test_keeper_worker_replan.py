"""Replan regressions anchored to production caller shapes and gate setup."""
import os
from pathlib import Path

import pytest

from keeper_util import seed
from test_keeper_worker_principal import scoped, require_backend
from plangraph.keeper.errors import Refused
from plangraph.planner.worker_claim import worker_claim


def test_rust_lock_arguments_from_claim(scoped, tmp_path, monkeypatch):
    from plangraph.planner.phase_finish import phase_branch
    from test_phase_finish import git_repo
    from test_planner_phase import card

    world, principal = scoped
    key, = seed(world, [("A", [])])
    world.store.put_card(world.p("router"), key, card())
    root, _ = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    claim = worker_claim(world.store, principal, key, repo_root=str(root),
                         base_branch=phase_branch("demo:PH-X"))
    assert claim["repo"] == str(root)
    source = Path("rust/worker/src/activities.rs").read_text()
    assert 'keeper.acquire_merge_lock(&input.repo, owner, merge_lock_ttl_s(cfg))' in source
    assert 'self.keeper.release_merge_lock(self.repo, &self.fence)' in source
    client = Path("rust/worker/src/keeper.rs").read_text()
    assert 'encode_segment(repo)' in client
    fence = world.store.lock_acquire(principal, claim["repo"],
                                     {"owner": "pg-worker-test", "ttl_s": 60})
    assert world.store.lock_release(principal, claim["repo"], fence) == {}


@pytest.mark.parametrize("method", ["lock_acquire", "lock_release"])
def test_configured_repo_needs_server_card_scope(scoped, monkeypatch, method):
    world, principal = scoped
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", "/repo")
    with pytest.raises(Refused, match="claimed project card"):
        getattr(world.store, method)(principal, "/repo", {})


def test_declared_postgres_never_silently_skips(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("PG_KEEPER_BACKEND", "postgres")
    with pytest.raises(pytest.fail.Exception, match="PG_DSN"):
        require_backend("postgres")


def test_observer_reopens_authenticated_backend(scoped):
    from plangraph.planner.jev_shadow import observer_connection

    world, principal = scoped
    key, = seed(world, [("A", [])])
    conn = observer_connection(world.store)()
    try:
        assert conn.execute("SELECT project FROM items WHERE key=?",
                            (key,)).fetchone()[0] == "demo"
    finally:
        conn.close()


@pytest.mark.parametrize("role", ["phase-worker", "system"])
def test_worker_cannot_disable_stored_scope(scoped, role):
    world, principal = scoped
    object.__setattr__(principal, "worker_scope", False)
    object.__setattr__(principal, "role", role)
    with pytest.raises(Refused, match="worker scope|authenticated worker scope"):
        world.store.snapshot(principal)


@pytest.mark.parametrize("attribute,value", [("role", "system"), ("worker_scope", False)])
def test_worker_identity_is_frozen(scoped, attribute, value):
    from dataclasses import FrozenInstanceError

    _, principal = scoped
    with pytest.raises(FrozenInstanceError):
        setattr(principal, attribute, value)


@pytest.mark.parametrize("role,scope", [("system", True), ("phase-worker", False),
                                        ("system", False)])
def test_corrupted_worker_identity_refused_at_every_gate(scoped, role, scope):
    from plangraph.keeper.worker_policy import check, check_identity

    world, principal = scoped
    key, = seed(world, [("A", [])])
    world.store.claim(world.p("workerA"), key)
    object.__setattr__(principal, "role", role)
    object.__setattr__(principal, "worker_scope", scope)
    row = world.conn.execute("SELECT role FROM principals WHERE name=?",
                             (principal.name,)).fetchone()
    for transport in (False, True):
        with pytest.raises(Refused) as denied:
            check(principal, "park" if transport else "worker_park", transport=transport)
        assert denied.value.code == "forbidden"
    with pytest.raises(Refused):
        check_identity(principal, row)
    for method, payload in (("worker_park", {"card": key, "exit_class": "ok"}),
                            ("worker_complete", {"card": key, "merged_sha": "a" * 40}),
                            ("worker_alarm", {"card": key, "reason": "test"})):
        with pytest.raises(Refused) as denied:
            getattr(world.store, method)(principal, payload)
        assert denied.value.code == "forbidden"
    assert world.store.show(world.p("router"), key)["claimed_by"] == world.p("workerA").name
