"""The opt-in identity's transport and audited store allowlists."""
import os
import pytest

from keeper_util import World
from plangraph.keeper import auth
from plangraph.keeper.api_routes import READ_VERBS, WRITE_VERBS, run_gate
from plangraph.keeper.errors import Refused
from plangraph.keeper.worker_policy import ACTIONS, VERBS


def require_backend(backend):
    if backend == "postgres" and not os.environ.get("PG_DSN"):
        if os.environ.get("PG_KEEPER_BACKEND") == "postgres":
            pytest.fail("PostgreSQL declared but PG_DSN not configured")
        pytest.skip("PG_DSN not configured")


@pytest.fixture(params=["sqlite", "postgres"])
def scoped(tmp_path, request):
    require_backend(request.param)
    world = World(tmp_path, backend=request.param)
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


STORE_CALLS = {
    "claim": ("claim", ("demo:A",)),
    "show": ("show", ("demo:A",)),
    **{action: (action, ({"card": "demo:A"},)) for action in (
        "worker_attempt", "worker_park", "worker_complete", "worker_alarm")},
    "lock_acquire": ("lock_acquire", ("repo", {})),
    "lock_release": ("lock_release", ("repo", {})),
    "ask_question": ("ask_question", ("demo:A", "question")),
    "answer_question": ("answer_question", (1,)),
    "get_card": ("get_card", ("demo:A",)),
}


@pytest.mark.parametrize("action", sorted(STORE_CALLS))
def test_each_worker_store_action(scoped, action, monkeypatch):
    world, principal = scoped
    from plangraph.keeper.worker_policy import check

    class ReachedRun(Exception):
        pass

    def observed(who, actual, key, callback):
        assert who is principal
        assert actual == action
        check(who, actual)
        raise ReachedRun

    monkeypatch.setattr(world.store, "_run", observed)
    method, args = STORE_CALLS[action]
    with pytest.raises(ReachedRun):
        getattr(world.store, method)(principal, *args)


def test_store_action_inventory():
    # The merge-evidence action is reached by the real finish route below.
    assert ACTIONS == STORE_CALLS.keys() | {"attach_merge_evidence"}


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


def test_scoped_phase_routes(scoped, tmp_path, monkeypatch):
    from plangraph.keeper import api_routes
    from plangraph.keeper.api import KeeperHandler
    from test_phase_finish import FakeGitHub, finish_cards, gate, git_repo
    from test_planner_phase import card

    world, principal = scoped
    gate(world)
    actions = []
    original_run = world.store._run

    def observed(who, action, key, callback):
        if who is principal:
            actions.append(action)
        return original_run(who, action, key, callback)

    monkeypatch.setattr(world.store, "_run", observed)
    for key in ("demo:C1", "demo:C2"):
        world.store.put_card(world.p("router"), key, card())
    root, head = git_repo(tmp_path)
    monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(root))
    fake = FakeGitHub()
    monkeypatch.setattr(api_routes, "_github_from_env", lambda: fake)
    need = KeeperHandler._need
    run_gate(principal, "phase_plan")
    plan = api_routes._phase_plan(world.store, principal, {"key": "demo:PH-X"}, need)
    assert {c["key"] for c in plan["cards"]} == {"demo:C1", "demo:C2"}
    finish_cards(world, "demo:C1", "demo:C2")
    run_gate(principal, "phase_finish")
    result = api_routes._phase_finish(world.store, principal, {
        "key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": head}, need)
    assert result["state"] == "merged"
    assert result["merge_sha"] == fake.merge_sha
    assert "attach_merge_evidence" in actions
    assert world.conn.execute(
        "SELECT count(*) FROM evidence WHERE kind='commit+ci'").fetchone()[0] == 2


def test_backend_refusal_coverage(monkeypatch):
    monkeypatch.setenv("PG_KEEPER_BACKEND", "postgres")
    monkeypatch.delenv("PG_DSN", raising=False)
    with pytest.raises(BaseException) as exc:
        require_backend("postgres")
    assert isinstance(exc.value, pytest.fail.Exception)
    assert "PostgreSQL declared" in str(exc.value)


@pytest.mark.parametrize("route", ["phase_plan", "phase_finish"])
def test_phase_helpers_enforce_scope_without_run_gate(scoped, monkeypatch, route):
    from plangraph.keeper import api_routes, worker_policy
    from plangraph.keeper.api import KeeperHandler
    world, principal = scoped
    monkeypatch.setattr(worker_policy, "VERBS", worker_policy.VERBS - {route})
    with pytest.raises(Refused, match="scope excludes"):
        getattr(api_routes, "_" + route)(world.store, principal, {
            "key": "demo:PH-X", "branch": "phase/demo-PH-X", "head": "a" * 40,
        }, KeeperHandler._need)


def test_principal_reread_security_rationale():
    from plangraph.keeper.store import Store
    doc = Store._check_principal.__doc__
    assert "authentication cannot keep acting" in doc
    assert "stale or forged identity" in doc
