"""Replan regressions anchored to production caller shapes and gate setup."""
import os
from pathlib import Path

import pytest

from keeper_util import seed
from test_keeper_worker_principal import scoped, require_backend
from plangraph.keeper.errors import Refused
from plangraph.planner.worker_claim import worker_claim


def test_rust_lock_arguments_from_claim(scoped, tmp_path, monkeypatch):
    from test_phase_finish import git_repo
    from test_planner_phase import card

    world, principal = scoped
    key, = seed(world, [("A", [])])
    world.store.put_card(world.p("router"), key, card())
    root, _ = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    claim = worker_claim(world.store, principal, key, repo_root=str(root),
                         base_branch="phase/demo-PH-X")
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


def test_fixture_generates_token_instead_of_literal():
    source = Path("tests/test_keeper_worker_principal.py").read_text()
    assert 'token = auth.add_principal(' in source
    assert 'auth.authenticate(world.conn, token)' in source


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
    principal.worker_scope = False
    principal.role = role
    with pytest.raises(Refused, match="worker scope|authenticated worker scope"):
        world.store.snapshot(principal)
