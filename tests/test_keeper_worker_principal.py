"""The opt-in identity's transport and audited store allowlists."""
import pytest

from keeper_util import World
from plangraph.keeper import auth
from plangraph.keeper.api_routes import READ_VERBS, WRITE_VERBS, run_gate
from plangraph.keeper.errors import Refused
from plangraph.keeper.worker_policy import ACTIONS, VERBS


@pytest.fixture
def scoped(tmp_path):
    world = World(tmp_path)
    token = auth.add_principal(world.conn, "phase-runner", "phase-worker", ["demo"])
    yield world, auth.authenticate(world.conn, token)
    world.close()


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_each_worker_transport_verb(scoped, verb):
    _, principal = scoped
    run_gate(principal, verb)


@pytest.mark.parametrize("verb", sorted((READ_VERBS | WRITE_VERBS) - VERBS))
def test_every_other_transport_verb_refused(scoped, verb):
    _, principal = scoped
    with pytest.raises(Refused, match="scope excludes"):
        run_gate(principal, verb)


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_each_worker_store_action(scoped, action):
    world, principal = scoped
    assert world.store._run(principal, action, None, lambda now: "allowed") == "allowed"


def test_store_refuses_outside_scope_and_old_tokens_unchanged(scoped):
    world, principal = scoped
    with pytest.raises(Refused, match="scope excludes"):
        world.store.snapshot(principal)
    for name in world.tokens:
        assert auth.authenticate(world.conn, world.tok(name)) == world.p(name)
    assert world.store.snapshot(world.p("system")) is not None


def test_scope_matches_rust_client():
    import re
    from pathlib import Path
    source = Path("rust/worker/src/keeper.rs").read_text()
    paths = set(re.findall(r'"/v1/([a-z_]+)', source))
    assert paths == VERBS


def test_scoped_worker_claim_show_and_locks(scoped):
    from keeper_util import seed
    world, principal = scoped
    key, = seed(world, [("A", [])])
    world.store.claim(principal, key)
    assert world.store.show(principal, key)
    fence = world.store.lock_acquire(principal, "repo", {"owner": "owner", "ttl_s": 30})
    assert fence
